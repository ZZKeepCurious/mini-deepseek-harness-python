"""第 6 章：子 agent 远程通道的 stdio worker —— 真子进程端点。

`python -m miniharness.seams.subagent.worker <acp|sdk> [--permission allow|reject]`

每个 worker 是独立进程：读 stdin 的 newline-delimited JSON-RPC 请求帧，
以 AcpServer / SdkRuntime 承载回合（假模型适配器），事件通知先于响应帧
写出（mini 同步载体的顺序约定，上游为并发流），EOF 后退出 0。

对齐上游：ACP 通道（subagent-acp run.ts 的 startAcpRun）与 SDK 通道
（subagent-dsh-sdk run.ts 的 startSdkRun）的子进程端点；mini 端点的
服务器面直接复用第 12 章已对齐的 AcpServer / SdkRuntime。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ...protocol.acp import AcpServer
from ...protocol.sdk import JsonRpcLineTransport, SdkRuntime


def _write_stdout(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _error_frame(id_: str, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id_,
                       "error": {"code": code, "message": message}}, ensure_ascii=False)


# ---------- ACP 协议端点 ----------

def run_acp_worker(permission: str) -> int:
    server = AcpServer()
    if permission == "reject":
        server.set_answerer(lambda request: "reject-once")
    else:
        server.set_answerer(lambda request: "allow-once")
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            message = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(message, dict) or not isinstance(message.get("id"), str):
            continue
        id_ = message["id"]
        method = message.get("method")
        params = message.get("params") or {}
        try:
            result = _dispatch_acp(server, method, params)
            _write_stdout(json.dumps({"jsonrpc": "2.0", "id": id_, "result": result}, ensure_ascii=False))
        except Exception as error:
            code = error.code if isinstance(error, Exception) and hasattr(error, "code") else -32603
            _write_stdout(_error_frame(id_, code, str(error)))
    return 0


def _dispatch_acp(server: AcpServer, method: Any, params: dict) -> Any:
    if method == "initialize":
        return server.initialize()
    if method == "newSession":
        result = server.new_session(params.get("cwd", "."))
        return result
    if method == "prompt":
        session_id = params["sessionId"]
        result = server.prompt(session_id, params.get("prompt") or [])
        _notify_acp_updates(server, session_id)
        return result
    if method == "cancel":
        server.cancel(params.get("sessionId", ""))
        return None
    if method == "shutdown":
        server.close()
        return None
    raise ValueError(f"method not found: {method}")


def _notify_acp_updates(server: AcpServer, session_id: str) -> None:
    """prompt 完成后发一次 session 更新通知（最后一次提交的 assistant 文本）。

    对齐 ACP 规范 session 通知形态；mini 同步载体只发终态一条
    （上游是流式多次，简化标注）。
    """
    record = server.sessions.get(session_id)
    if record is None:
        return
    text = record["loop"].last_response()
    if not text:
        return
    _write_stdout(json.dumps({"jsonrpc": "2.0", "method": "session", "params": {
        "sessionId": session_id,
        "updates": [{"type": "assistant", "message": {
            "role": "assistant", "content": [{"type": "text", "text": text}]}}],
    }}, ensure_ascii=False))


# ---------- SDK 协议端点 ----------

class _SdkWorkerRuntime:
    """SdkRuntime 包装：session/prompt 后发 session.event 通知（输出流）。

    对齐上游 wire 的 session.event 通知词汇；mini 以终态一条承载
    （上游逐块流式，简化标注）。
    """

    def __init__(self):
        self.runtime = SdkRuntime()
        self._notifications: list[tuple[str, dict]] = []

    def handle(self, method: str, params: dict) -> Any:
        result = self.runtime.handle(method, params)
        if method == "session/prompt":
            session_id = params.get("sessionId")
            loop = self.runtime.sessions.get(session_id)
            text = loop.last_response() if loop else ""
            if text:
                self._notifications.append(("session.event", {
                    "sessionId": session_id,
                    "event": {"type": "assistant/message", "data": {
                        "content": [{"type": "text", "text": text}]}},
                }))
        return result

    def drain(self) -> list[tuple[str, dict]]:
        notifications = self._notifications
        self._notifications = []
        return notifications


def run_sdk_worker() -> int:
    runtime = _SdkWorkerRuntime()
    transport = JsonRpcLineTransport()
    transport.on_request(runtime.handle)
    for line in sys.stdin:
        transport.feed(line)
        responses = transport.out_lines
        transport.out_lines = []
        # 通知先于响应帧写出：客户端读到响应即返回，通知必须先行
        for method, params in runtime.drain():
            transport.notify(method, params)
        transport.out_lines.extend(responses)
        for out in transport.out_lines:
            _write_stdout(out)
        transport.out_lines.clear()
    return 0


def main() -> int:
    sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(prog="miniharness.seams.subagent.worker")
    parser.add_argument("protocol", choices=("acp", "sdk"))
    parser.add_argument("--permission", choices=("allow", "reject"), default="reject")
    args = parser.parse_args()
    if args.protocol == "acp":
        return run_acp_worker(args.permission)
    return run_sdk_worker()


if __name__ == "__main__":
    sys.exit(main())