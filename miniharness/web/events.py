"""web 远程事件流：`$events` 注册表 + api-session/* 转发源 + 审批瀑布结算。

对齐上游 `packages/api/gateway/src/index.ts` 的 remote-event 子系统 +
`packages/api/session-controller` 的 api-session/* 事件 + `packages/api/remotes/`：

  * `$events` 流端点：payload 恰 `{args:{}}` → 首个下游帧取 `ready`
    （clientId + host.home，供前端缩写本机路径显示），随后逐帧转发下游帧
    （emit / waterfall / cancel）。结果经 HTTP `$events/result` unary 回来
    （`parse_remote_event_result_payload` 是 wire 入口），对已结算/被取代的
    投递幂等 no-op（对齐 receiveRemoteEventResult）。
  * 转发源（emit 模式，session 控制器 api-session/* 族）：
      session/created → api-session/added（初始 list row）
      session/disposed → api-session/removed（sessionId）
      agent/status    → api-session/status（sessionId, running）
      agent/error     → api-session/error（sessionId, failure.message）
      user/message(source.user) → api-session/activity（sessionId, event.time）
  * 瀑布（waterfall）模式：审批问询经 `invoke('approval/request', agentId,
    request)` 投递为 waterfall 帧，首个客户端的 `$events/result` 结算；无应答
    时挂起，`dispose()` 全量 'cancelled'（对齐 closeRemoteEvents）。projected
    request 由 `tools/ask` 闸门映射（上游由 last-resort 转发，mini 的同步 core
    ApprovalService 是独立教学面，见 AGENTS.md 标注）。

mini 简化（须同步 verified-diffs §3.4）：只转发 api-session/* + approval/request
两个族；无 agent registry 时 agentId = 会话 id（root 常驻，无子代理下游）。
"""
from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from .stream_protocol import (
    REMOTE_EVENT_STREAM_ENDPOINT,
    is_remote_event_agent_id,
    is_remote_json_value,
)

__all__ = ["RemoteEventRegistry", "EventSourceFailure"]


class EventSourceFailure(RuntimeError):
    """`$events` 流打开失败（open 内抛，mux 折成 error 帧）。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


class _ClientQueue:
    """一个客户端事件代次的缓冲队列（pull + 唤醒，同步推进、异步消费）。"""

    def __init__(self, id_: str):
        self.id = id_
        self._frames: list[dict] = []
        self._waiter = None
        self.closed = False
        self._loop = asyncio.get_running_loop()

    def push(self, frame: dict) -> None:
        if self.closed:
            return
        self._frames.append(frame)
        self._wake()

    def pull(self) -> dict | None:
        if self._frames:
            return self._frames.pop(0)
        return None

    def set_waiter(self, callback) -> None:
        self._waiter = callback

    def end(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._wake()

    def _wake(self) -> None:
        waiter = self._waiter
        self._waiter = None
        if waiter is not None:
            loop = self._loop
            if loop.is_closed():
                return
            loop.call_soon_threadsafe(waiter)


class _PendingInvocation:
    """一次挂起的 Host→Client waterfall：投递集合 + 结算 future。

    settle 语义（对齐 gateway finishRemoteEvent + source.resolve/reject）：
      result    —— 第一个客户端投出结果（kind=='result'）→ ('result', value)
      next      —— 投递集合耗尽且客户端 next() → ('next', None)
      rejected  —— 客户端监听器抛错 → ('rejected', {name, message, code?, details?})
      cancelled —— 源销毁/天折 → ('cancelled', reason)
    """

    def __init__(self, id_: str, event: str, agent_id: str, request: dict,
                 signal=None):
        self.id = id_
        self.event = event
        self.agent_id = agent_id
        self.request = request
        self.signal = signal
        self.deliveries: set[_ClientQueue] = set()
        self.frame = {"type": "waterfall", "event": event, "eventId": id_,
                      "agentId": agent_id, "request": request}
        loop = asyncio.get_running_loop()
        self._settled: asyncio.Future = loop.create_future()

    def settle(self, kind: str, value: Any = None) -> None:
        if self._settled.done():
            return
        loop = self._settled.get_loop()
        loop.call_soon_threadsafe(self._settled.set_result, (kind, value))

    @property
    def done(self) -> bool:
        return self._settled.done()

    async def wait(self):
        return await self._settled


def _host_home() -> str:
    return str(Path.home())


class RemoteEventRegistry:
    """`$events` 流注册表：客户端代次、挂起 waterfall、api-session/* 转发源。

    WebApi 构造时创建本对象并调用 `setup_source(api)` 注册会话事件转发；
    `mux.py` 的 `$events` open 与 `server.py` 的 `$events/result` 直接消费本表。
    """

    def __init__(self, home: str | None = None):
        self.home = home if home is not None else _host_home()
        self._clients: dict[str, _ClientQueue] = {}
        self._pending: dict[str, _PendingInvocation] = {}
        self._source_installed = False
        self._disposers: list[Any] = []

    # ---------- 会话事件转发源（api/remotes remoteEventSource 同款） ----------

    def setup_source(self, api: Any) -> None:
        """在 api.ctx 上注册 api-session/* 转发监听（幂等）。"""
        if self._source_installed:
            return
        self._source_installed = True

        def on_created(payload: dict) -> None:
            session = payload["session"]
            loop = api._agents.get(session.session_id)
            running = loop.status == "running" if loop is not None else False
            self.broadcast("api-session/added", api._summary(session, running))

        def on_disposed(payload: dict) -> None:
            session = payload["session"]
            self.broadcast("api-session/removed", session.session_id)

        def on_status(payload: dict) -> None:
            agent = payload["agent"]
            status = payload.get("status")
            self.broadcast("api-session/status", agent.id, status == "running")

        def on_error(payload: dict) -> None:
            agent = payload["agent"]
            error = payload.get("error")
            message = error.get("message") if isinstance(error, dict) else str(error)
            self.broadcast("api-session/error", agent.id,
                           message if message else str(error))

        def on_session_event(payload: dict) -> None:
            event = payload.get("event")
            if (getattr(event, "get", None) is None
                    or event.get("type") != "user/message"
                    or (event.get("data") or {}).get("source", {}).get("kind") != "user"):
                return
            session = payload.get("session")
            session_id = getattr(session, "session_id", None)
            if session_id is None:
                return
            self.broadcast("api-session/activity", session.session_id, event.get("time"))

        ctx = api.ctx
        self._disposers = [
            ctx.on("session/created", on_created, global_=True),
            ctx.on("session/disposed", on_disposed, global_=True),
            ctx.on("agent/status", on_status, global_=True),
            ctx.on("agent/error", on_error, global_=True),
            ctx.on("session/event", on_session_event, global_=True),
        ]

    # ---------- 流侧：$events open + $events/result ----------

    async def open(self, payload: Any, signal=None):
        """打开一个客户端代次：校验 payload → ready 帧 + 续帧（对齐 openRemoteEvents）。

        @param payload - 必须恰为 `{args:{}}`。
        @param signal - 可选取消句柄（mux 关闭时终止生成器）。
        """
        if (not isinstance(payload, dict)
                or set(payload) != {"args"}
                or not isinstance(payload.get("args"), dict)
                or payload["args"]):
            raise EventSourceFailure(
                "gateway/arguments-invalid",
                f"typert gateway: {REMOTE_EVENT_STREAM_ENDPOINT}: "
                "forwarded Remote event stream requires an empty args object",
                {},
            )
        client = _ClientQueue(str(uuid.uuid4()))
        self._clients[client.id] = client
        for pending in list(self._pending.values()):
            self._deliver(pending, client)
        try:
            yield {"type": "ready", "clientId": client.id, "host": {"home": self.home}}
            while True:
                frame = client.pull()
                if frame is not None:
                    yield frame
                    continue
                if client.closed:
                    return
                wake = asyncio.Event()
                client.set_waiter(wake.set)
                frame = client.pull()
                if frame is not None:
                    client.set_waiter(None)
                    yield frame
                    continue
                await wake.wait()
        finally:
            self._remove_client(client)

    def receive_result(self, result: dict) -> None:
        """处理一条 `$events/result`（词法已由 parse_remote_event_result_payload 校验）。

        对应 gateway dispatchRpc 的 $events/result 分支 + receiveRemoteEventResult：
        未知 clientId → 抛错（server 折进 RpcResult internal）；已结算/被取代的
        投递幂等 no-op。
        """
        client = self._clients.get(result["clientId"])
        if client is None:
            raise EventSourceFailure(
                "gateway/internal",
                "typert gateway: Remote event result identifies no active event stream",
                {})
        pending = self._pending.get(result["eventId"])
        if pending is None or client not in pending.deliveries:
            return
        outcome = result["outcome"]
        kind = outcome["kind"]
        if kind == "result":
            self._remove_delivery(pending, client)
            self._settle(pending, "result", outcome.get("value"))
        elif kind == "rejected":
            self._cancel(pending, outcome["error"])
        else:  # kind == "next"：该客户端向上游委托；若无其它待交付 → 'next'
            self._remove_delivery(pending, client)
            if not pending.deliveries:
                self._settle(pending, "next")

    # ---------- 宿主侧：广播与瀑布 water ----------

    def broadcast(self, event: str, *args: Any) -> None:
        """转发一个 emit 宿主事件（args 须无损 JSON，转发源已保证）。"""
        if not event or not isinstance(event, str):
            raise EventSourceFailure("gateway/internal",
                                     "typert gateway: Remote event name must be a "
                                     "nonempty string", {})
        frame = {"type": "emit", "event": event, "args": list(args)}
        for client in list(self._clients.values()):
            client.push(frame)

    async def invoke(self, event: str, agent_id: str, request: dict,
                     signal=None) -> tuple[str, Any]:
        """投递一个 agent 域 waterfall 给所有客户端，等待首个结果（startRemoteEvent）。

        @returns (kind, value)：'result'(value) / 'next' / 'rejected'
            ({name,message,code?,details?}) / 'cancelled'。
        """
        if (not event or not isinstance(event, str)
                or not is_remote_event_agent_id(agent_id)):
            raise EventSourceFailure("gateway/internal",
                                     "typert gateway: scoped Remote events require "
                                     "a non-empty Agent identity", {})
        if not is_remote_json_value(request):
            raise EventSourceFailure("gateway/internal",
                                     "typert gateway: Remote event request is not "
                                     "lossless JSON data", {})
        pending = _PendingInvocation(str(uuid.uuid4()), event, agent_id, request,
                                     signal)
        self._pending[pending.id] = pending
        for client in list(self._clients.values()):
            self._deliver(pending, client)
        return await pending.wait()

    # ---------- 内部 ----------

    def _deliver(self, pending: _PendingInvocation, client: _ClientQueue) -> None:
        pending.deliveries.add(client)
        client.push(pending.frame)

    def _remove_delivery(self, pending: _PendingInvocation, client: _ClientQueue) -> None:
        pending.deliveries.discard(client)

    def _remove_client(self, client: _ClientQueue) -> None:
        self._clients.pop(client.id, None)
        for pending in list(self._pending.values()):
            pending.deliveries.discard(client)
        client.end()

    def _settle(self, pending: _PendingInvocation, kind: str, value: Any = None) -> None:
        self._finish(pending)
        pending.settle(kind, value)

    def _cancel(self, pending: _PendingInvocation, reason: Any) -> None:
        self._finish(pending)
        pending.settle("rejected" if isinstance(reason, dict) else "cancelled", reason)

    def _finish(self, pending: _PendingInvocation) -> None:
        if self._pending.get(pending.id) is not pending:
            return
        self._pending.pop(pending.id, None)
        cancelled = set(pending.deliveries)
        for client in cancelled:
            pending.deliveries.discard(client)
        frame = {"type": "cancel", "eventId": pending.id}
        for client in cancelled:
            client.push(frame)

    # ---------- 生命周期 ----------

    def dispose(self) -> None:
        """销毁转发源：全量 pending 'cancelled'，结束所有客户端代次（closeRemoteEvents）。"""
        for fn in reversed(self._disposers):
            fn()
        self._disposers.clear()
        self._source_installed = False
        for pending in list(self._pending.values()):
            self._cancel(pending, "forwarded Remote event source was removed")
        for client in list(self._clients.values()):
            client.end()
        self._clients.clear()