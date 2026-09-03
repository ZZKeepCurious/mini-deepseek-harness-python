"""第 6 章：子 agent 远程通道的 stdio worker —— 真子进程端点。

`python -m miniharness.seams.subagent.worker <acp|sdk> [--permission allow|reject]`

每个 worker 是独立进程：读 stdin 的 newline-delimited JSON-RPC 请求帧，
以 AcpServer / SdkRuntime 承载回合（假模型适配器）。ACP 通道经
update_sink 在回合执行期间逐事件并发流式写 session/update 通知（先于
响应帧；对齐上游 notify 并发流）；SDK 通道按事件终态逐条转发，EOF 后退出 0。

对齐上游：ACP 通道（subagent-acp run.ts 的 startAcpRun）与 SDK 通道
（subagent-dsh-sdk run.ts 的 startSdkRun）的子进程端点；mini 端点的
服务器面直接复用第 12 章已对齐的 AcpServer / SdkRuntime。
"""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from ...protocol.acp import AcpRequestError, AcpServer
from ...protocol.sdk import JsonRpcLineTransport, SdkRuntime
from ...core.session import thaw


def _write_stdout(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _error_frame(id_: str, code: int, message: str) -> str:
    return json.dumps({"jsonrpc": "2.0", "id": id_,
                       "error": {"code": code, "message": message}}, ensure_ascii=False)


# ---------- ACP 协议端点 ----------

def run_acp_worker(permission: str) -> int:
    server = AcpServer(update_sink=_acp_update_sink)
    if permission == "reject":
        # 上游 reject 策略经 wire 应答 {outcome:'cancelled'}（subagent-acp
        # run.ts:262），桥映射为审批 'cancelled'；对齐取 'cancelled' 而非 'rejected'
        server.set_answerer(lambda request: "cancelled")
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


def _acp_update_sink(session_id: str, update: dict) -> None:
    """把每条 session/update 即时写成一个 notification 帧（对齐上游 notify）。

    prompt 回合执行期间（followup 同步泵送、事件逐条 append 时）逐块外发，
    先于 prompt 的 response 帧到达客户端——并发流式，非回合后批量排发。
    """
    _write_stdout(json.dumps({
        "jsonrpc": "2.0",
        "method": "session/update",
        "params": {"sessionId": session_id, "update": update},
    }, ensure_ascii=False))


def _dispatch_acp(server: AcpServer, method: Any, params: dict) -> Any:
    if method == "initialize":
        return server.initialize()
    if method == "newSession":
        result = server.new_session(params.get("cwd", "."))
        return result
    if method == "prompt":
        result = server.prompt(session_id=params["sessionId"],
                               prompt=params.get("prompt") or [])
        return result
    if method == "cancel":
        server.cancel(params.get("sessionId", ""))
        return None
    if method == "shutdown":
        server.close()
        return None
    # 未知方法 → JSON-RPC -32601 method not found（已核实上游 @agentclientprotocol
    # /sdk 0.25.1 RequestError.methodNotFound → -32601，acp.js:548,1270）
    raise AcpRequestError(-32601, f"method not found: {method}")



# ---------- SDK 协议端点 ----------

class _SdkWorkerRuntime:
    """SdkRuntime 包装：session/prompt 后发 session.event / session.status 通知。

    对齐上游 wire 的 session.event / session.status 通知词汇。回合级透传：
    prompt 同步跑完整个回合后，把回合期间新增的 agent/inbox/spliced（含本次
    messageId 的 inserted 回执）、assistant/message 与 turn/end 事件逐条发
    session.event，末尾补 session.status = idle 通知；上游逐块流式透传，mini
    按事件逐条终态透传（简化标注）。
    """

    def __init__(self):
        self.runtime = SdkRuntime()
        self._notifications: list[tuple[str, dict]] = []
        self._event_boundary = 0

    def handle(self, method: str, params: dict) -> Any:
        result = self.runtime.handle(method, params)
        if method == "session/prompt":
            session_id = params.get("sessionId")
            loop = self.runtime.sessions.get(session_id)
            if loop is not None:
                self._emit_round_events(loop, session_id)
                self._notifications.append(("session.status", {
                    "sessionId": session_id,
                    "status": "idle",
                }))
        return result

    def _emit_round_events(self, loop: AgentLoop, session_id: str) -> None:
        """把本次 prompt 投递新增的 inbox 回执、assistant/message、turn/end 发成 session.event。

        以 followup 投递的消息 id 为界，且只扫本次投递之后的事件（不重发
        历史回合）：agent/inbox/spliced（inserted 含本次 messageId 的回执）、
        assistant/message 与 turn/end 逐条透传（上游 SDK Session.run 消费这
        三类事件以结算结果）。
        """
        wanted_seqs: list[int] = []
        message_id = self.runtime.last_message_id
        for event in loop.session.events[self._event_boundary:]:
            if event["type"] == "agent/inbox/spliced":
                inserted = event["data"].get("inserted")
                if inserted and any(
                    getattr(m, "get", lambda _k, _d=None: None)("id") == message_id
                    for m in inserted
                ):
                    wanted_seqs.append(event["seq"])
            elif event["type"] == "assistant/message":
                wanted_seqs.append(event["seq"])
            elif event["type"] == "turn/end":
                wanted_seqs.append(event["seq"])
        wanted = set(wanted_seqs)
        for event in loop.session.events:
            if event["seq"] not in wanted:
                continue
            plain = thaw(event)
            self._notifications.append(("session.event", {
                "sessionId": session_id,
                "event": {"type": plain["type"], "data": plain["data"]},
            }))
        self._event_boundary = len(loop.session.events)

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