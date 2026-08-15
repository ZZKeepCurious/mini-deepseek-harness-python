"""第 6 章：子 agent 远程通道 —— fork / ACP / SDK 三通道。

对应 dsh 真实源码：packages/subagent/subagent（Service Definition）
+ subagent-fork-in-process（fork 通道）+ subagent-acp（ACP 通道）
+ subagent-dsh-sdk（SDK 通道）。

上游语义（已核实，各 provider 的 index.ts + run.ts）：
  * fork 是"进程内 fork"：子 agent 用父会话日志的 completed-turn 前缀
    作 seed（到最后一个 turn/end 为止）——当前工具回合不平衡，不能作为
    合法子会话重放；无完成回合则全新开始。seed 契约：seq 从 0 连续、
    无损 JSON、平衡。fork 继承父上下文（inheritsParentContext = true）。
  * ACP 通道：每个子 agent 独立进程 + ACP stdio 协议（上游 ndJsonStream
    ——newline-delimited JSON-RPC），共享无 Cordis 上下文；唯一从父读的
    东西是 workspace cwd；permission 策略自动应答子进程的权限提示
    （reject 默认拒绝一切 / allow 走第一个允许选项），不上报人。
  * SDK 通道：每个子 agent 是完整独立 runtime（own 组合/会话/模型/工具），
    经 stdio JSON-RPC（initialize → session/prompt 懒创建会话 → shutdown）；
    无任何 start 期能力（NO_START_CAPABILITIES）；输出经 session.event
    通知流收集。
  * 子进程 env：父 env 的凭据清洗副本 + 显式 env 转发（mini 简化：直传，
    标注）。

载体简化（须在文档标注）：上游 async 流式；mini 同步——prompt 请求在
worker 内跑完整回合后，事件通知先于响应帧写出（上游并发流无顺序契约），
客户端单读循环天然收集；permission 策略注入 AcpServer 的 answerer
（上游是 wire 的 session/request_permission 通知）。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import uuid
from typing import Any, Callable

from .seams import SubAgent, SubAgentProvider
from .session import Session, thaw


def completed_turn_prefix(events) -> list[dict]:
    """父日志的平衡已完成回合前缀：到最后一个 turn/end（含）为止。

    与上游 completedTurnPrefix 同语义：当前 in-flight 回合（工具调用
    未平衡）不能重放为合法子会话；无完成回合 → 空（全新子会话）。
    """
    last_end = None
    for index, event in enumerate(events):
        if event["type"] == "turn/end":
            last_end = index
    if last_end is None:
        return []
    return [thaw(event) for event in events[:last_end + 1]]


# ---------- 1) fork 通道（进程内，父上下文 seed） ----------

class ForkSubAgentProvider(SubAgentProvider):
    """fork 通道：子 agent 复用主循环，并以父日志前缀作 seed 继承上下文。

    对齐上游 subagent-fork-in-process：make_loop(system_prompt, seed) 由
    消费者注入（seed 为空列表 = 全新子会话）；父会话通过 spawn 的
    parent 参数传入（对齐上游 start(request.parent)）。
    """

    def __init__(self, make_loop: Callable[[str, list], Any]):
        self._make_loop = make_loop

    def spawn(self, name: str, system_prompt: str, parent: Any = None) -> SubAgent:
        seed = completed_turn_prefix(parent.session.events) if parent is not None else []
        return _ForkChild(self._make_loop(system_prompt, seed), name)


class _ForkChild(SubAgent):
    def __init__(self, loop, name: str):
        self._loop = loop
        self.name = name

    def run(self, task: str) -> str:
        self._loop.run(task)
        return self._loop.last_response()


# ---------- stdio JSON-RPC 客户端（进程通道共用） ----------

class _WorkerClosedError(RuntimeError):
    pass


class _StdioRpcClient:
    """子进程 stdio 上的 newline-delimited JSON-RPC 客户端。

    与上游 ndJsonStream/JsonRpcLineTransport 同帧形状；同步阻塞版。
    通知帧（无 id）在等待响应期间被收集进 notifications。
    """

    def __init__(self, proc: subprocess.Popen):
        self._proc = proc
        self.notifications: list[tuple[str, dict]] = []

    def request(self, method: str, params: dict | None = None) -> Any:
        id_ = "req_" + uuid.uuid4().hex
        frame: dict = {"jsonrpc": "2.0", "id": id_, "method": method}
        if params is not None:
            frame["params"] = params
        self._write(json.dumps(frame, ensure_ascii=False))
        while True:
            line = self._proc.stdout.readline()
            if not line:
                raise _WorkerClosedError(f"subagent worker closed before answering {method}")
            try:
                message = json.loads(line)
            except (ValueError, TypeError):
                continue
            if not isinstance(message, dict):
                continue
            if "method" in message and "id" not in message:
                self.notifications.append((message["method"], message.get("params") or {}))
                continue
            if message.get("id") != id_:
                continue
            error = message.get("error")
            if isinstance(error, dict):
                detail = error.get("message") if isinstance(error.get("message"), str) else "JSON-RPC error"
                raise RuntimeError(detail)
            return message.get("result")

    def shutdown(self) -> None:
        try:
            self.request("shutdown")
        except (_WorkerClosedError, OSError):
            pass
        try:
            self._proc.stdin.close()
        except OSError:
            pass

    def close(self) -> None:
        self.shutdown()
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
            self._proc.wait()

    def _write(self, line: str) -> None:
        try:
            self._proc.stdin.write(line + "\n")
            self._proc.stdin.flush()
        except (BrokenPipeError, OSError):
            raise _WorkerClosedError("subagent worker stdin closed") from None


def _worker_command(protocol: str, extra: list[str] | None = None) -> list[str]:
    return [sys.executable, "-m", "miniharness.subagent_worker", protocol, *(extra or [])]


# ---------- 2) ACP 通道（真子进程，ACP stdio 协议） ----------

class AcpSubAgentProvider(SubAgentProvider):
    """ACP 通道：每个子 agent 独立进程 + ACP 协议。

    对齐上游 subagent-acp：cwd 来自父 workspace（spawn 参数注入，缺省
    当前目录）；permission 策略自动应答（reject 默认 / allow），不上报人。
    """

    def __init__(self, permission: str = "reject"):
        if permission not in ("allow", "reject"):
            raise ValueError("permission must be 'allow' or 'reject'")
        self.permission = permission

    def spawn(self, name: str, system_prompt: str, cwd: str | None = None) -> SubAgent:
        proc = subprocess.Popen(
            _worker_command("acp", ["--permission", self.permission]),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        client = _StdioRpcClient(proc)
        try:
            client.request("initialize")
            result = client.request("newSession", {"cwd": cwd or os.getcwd()})
        except Exception:
            client.close()
            raise
        return _AcpChild(client, result["sessionId"], name)


class _AcpChild(SubAgent):
    def __init__(self, client: _StdioRpcClient, session_id: str, name: str):
        self._client = client
        self._session_id = session_id
        self.name = name
        self.stop_reason: str | None = None

    def run(self, task: str) -> str:
        pending = len(self._client.notifications)
        result = self._client.request("prompt", {
            "sessionId": self._session_id,
            "prompt": [{"type": "text", "text": task}],
        })
        self.stop_reason = result["stopReason"]
        return self._last_assistant_text(pending)

    def _last_assistant_text(self, start: int) -> str:
        for method, params in self._client.notifications[start:]:
            if method == "session" and params.get("sessionId") == self._session_id:
                for update in params.get("updates") or []:
                    block = update.get("message", {}).get("content", [])
                    texts = [b.get("text", "") for b in block if b.get("type") == "text"]
                    if texts:
                        return "".join(texts)
        return ""

    def close(self) -> None:
        self._client.close()


# ---------- 3) SDK 通道（真子进程，SDK stdio JSON-RPC） ----------

class SdkSubAgentProvider(SubAgentProvider):
    """SDK 通道：每个子 agent 是独立 runtime，经 SDK stdio 协议驱动。

    对齐上游 subagent-dsh-sdk：initialize（cwd/provider/model）→
    session/prompt（懒创建会话）→ shutdown；无 start 期能力。
    """

    def __init__(self, provider: str = "fake", model: str = "fake-model"):
        self.provider = provider
        self.model = model

    def spawn(self, name: str, system_prompt: str, cwd: str | None = None) -> SubAgent:
        proc = subprocess.Popen(
            _worker_command("sdk"),
            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
        client = _StdioRpcClient(proc)
        try:
            client.request("initialize", {
                "cwd": cwd or os.getcwd(),
                "provider": self.provider,
                "model": self.model,
            })
        except Exception:
            client.close()
            raise
        return _SdkChild(client, f"sub-{uuid.uuid4().hex[:8]}", name)


class _SdkChild(SubAgent):
    def __init__(self, client: _StdioRpcClient, session_id: str, name: str):
        self._client = client
        self._session_id = session_id
        self.name = name
        self.message_id: str | None = None

    def run(self, task: str) -> str:
        pending = len(self._client.notifications)
        result = self._client.request("session/prompt", {
            "sessionId": self._session_id,
            "contentBlocks": [{"type": "text", "text": task}],
        })
        self.message_id = result["messageId"]
        return self._last_assistant_text(pending)

    def _last_assistant_text(self, start: int) -> str:
        for method, params in self._client.notifications[start:]:
            if method == "session.event" and params.get("sessionId") == self._session_id:
                event = params.get("event") or {}
                if event.get("type") == "assistant/message":
                    texts = [b.get("text", "") for b in (event.get("data") or {}).get("content", [])
                             if b.get("type") == "text"]
                    if texts:
                        return "".join(texts)
        return ""

    def close(self) -> None:
        self._client.close()


def spawn_fork(make_loop: Callable[[str, list], Any]):
    """便捷工厂：ForkSubAgentProvider（缺省 make_session 由调用方注入）。"""
    return ForkSubAgentProvider(make_loop)