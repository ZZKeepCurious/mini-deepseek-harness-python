"""Durable Team mailbox admission, target-local dispatch, acknowledgement, recovery
（对齐上游 mailbox.ts）。

mini 载体差异（verified-diffs §2.29）：
  * 同步编排：send 的 transact 内联完成 enqueue + dispatch 至 accepted/queued；
    sync 载体目标 steering 即内联泵完回合，checkpoint 首轮即收敛；driver 载体
    走 session/event 观察者补 ack（observe_session_event）。
  * 每个 target 一把串行锁统一 dispatch 顺序（对齐 dispatchTails 队列语义）；
    无 detached Promise 簿记（同步完成），lifecycle.pending 只留观察钩子。
"""

from __future__ import annotations

import json
import threading
from typing import Any

from ...core.agent_loop.agent import AgentLoop
from ...core.session import create_message
from .error import TeamError, error_message
from .journal import TeamJournal
from .lifecycle import TeamRuntimeLifecycle
from .roster import (
    TeamRoster,
    message_accepted,
    read_persisted_session,
    resolve_active_member,
)
from .types import (
    TeamMessageSnapshot,
    SendTeamMessageRequest,
    SendTeamMessageResult,
    generate_message_id,
)

__all__ = ["TeamMailbox"]


def _team_source(root: AgentLoop, message: TeamMessageSnapshot) -> dict:
    return {
        "kind": "team-message",
        "teamId": root.id,
        "messageId": message.id,
        "senderId": message.senderId,
        "senderName": message.senderName,
    }


def delivery_content(message: TeamMessageSnapshot) -> list[dict]:
    """投递帧：稳定 sender 文本前缀 + 原内容（对齐 upstream deliveryContent）。"""
    return [
        {"type": "text", "text": f"Team message {message.id} from {message.senderName}:"},
        *[dict(x) for x in message.content],
    ]


class TeamMailbox:
    def __init__(
        self,
        agents: Any,
        sessions: Any,
        persistence: Any,
        manager: Any,
        journal: TeamJournal,
        roster: TeamRoster,
        lifecycle: TeamRuntimeLifecycle,
        max_pending_messages_per_member: int,
        max_message_bytes: int,
    ):
        self._agents = agents
        self._sessions = sessions
        self._persistence = persistence
        self._manager = manager
        self._journal = journal
        self._roster = roster
        self._lifecycle = lifecycle
        self._max_pending = max_pending_messages_per_member
        self._max_message_bytes = max_message_bytes
        self._dispatch_locks: dict[str, threading.Lock] = {}
        self._in_flight: set[str] = set()

    # ---------- 入队 + 即时投递 ----------

    def send(self, caller: AgentLoop, request: SendTeamMessageRequest) -> SendTeamMessageResult:
        membership = self._roster.membership(caller)
        self._lifecycle.assert_admitting()
        root = membership.root
        content = [dict(x) for x in request.content]
        # 入队与投递分开持锁：入队占用 Lead 日志锁（快、原子），投递在锁外——
        # 投递阻塞在 steer（等 target 回合完成），而回合完成回调
        # observe_session_event 会从后台线程再进 _mark_delivered（同一 Lead 锁）。
        # 锁内投递 = 跨线程 RLock 不重入 → 死锁（见 verified-diffs §2.29）。
        message = self._journal.transact(
            root.id,
            lambda: self._enqueue(root, caller, membership.name, request.target, content),
        )
        if self._lifecycle.disposed:
            return SendTeamMessageResult(messageId=message.id, status="queued")
        accepted = self._try_dispatch(root, message)
        return SendTeamMessageResult(
            messageId=message.id, status="accepted" if accepted else "queued"
        )

    def _enqueue(
        self, root: AgentLoop, caller: AgentLoop, sender_name: str,
        target_name: str, content: list[dict],
    ) -> TeamMessageSnapshot:
        self._lifecycle.assert_admitting()
        state = self._journal.state(root)
        target = resolve_active_member(root, state, target_name)
        if target["id"] == caller.id:
            raise TeamError("a Team member cannot message itself", "TEAM_SELF_MESSAGE")
        pending_for_target = sum(
            1 for m in state.messages
            if m.targetId == target["id"] and m.id not in state.delivered
        )
        if pending_for_target >= self._max_pending:
            raise TeamError(
                f'teammate "{target["name"]}" has {pending_for_target} pending messages',
                "TEAM_MAILBOX_FULL",
            )
        message = TeamMessageSnapshot(
            id=generate_message_id(),
            senderId=caller.id,
            senderName=sender_name,
            targetId=target["id"],
            content=tuple(content),
        )
        frame = delivery_content(message)
        if len(json.dumps(frame, ensure_ascii=False)) > self._max_message_bytes:
            raise TeamError(
                f"team message exceeds {self._max_message_bytes} bytes",
                "TEAM_MESSAGE_TOO_LARGE",
            )
        self._journal.append_and_flush(root, "team/message/queued", {
            "version": 2, "teamId": root.id, "message": message.to_dict(),
        })
        return message

    # ---------- 观察者：target 侧 durable 回执 ----------

    def observe_session_event(self, session: Any, event: dict) -> None:
        """观察 target Session 的 user/message 团队来源回执并签收 Lead 日志。"""
        if self._lifecycle.disposed or event.get("type") != "user/message":
            return
        data = event.get("data") or {}
        source = data.get("source") or {}
        if source.get("kind") != "team-message":
            return
        try:
            root = self._agents.get(source.get("teamId"))
            if root is not None:
                self._checkpoint_delivered(root, session, source.get("messageId"))
        except Exception as error:  # noqa: BLE001 - ack 失败只保暖日志
            logger = getattr(self._agents, "logger", None)
            if getattr(logger, "warn", None) is not None:
                logger.warn(
                    f'Team message "{source.get("messageId")}" acknowledgement failed: '
                    f"{error_message(error)}"
                )

    # ---------- 恢复：一位成员启动后重试相关 pending ----------

    def recover_for(self, agent: AgentLoop) -> None:
        membership = self._roster.try_membership(agent)
        if membership is None:
            return
        state = self._journal.state(membership.root)
        messages = [
            m for m in state.messages
            if m.id not in state.delivered
            and (membership.role == "lead" or m.targetId == agent.id)
        ]
        for message in messages:
            self._lifecycle.assert_admitting()
            self._try_dispatch(membership.root, message)

    # ---------- 投递原语 ----------

    def _try_dispatch(self, root: AgentLoop, message: TeamMessageSnapshot) -> bool:
        if self._lifecycle.disposed:
            return False
        if message.id in self._in_flight:
            return False
        self._in_flight.add(message.id)
        try:
            return self._serialize_dispatch(message, lambda: self._dispatch_once(root, message))
        finally:
            self._in_flight.discard(message.id)

    def _serialize_dispatch(self, message: TeamMessageSnapshot, operation) -> bool:
        """按 queue 顺序串行化一个 durable target 的投递准入。"""
        target_id = message.targetId
        lock = self._dispatch_locks.setdefault(target_id, threading.Lock())
        with lock:
            return operation()

    def _dispatch_once(self, root: AgentLoop, message: TeamMessageSnapshot) -> bool:
        try:
            target = self._agents.get(message.targetId)
            content = delivery_content(message)
            source = _team_source(root, message)
            if target is not None:
                if self._target_recorded(target.session, message.id):
                    return self._checkpoint_delivered(root, target.session, message.id)
                if message.targetId == root.id:
                    root.steer(create_message("user", content, source))
                    return self._checkpoint_delivered(root, root.session, message.id)
                self._manager.steer_host_subagent(message.targetId, content, source)
                return self._checkpoint_delivered(root, target.session, message.id)
            recorded = self._persisted_target_recorded(message.targetId, message.id)
            if recorded is None:
                return False
            if recorded:
                self._mark_delivered(root, message.id, message.targetId)
                return True
            self._manager.steer_host_subagent(message.targetId, content, source)
            return True
        except Exception as error:  # noqa: BLE001 - 一次投递失败保持 queued
            logger = getattr(self._agents, "logger", None)
            if getattr(logger, "warn", None) is not None:
                logger.warn(
                    f'team message "{message.id}" remains queued: {error_message(error)}'
                )
            return False

    def _checkpoint_delivered(
        self, root: AgentLoop, target_session: Any, message_id: str
    ) -> bool:
        """先 flush 一个 live target 回执，再在 Lead 记 delivered 边。"""
        self._sessions.flush(target_session)
        if not self._target_recorded(target_session, message_id):
            return False
        self._mark_delivered(root, message_id, target_session.session_id)
        return True

    def _mark_delivered(
        self, root: AgentLoop, message_id: str, target_id: str
    ) -> None:
        self._journal.transact(root.id, lambda: self._record_delivered(
            root, message_id, target_id))

    def _record_delivered(
        self, root: AgentLoop, message_id: str, target_id: str
    ) -> None:
        state = self._journal.state(root)
        if message_id in state.delivered:
            return
        queued = next((m for m in state.messages if m.id == message_id), None)
        if queued is None or queued.targetId != target_id:
            return
        self._journal.append_and_flush(root, "team/message/delivered", {
            "version": 2, "teamId": root.id,
            "messageId": message_id, "targetId": target_id,
        })

    def _target_recorded(self, session: Any, message_id: str) -> bool:
        suffix = session.snapshot_events(session.inherited_event_count)
        return message_accepted(
            list(suffix),
            lambda m: (m.get("source") or {}).get("kind") == "team-message"
            and (m.get("source") or {}).get("messageId") == message_id,
        )

    def _persisted_target_recorded(
        self, target_id: str, message_id: str
    ) -> bool | None:
        """冷目标 durable 读；不确定（不可读）保持 queued（对齐上游）。"""
        try:
            stored = read_persisted_session(self._persistence, target_id)
            suffix = stored["events"][stored["inheritedEventCount"]:]
            return message_accepted(
                suffix,
                lambda m: (m.get("source") or {}).get("kind") == "team-message"
                and (m.get("source") or {}).get("messageId") == message_id,
            )
        except Exception as error:  # noqa: BLE001 - 读取失败保持 queued
            logger = getattr(self._agents, "logger", None)
            if getattr(logger, "warn", None) is not None:
                logger.warn(
                    f'cannot read Team message target "{target_id}": {error_message(error)}'
                )
            return None