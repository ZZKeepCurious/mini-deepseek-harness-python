"""per-Lead 串行 Team 事务 + 已提交事件发布（对齐上游 journal.ts）。

上游对每个 Lead 用 Promise tail 串行 read-check-append 异步事务；mini 同步载体改
per-Lead 可重入锁：操作同步执行到提交，嵌套 transact（send → dispatch →
checkpointDelivered → markDelivered）经 RLock 重入——顺序不变、无死锁（载体
一致性已在 verified-diffs §2.29 登记）。
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from ...core.agent_loop.agent import AgentLoop
from .projection import TeamState, fold_team_state, is_team_event

__all__ = ["TeamJournal"]

MUTABLE_TEAM_EVENT_TYPES = frozenset({
    "team/member", "team/task", "team/message/queued", "team/message/delivered",
})


class TeamJournal:
    """Owns per-Lead transaction order and committed Team event publication."""

    def __init__(self, sessions: Any, on_commit: Callable[[AgentLoop], None]):
        self._sessions = sessions
        self._on_commit = on_commit
        self._locks: dict[str, threading.RLock] = {}

    def state(self, root: AgentLoop) -> TeamState:
        """读一个 exact live Lead 的权威 Team 状态：对 Lead 会话自有团队事件折叠。"""
        events = [e for e in root.session.own_events() if is_team_event(e)]
        return fold_team_state(events, root.id)

    def transact(self, root_id: str, operation: Callable[[], Any]) -> Any:
        """串行化一个 Lead 的完整 read-check-append 操作（同步载体，RLock 重入）。"""
        lock = self._locks.setdefault(root_id, threading.RLock())
        with lock:
            return operation()

    def append_and_flush(self, root: AgentLoop, type_: str, data: dict) -> None:
        """append 一条 root 属主 Team 事件并落盘，然后在提交后同步发布。"""
        if type_ not in MUTABLE_TEAM_EVENT_TYPES:
            raise ValueError(f"cannot append non-Team event type {type_!r}")
        root.session.append(type_, data)
        self._sessions.flush(root.session)
        self._on_commit(root)