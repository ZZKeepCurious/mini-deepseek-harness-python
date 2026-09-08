"""一次 Team 变更等待基础（对齐上游 activity.ts）。

上游 per-TeamId waiter 集合，resolve 后即消灭（一次性）；timeoutMs 约束为
10_000..3_600_000，越界拒绝。mini 同步载体 wait() 在 caller 轮询窗口内：
activity 已发生 → 立返非超时；否则线程 Event 等待（其他 driver 线程的
onCommit 可唤醒）；无 driver 的单线程测试应用退化为确定性 timeout。载体差异
已在 verified-diffs §2.29 登记。
"""

from __future__ import annotations

import threading
import time

from .error import TeamError

__all__ = ["TeamActivity", "TeamWaitResult"]


class TeamWaitResult:
    def __init__(self, timed_out: bool):
        self.timed_out = timed_out

    def to_dict(self) -> dict:
        return {"timedOut": self.timed_out}


MIN_RANGE = 10_000
MAX_RANGE = 3_600_000
_WAIT_STEP_MS = 50


class TeamActivity:
    """一次性 waiter 簿记：waiter 只保留到其第一次唤醒（对齐上游 resolve=消灭）。"""

    def __init__(self) -> None:
        self._waiters: dict[str, list[threading.Event]] = {}

    def register(self, team_id: str, timeout_ms: int) -> threading.Event:
        if not isinstance(timeout_ms, int) or isinstance(timeout_ms, bool) \
                or not (MIN_RANGE <= timeout_ms <= MAX_RANGE):
            raise TeamError(
                f"timeout must be between {MIN_RANGE} and {MAX_RANGE} ms",
                "TEAM_INVALID_ARGUMENT",
            )
        event = threading.Event()
        self._waiters.setdefault(team_id, []).append(event)
        return event

    def wake(self, team_id: str) -> None:
        """唤醒该 Team 的全部 waiter 并清空集合（resolve + 消灭）。"""
        waiters = self._waiters.pop(team_id, None)
        if waiters is None:
            return
        for event in waiters:
            event.set()

    def wait_for(self, team_id: str, timeout_ms: int) -> TeamWaitResult:
        """等待一次 Team 活动或超时；总是收起 waiters（一次性语义）。"""
        event = self.register(team_id, timeout_ms)
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._discard(team_id, event)
                return TeamWaitResult(timed_out=True)
            if event.wait(timeout=(min(_WAIT_STEP_MS / 1000, remaining))):
                return TeamWaitResult(timed_out=False)

    def _discard(self, team_id: str, event: threading.Event) -> None:
        waiters = self._waiters.get(team_id)
        if waiters is None:
            return
        try:
            waiters.remove(event)
        except ValueError:
            return
        if not waiters:
            self._waiters.pop(team_id, None)