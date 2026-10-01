"""单一 Host 定时器把到期任务投递回原会话（对齐 runtime.ts）。

驱动一回：扫描到期 active 任务 → 同一会话的全部到期周期任务并成一批 →
经解析器恢复原会话 → ``agent.followup`` 入队 → 等待 ``sessions.flush`` 持久化
屏障 → 提交 status/target/receipt/history（单次→inactive；周期→推进或耗尽）。
合并的 ``request_drive`` 与 ``dispose`` 排干在飞投递。
"""
from __future__ import annotations

import asyncio
from typing import Any, Callable

from ..core.session.message import create_message, text_block
from .domain import (
    _epoch_from_canonical,
    _format_epoch_ms,
    is_recurring_schedule_record,
    now_ms,
    render_recurring_reminder_batch_framing,
    render_reminder_framing,
    resolve_recurring_occurrence,
)
from .delivery_history import append_delivery

__all__ = ["MAX_TIMER_DELAY_MS", "ScheduleRuntime"]

#: Node 定时器不钳位的最大时延（毫秒，上游 runtime.ts MAX_TIMER_DELAY_MS）。
MAX_TIMER_DELAY_MS = 2_147_483_647


def _epoch_of(task: dict) -> int:
    return _epoch_from_canonical(task["record"]["scheduledAt"])


class ScheduleRuntime:
    """跨全部存储任务至多一个定时器；投递与管理写共享串行化。"""

    def __init__(self, ctx: Any, tasks: Callable[[], list],
                 transact: Callable[[Callable[[], Any]], Any],
                 commit: Callable[[dict], Any], retention: dict,
                 resolve_session: Callable[[str], Any], logger: Any = None) -> None:
        self.ctx = ctx
        self._tasks = tasks
        self._transact = transact
        self._commit = commit
        self._retention = retention
        self._resolve_session = resolve_session
        self._logger = logger
        self._timer: asyncio.TimerHandle | None = None
        self._running: asyncio.Task | None = None
        self._stopping = False
        self._requested = False

    # ---------- 触发 / 拆解 ----------

    def request_drive(self) -> None:
        """启动或合并一次重算；投递失败记日志、拒绝的投递不自动重试。"""
        if self._stopping:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return  # 无运行 loop：等首个 loop 内 request_drive 兜住
        self._requested = True
        self._clear_timer()
        if self._running is not None and not self._running.done():
            return
        self._running = loop.create_task(self._run_requested())

    async def dispose(self) -> None:
        self._stopping = True
        self._clear_timer()
        running = self._running
        if running is not None:
            try:
                await asyncio.shield(running)
            except BaseException:  # noqa: BLE001 - teardown 只等静默
                pass

    # ---------- 内部 ----------

    def _clear_timer(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    def _warn(self, message: str) -> None:
        logger = self._logger
        if logger is not None and hasattr(logger, "warn"):
            logger.warn(f"schedule: {message}")

    async def _run_requested(self) -> None:
        try:
            while self._requested and not self._stopping:
                self._requested = False
                await self._transact(self.drive)
        except BaseException as error:  # noqa: BLE001 - 执行失败记日志
            self._warn(f"dispatch stopped: {error}")
        finally:
            self._running = None
            if self._requested and not self._stopping:
                self.request_drive()

    def _arm(self, target_ms: int, now_ms: int) -> None:
        delay = max(0, min(target_ms - now_ms, MAX_TIMER_DELAY_MS))
        loop = asyncio.get_running_loop()

        def _fire() -> None:
            self._timer = None
            self.request_drive()

        self._timer = loop.call_later(delay / 1000.0, _fire)

    async def _flush(self, agent: Any) -> bool:
        sessions = self.ctx.get("sessions")
        if sessions is None:
            return False
        result = sessions.flush(agent.session)
        if asyncio.iscoroutine(result) or isinstance(result, asyncio.Future):
            result = await result
        return bool(result)

    async def drive(self) -> None:
        """扫描并投递全部到期任务，随后为最近的未来任务重新武装定时器。"""
        self._clear_timer()
        failed: set = set()
        handled: set = set()
        scan_now = now_ms()
        due = [task for task in self._tasks()
               if task["status"] == "active" and _epoch_of(task) <= scan_now]
        for task in due:
            if self._stopping:
                return
            task_id = task["record"]["id"]
            if task_id in handled:
                continue
            if is_recurring_schedule_record(task["record"]):
                group = [candidate for candidate in due
                         if candidate["sessionId"] == task["sessionId"]
                         and is_recurring_schedule_record(candidate["record"])]
            else:
                group = [task]
            for member in group:
                handled.add(member["record"]["id"])
            admitted = group
            committed: set = set()
            try:
                agent = await self._resolve_session(task["sessionId"])
                if self._stopping:
                    return
                now = now_ms()
                admitted = [member for member in group if _epoch_of(member) <= now]
                if not admitted:
                    continue
                recurring = [member for member in admitted
                             if is_recurring_schedule_record(member["record"])]
                occurrences = [
                    (member, resolve_recurring_occurrence(member["record"], now))
                    for member in recurring
                ]
                if is_recurring_schedule_record(task["record"]):
                    text = render_recurring_reminder_batch_framing([
                        {"record": member["record"], "occurrenceAt": occurrence["occurrenceAt"]}
                        for member, occurrence in occurrences
                    ])
                else:
                    text = render_reminder_framing(task["record"])
                message = create_message("user", [text_block(text)], {"kind": "schedule"})
                agent.followup(message)
                flushed = await self._flush(agent)
                if not flushed:
                    raise RuntimeError("Session persistence did not acknowledge the reminder")
                delivered_at = _format_epoch_ms(now_ms())
                if not is_recurring_schedule_record(task["record"]):
                    await self._commit({
                        **task, "status": "inactive",
                        **append_delivery(task, {
                            "scheduledAt": task["record"]["scheduledAt"],
                            "deliveredAt": delivered_at,
                            "messageId": message["id"],
                        }, self._retention),
                    })
                    committed.add(task["record"]["id"])
                for member, occurrence in occurrences:
                    next_at = occurrence.get("nextScheduledAt")
                    await self._commit({
                        **member,
                        "record": {**member["record"],
                                   "scheduledAt": next_at or occurrence["occurrenceAt"]},
                        "status": "active" if next_at is not None else "inactive",
                        **append_delivery(member, {
                            "scheduledAt": occurrence["occurrenceAt"],
                            "deliveredAt": delivered_at,
                            "messageId": message["id"],
                        }, self._retention),
                    })
                    committed.add(member["record"]["id"])
            except BaseException as error:  # noqa: BLE001 - 单组失败不阻断其余
                pending = [member for member in admitted
                           if member["record"]["id"] not in committed]
                failed_at = now_ms()
                for member in pending:
                    if _epoch_of(member) <= failed_at:
                        failed.add(member["record"]["id"])
                ids = [member["record"]["id"] for member in pending]
                self._warn(
                    f"reminders {ids!r} were not acknowledged: {error}")
        if self._stopping:
            return
        targets = [
            _epoch_of(task) for task in self._tasks()
            if task["status"] == "active" and task["record"]["id"] not in failed
        ]
        if targets:
            self._arm(min(targets), now_ms())
