"""web 事件流：mux + host 两路下游帧（对齐 `packages/host/apiproxy/src/api/events.ts`）。

两路流都是 `server-request` 窄形帧（rpcId 是发起方签发、应答回显的那个 id；
纯推送帧里它标识这一帧本身）。mini 的 mux/host 帧不含 rpcId（教学简化：无
approval/question 等可应答交互，纯推送帧的 rpcId 对消费方无意义）。

迭代 1 帧集（events.ts 子集）：
  mux  = session/subscribed + session/event + session/queue + session/jobs
  host = host/session-added + host/session-removed + host/session-status
         + host/agent-error
未实现：approval/*、question/*、session/projection、workspace/*、
archived-sessions、host/remote-event、stream/error（无对应注册表/需应答交互）。

订阅语义（对齐上游）：
  * mux 打开时：为每个已挂 agent 的会话发 `session/subscribed` {sessionId,
    lastSeq=当前日志长度}，随后补该会话的 inbox 快照（session/queue，非空才发）
    与作业快照（session/jobs，非空才发）；之后实时转发 session/event，并在每次
    agent/inbox/spliced 后重发 session/queue 全量、每次 jobs 变更后重发
    session/jobs 全量（全量快照 = 变更 / 重连收敛的单一权威信号）。
  * session/queue 的 items 在 splice 广播点（`session.append` 同步 emit）由
    splice 自身重投影得出（对齐 api-proxy.ts queueItems：observer 看到的
    inbox 是 pre-splice 列表，把 splice 的 start/removedCount/inserted
    toSpliced 上去即得 post-splice 快照）；placement 三态：next-turn 一律
    'queued'，next-step 里 source.kind=='user' 为 'steering'，注入上下文
    （approval 通知、任务完成等）为 'context'。空快照不发（上游 host 在
    live 队列空时省略，客户端保留最后非空值）。
  * host 流无基线：session/created → host/session-added（blank 恒 true，客户端
    在首帧 running:true 上翻位，重连以 session.list 为准），agent/status 翻转
    → host/session-status（maintenance 对事件公开为 idle），agent/error →
    host/agent-error（取 failure.message）。

事件来源：SessionStore 在 owner scope 上派发 session/created|disposed|event，
AgentLoop 在自身 scope 上派发 agent/status|error；两者都沿父链到根 ctx，故
hub 只需在根 ctx 注册监听。帧按目标集合（mux/host）分发到每个连接的队列。

迭代 1 简化（须在 AGENTS.md 标注）：无 rpcId 字段；session/jobs
来自 ctx.jobs 的 on_jobs_changed 回调（owner 恒为 AgentLoop，unowned 作业
不落到任何会话）；无 since 恢复游标（重连 = 重开流 + 重拉 history）。
"""
from __future__ import annotations

import asyncio
from typing import Any

__all__ = ["StreamHub"]

QUEUE_CAPACITY = 1024


class StreamHub:
    """mux/host 下游流中心：每连接一个队列，事件监听按帧类型分发。

    `mux()` / `host()` 是 async 生成器，供传输层逐帧消费。监听器在根 ctx
    懒注册（首个流打开时），dispose 逆序回滚。
    """

    def __init__(self, ctx, api: Any):
        self.ctx = ctx
        self.api = api
        self._mux: dict[asyncio.Queue, None] = {}
        self._host: dict[asyncio.Queue, None] = {}
        self._attached = False
        self._disposers: list[Any] = []

    # ---------- 生命周期 ----------

    def _attach(self) -> None:
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
        try:
            jobs = self.ctx.inject("jobs")
        except KeyError:
            jobs = None
        if jobs is not None and hasattr(jobs, "on_jobs_changed"):
            self._disposers.append(jobs.on_jobs_changed(self._on_jobs_changed))

    def dispose(self) -> None:
        for fn in reversed(self._disposers):
            fn()
        self._disposers.clear()
        self._attached = False

    def _broadcast(self, target: dict, frame: dict) -> None:
        for queue in list(target):
            try:
                queue.put_nowait(frame)
            except asyncio.QueueFull:
                # 慢消费者丢帧：帧流是幂等收敛的，重连重拉 history 即恢复
                pass

    # ---------- 两路生成器 ----------

    async def mux(self):
        """mux 流：baseline（subscribed + 队列/作业快照）后实时转发。"""
        self._attach()
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_CAPACITY)
        self._mux[queue] = None
        try:
            for session_id in sorted(self.api._agents):
                session = self.api.store.get(session_id)
                if session is None:
                    continue
                queue.put_nowait({
                    "type": "session/subscribed",
                    "sessionId": session_id,
                    "lastSeq": session.seq,
                })
                self._queue_snapshot(queue, session_id)
                self._jobs_snapshot(queue, session_id)
            while True:
                frame = await queue.get()
                yield frame
        finally:
            self._mux.pop(queue, None)

    async def host(self):
        """host 流：纯实时帧（无基线）。"""
        self._attach()
        queue: asyncio.Queue = asyncio.Queue(maxsize=QUEUE_CAPACITY)
        self._host[queue] = None
        try:
            while True:
                frame = await queue.get()
                yield frame
        finally:
            self._host.pop(queue, None)

    # ---------- mux 帧 ----------

    def _on_session_event(self, payload: dict) -> None:
        session = payload["session"]
        if session.session_id not in self.api._agents:
            return
        event = payload["event"]
        self._broadcast(self._mux, {
            "type": "session/event", "sessionId": session.session_id, "event": event,
        })
        if event.get("type") == "agent/inbox/spliced":
            self._queue_snapshot_into(session.session_id, event["data"])

    def _queue_snapshot_into(self, session_id: str, splice: dict | None = None) -> None:
        for queue in list(self._mux):
            self._queue_snapshot(queue, session_id, splice)

    def _queue_snapshot(self, queue: asyncio.Queue, session_id: str,
                        splice: dict | None = None) -> None:
        loop = self.api._agents.get(session_id)
        if loop is None:
            return
        # 对齐 api-proxy.ts queueItems：splice 广播点在内存 mutation 之前，
        # 观察到的是 pre-splice 列表，把 splice 重投影上去得到 post-splice 快照。
        def project(target: str) -> list[dict]:
            messages = loop.inbox.next_turn if target == "next-turn" else loop.inbox.next_step
            if splice is not None and splice.get("target") == target:
                start = splice.get("start", 0)
                removed = splice.get("removedCount", 0)
                before = messages[:start]
                after = messages[start + removed:]
                return before + list(splice.get("inserted", [])) + after
            return messages

        items = []
        for message in project("next-turn"):
            items.append({"id": message["id"], "placement": "queued", "message": message})
        for message in project("next-step"):
            placement = ("steering" if message.get("source", {}).get("kind") == "user"
                         else "context")
            items.append({"id": message["id"], "placement": placement, "message": message})
        if not items:
            return
        queue.put_nowait({"type": "session/queue", "sessionId": session_id, "items": items})

    def _on_jobs_changed(self, owner: Any) -> None:
        session_id = getattr(owner, "id", None)
        if not session_id or session_id not in self.api._agents:
            return
        jobs = self._jobs_registry()
        if jobs is None:
            return
        snapshots = [self._job_view(job) for job in jobs.list(owner)]
        frame = {"type": "session/jobs", "sessionId": session_id, "jobs": snapshots}
        for queue in list(self._mux):
            queue.put_nowait(frame)

    def _jobs_snapshot(self, queue: asyncio.Queue, session_id: str) -> None:
        loop = self.api._agents.get(session_id)
        jobs = self._jobs_registry()
        if loop is None or jobs is None:
            return
        snapshots = [self._job_view(job) for job in jobs.list(loop)]
        if not snapshots:
            return
        queue.put_nowait({"type": "session/jobs", "sessionId": session_id, "jobs": snapshots})

    def _jobs_registry(self):
        try:
            return self.ctx.inject("jobs")
        except KeyError:
            return None

    @staticmethod
    def _job_view(snapshot: dict) -> dict:
        return {key: snapshot[key] for key in ("id", "kind", "label", "status", "startedAt")}

    # ---------- host 帧 ----------

    def _on_session_created(self, payload: dict) -> None:
        session = payload["session"]
        frame: dict[str, Any] = {
            "type": "host/session-added", "sessionId": session.session_id, "blank": True,
        }
        meta = session.meta
        if meta.get("parentSession") is not None:
            frame["parentSessionId"] = meta["parentSession"]
        if meta.get("origin") is not None:
            frame["origin"] = meta["origin"]
        if meta.get("cwd") is not None:
            frame["cwd"] = meta["cwd"]
        if meta.get("agentPreset") is not None:
            frame["agentPreset"] = meta["agentPreset"]
        self._broadcast(self._host, frame)

    def _on_session_disposed(self, payload: dict) -> None:
        session = payload["session"]
        self._broadcast(self._host, {
            "type": "host/session-removed", "sessionId": session.session_id,
        })

    def _on_agent_status(self, payload: dict) -> None:
        agent = payload["agent"]
        status = payload.get("status")
        self._broadcast(self._host, {
            "type": "host/session-status", "sessionId": agent.id, "running": status == "running",
        })

    def _on_agent_error(self, payload: dict) -> None:
        agent = payload["agent"]
        error = payload.get("error") or {}
        message = error.get("message") if isinstance(error, dict) else str(error)
        self._broadcast(self._host, {
            "type": "host/agent-error", "sessionId": agent.id,
            "message": message if message else str(error),
        })