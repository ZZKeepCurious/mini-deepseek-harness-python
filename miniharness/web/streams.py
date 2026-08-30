"""web 远程方法面（GatewayStreams）：`$events` 装配 + session.follow/control 流。

对齐上游 `packages/api/gateway/src/` + `packages/api/session-controller/src/`：
本类把 session-controller 进程侧能力折叠成一个可被 `web/mux.py` 按 endpoint
打开/取消的路由表（Remote method exports 的 stream 子集）：

  * `session.follow`  —— 单个会话跟随流（history.follow）：开流即一个 `snapshot`
    帧（header/cursor/records/hasMore/projections）后逐条 `event` 帧。
  * `session.control` —— 宿主级 live control：首个 `baseline` 帧（queues/jobs/
    projections）后按变更给 `queue` / `jobs` / `projection` 帧。
  * `$events`         —— 远程事件流（`web/events.py` RemoteEventRegistry），承载
    api-session/* 转发源 + 审批 waterfall（`web/approvals.py` bridge）。

进程侧数据来自 WebApi（`api._agents` 常驻 AgentLoop、`api.store` 的 Session、
`ctx` 的 jobs 注册表）。跨进程耦合面只有本类发布给 mux 的 wire 契约（endpoint
名 + 帧形状）；同包内部直接引用 WebApi（对齐上游同进程组装）。

mini 简化（须同步 verified-diffs §3.4）：follow 的 records 用会话日志事件流
`Session.events` 投影，无 page/projection 域的 message 对齐游标；`_attach` 冷
会话自动 resume 后取日志尾部快照，再以 `_poll_new_event`（轮询 + 空闲 sleep）实时
补 event 帧，无 since 恢复游标（重开流重拉）；jobs 来自 ctx 的 on_jobs_changed
回调，owner 恒为 AgentLoop；心跳 Ping 省略（Starlette WS 无 Ping API）。
"""
from __future__ import annotations

import asyncio
from typing import Any

from .events import RemoteEventRegistry
from .stream_protocol import REMOTE_EVENT_STREAM_ENDPOINT

__all__ = ["GatewayStreams", "RemoteStreamError"]

QUEUE_CAPACITY = 1024
FOLLOW_POLL_INTERVAL = 0.05


class RemoteStreamError(RuntimeError):
    """某 endpoint 打开/运转失败（上游 RemoteStreamError family 的 mini 折叠）。

    @param code - RPC 码（stream-server 折进 error 帧 details）。
    """

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _as_plain(value: Any) -> Any:
    from ..core.session.json import thaw
    return thaw(value)


class GatewayStreams:
    """WebApi 之上组装好的 Remote 方法面（$events + session.follow/control）。

    WebApi 构造时创建一次（`api.gateway`）；`web/mux.py` 的 WS open 按 endpoint
    分发到 `open_stream`；`web/server.py` 的 `$events/result` unary 经
    `receive_result` 消费；`dispose()` 级联清理转发源与审批桥。
    """

    def __init__(self, api: Any):
        self.api = api
        self.ctx = api.ctx
        self.events = RemoteEventRegistry(home=api.cwd)
        self.events.setup_source(api)
        from .approvals import RemoteApprovalBridge
        self.approvals = RemoteApprovalBridge(self)
        self._control_queues: dict[asyncio.Queue, None] = {}
        self._attached = False
        self._disposers: list[Any] = []
        self._follow_waiters: list[_FollowWaiter] = []

    # ---------- mux 分发入口 ----------

    def stream_kinds(self) -> dict[str, str]:
        """endpoint → 打开/消费实现（上游 RemoteMethod exports 的 stream 子集）。"""
        return {
            REMOTE_EVENT_STREAM_ENDPOINT: "$events",
            "session.follow": "follow",
            "session.control": "control",
        }

    def open_stream(self, endpoint: str, payload: Any, signal=None):
        """按 endpoint 打开一个流：返回 async 生成器（帧 value 序列）。

        @param payload - open 帧的 payload（`{args: ...}`，各 endpoint 自校验）。
        @param signal - 可选取消句柄（mux 关闭/客户端 cancel 时终止）。
        @raises EventSourceFailure / RemoteStreamError。
        """
        kind = self.stream_kinds().get(endpoint)
        if kind is None:
            raise RemoteStreamError(
                "internal",
                f"typert gateway: {endpoint}: no active Remote method exports this endpoint")
        if kind == "$events":
            return self.events.open(payload, signal=signal)
        if (not isinstance(payload, dict) or set(payload) != {"args"}
                or not isinstance(payload["args"], dict)):
            raise RemoteStreamError(
                "arguments-invalid",
                f"typert gateway: {endpoint}: requires exactly an args object")
        if kind == "follow":
            return self._follow(payload["args"], signal)
        return self._control(signal)

    # ---------- session.follow（历史跟随流） ----------

    async def _follow(self, args: dict, signal=None):
        address = args.get("address")
        if (not isinstance(address, dict) or address.get("kind") != "session"
                or not isinstance(address.get("sessionId"), str)
                or not address["sessionId"]):
            raise RemoteStreamError("arguments-invalid",
                                    "session.follow requires a session address")
        session_id = address["sessionId"]
        session = self.api.store.get(session_id)
        if session is None:
            raise RemoteStreamError("session-not-found",
                                    f'session "{session_id}" not found')
        if self.api._agents.get(session_id) is None:
            self.api._attach(session)
        seq = session.seq
        records = [self._record(e) for e in list(session.events)]
        has_more = False
        max_messages = args.get("maxMessages")
        if isinstance(max_messages, int) and max_messages > 0 and len(records) > max_messages:
            records = records[-max_messages:]
            has_more = True
        yield {"type": "snapshot", "header": self._header(session), "cursor": seq,
               "records": records, "hasMore": has_more, "projections": {}}
        subscribed = seq
        while True:
            event = await _poll_new_event(self.api, session_id, subscribed, signal)
            if event is None:
                return
            subscribed = event["seq"]
            yield {"type": "event", "event": event}

    @staticmethod
    def _header(session) -> dict:
        meta = session.meta
        header: dict[str, Any] = {"sessionId": session.session_id}
        if meta.get("cwd") is not None:
            header["cwd"] = meta["cwd"]
        if meta.get("parentSession") is not None:
            header["parentSessionId"] = meta["parentSession"]
        if meta.get("origin") is not None:
            header["origin"] = meta["origin"]
        return header

    @staticmethod
    def _record(event: dict) -> dict:
        return _as_plain(event)

    # ---------- session.control（宿主级 live control） ----------

    def _attach_control(self) -> None:
        if self._attached:
            return
        self._attached = True
        self._disposers = [
            self.ctx.on("session/event", self._on_session_event),
            self.ctx.on("session/created", self._on_session_created),
            self.ctx.on("session/disposed", self._on_session_disposed),
            self.ctx.on("agent/status", self._on_agent_status),
            self.ctx.on("agent/error", self._on_agent_error),
        ]
        jobs = self.ctx.get("jobs")
        if jobs is not None and hasattr(jobs, "on_jobs_changed"):
            self._disposers.append(jobs.on_jobs_changed(self._on_jobs_changed))

    async def _control(self, signal=None):
        self._attach_control()
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_CAPACITY)
        self._control_queues[queue] = None
        try:
            yield {"type": "baseline", "value": self._control_baseline()}
            while True:
                frame = await queue.get()
                yield frame
                if signal is not None and getattr(signal, "cancelled", lambda: False)():
                    return
        finally:
            self._control_queues.pop(queue, None)

    def _control_baseline(self) -> dict:
        queues: dict[str, list] = {}
        jobs: dict[str, list] = {}
        for session_id in sorted(self.api._agents):
            queues[session_id] = self._queue_view(session_id)
            jobs_view = self._jobs_view(session_id)
            if jobs_view:
                jobs[session_id] = jobs_view
        return {"queues": queues, "jobs": jobs, "projections": {}}

    def _on_session_event(self, payload: dict) -> None:
        session = payload["session"]
        if session.session_id not in self.api._agents:
            return
        event = payload["event"]
        if event.get("type") == "agent/inbox/spliced":
            frame = {"type": "queue", "sessionId": session.session_id,
                     "items": self._queue_view(session.session_id, event["data"])}
            for queue in list(self._control_queues):
                queue.put_nowait(frame)

    def _on_jobs_changed(self, owner: Any) -> None:
        session_id = getattr(owner, "id", None)
        if not session_id or session_id not in self.api._agents:
            return
        frame = {"type": "jobs", "sessionId": session_id,
                 "jobs": self._jobs_view(session_id)}
        for queue in list(self._control_queues):
            queue.put_nowait(frame)

    def _on_session_created(self, payload: dict) -> None:
        pass  # control baseline 覆盖已挂会话；新会话经 list/baseline 收敛

    def _on_session_disposed(self, payload: dict) -> None:
        session_id = payload["session"].session_id
        for queue in list(self._control_queues):
            queue.put_nowait({"type": "queue", "sessionId": session_id, "items": []})

    def _on_agent_status(self, payload: dict) -> None:
        pass  # running 位由 api-session/status 承担（$events），control 不发重复

    def _on_agent_error(self, payload: dict) -> None:
        pass

    def _queue_view(self, session_id: str, splice: dict | None = None) -> list[dict]:
        loop = self.api._agents.get(session_id)
        if loop is None:
            return []
        items = []
        for message in loop.inbox.next_turn:
            items.append({"id": message["id"], "placement": "queued",
                          "message": {"id": message["id"],
                                      "content": message.get("content", [])}})
        for message in loop.inbox.next_step:
            placement = ("steering" if message.get("source", {}).get("kind") == "user"
                         else "context")
            items.append({"id": message["id"], "placement": placement,
                          "message": {"id": message["id"],
                                      "content": message.get("content", [])}})
        return items

    def _jobs_registry(self):
        return self.ctx.get("jobs")

    def _jobs_view(self, session_id: str) -> list[dict]:
        loop = self.api._agents.get(session_id)
        jobs = self._jobs_registry()
        if loop is None or jobs is None:
            return []
        return [self._job_row(job) for job in jobs.list(loop)]

    @staticmethod
    def _job_row(snapshot: dict) -> dict:
        return {key: snapshot[key] for key in ("id", "kind", "label", "status",
                                               "startedAt", "detail", "finishedAt")
                if key in snapshot}

    # ---------- 生命周期 ----------

    def receive_result(self, result: dict) -> None:
        """把一条 `$events/result`（词法已由 stream_protocol 校验）交给注册表结算。"""
        self.events.receive_result(result)

    def dispose(self) -> None:
        for fn in reversed(self._disposers):
            fn()
        self._disposers.clear()
        self._attached = False
        self.events.dispose()
        self.approvals.dispose()


class _FollowWaiter:
    """未实现（保留占位以明确简化）：follow 用 `_poll_new_event` 轮询。"""


async def _poll_new_event(api: Any, session_id: str, from_seq: int, signal=None):
    """轮询会话日志在 from_seq 之后的新事件（简化 follow，无 since 恢复游标）。

    对齐 history.follow 的 gap-free 逐帧语义：从 `from_seq` 起按 seq 严格递增
    提取。mini 用短轮询替代事件通知（会话日志在进程内 append 即现成可读），
    心跳/取消信号与轮询间隔由调用方控制。
    """
    loop_ = api._agents.get(session_id)
    if loop_ is None:
        return None
    session = loop_.session
    while True:
        next_event = None
        for event in list(session.events):
            if event.get("seq", 0) > from_seq:
                next_event = event
                break
        if next_event is not None:
            return next_event
        if signal is not None and getattr(signal, "cancelled", lambda: False)():
            return None
        await asyncio.sleep(FOLLOW_POLL_INTERVAL)
