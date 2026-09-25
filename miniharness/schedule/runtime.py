"""一个精确 root agent 的一次性定时投影（对齐 packages/schedule/schedule/src/runtime.ts）。

ScheduleRuntime 是进程内可弃置投影：定时器 / idle 等待 / 工具值都是可弃的影子，
唯一持久状态是 owner session 的 ``schedule/change`` v1 流（上游 AGENTS.md 规则）。

载体（与上游差异，登记 verified-diffs §2.35）：
  * mini ``agent.run_maintenance`` 是同步认领（agent.py:547）：仅在 true idle 下
    执行并置 status='maintenance'，非 idle 同步抛错——维护回调本体为纯同步
    （framing/createMessage/followup/append 全部同步），对应上游 Promise 回调。
  * ``wait_for_idle`` 竞速 ``when_idle_async()``（仅 driver 模式）vs stop future；
    driver 未启动时不上 idle 等待（无等价公共异步面，护栏一致性 up）。
  * 事务用 ``run_schedule_transaction``；持久化屏障用 ``flush_schedule_persistence``。
"""
from __future__ import annotations

import asyncio
import time
from typing import Any

from ..core.session.message import create_message, text_block
from .domain import (
    _format_epoch_ms,
    _parse_canonical,
    fold_schedule_events,
    render_every_reminder_batch_framing,
    render_reminder_framing,
    resolve_every_occurrence,
)
from .persistence import flush_schedule_persistence
from .transaction import run_schedule_transaction

__all__ = ["MAX_TIMER_DELAY_MS", "ScheduleRuntime"]

#: Node 定时器不钳位的最大时延（毫秒，上游 runtime.ts:22）。
MAX_TIMER_DELAY_MS = 2_147_483_647


class ScheduleRuntime:
    """一个精确 root agent 的会话内定时投影。"""

    def __init__(self, ctx: Any, agent: Any):
        self.ctx = ctx
        self.agent = agent
        self._timer_handle: asyncio.TimerHandle | None = None
        self._idle_waiter: asyncio.Future | None = None
        self._run: asyncio.Task | None = None
        self._requested = False
        self._stopping = False
        self._faulted = False
        self._disposed = False
        self._stop_event = asyncio.Event()

    # ---------- 公开面 ----------

    def start(self) -> None:
        """开始首次持久化 preflight 与定时派生（上游 start → requestDrive）。"""
        self._request_drive()

    def request_drive(self) -> None:
        """一次提交变化或 idle 转换后重算 live 投影（上游 requestDrive）。"""
        if not self.is_live():
            return
        self._request_drive()

    async def dispose(self) -> None:
        """停止未来工作、取消定时器、等待每个在飞 runtime promise 结算。

        async 载体：可在无运行 loop 的同步拆解路径被 await（此时无在飞任务），
        幂等。每次调用对当前在飞任务集合独立收敛（外部等待者不可取消内部等待，
        对齐上游 disposal promise 复用 allSettled 语义）。
        """
        self.stop()
        pending = [t for t in (self._run, self._idle_waiter) if t is not None]
        for item in pending:
            try:
                await asyncio.wait_for(asyncio.shield(item), None)
            except BaseException:
                pass

    def stop(self) -> None:
        """同步收敛：停止未来工作、取消定时器（不等待在飞任务）。

        供无运行 loop 的同步拆解路径（EffectDisposer._run_sync）使用。
        """
        if self._disposed:
            return
        self._disposed = True
        self._stopping = True
        self._requested = False
        self._clear_timer()
        self._stop_event.set()

    def is_live(self) -> bool:
        """这个精确 root 生命周期是否仍然权威（上游 isLive）。"""
        agents = getattr(self.ctx, "get", None)
        if agents is None:
            return False
        registry = self.ctx.get("agents") if hasattr(self.ctx, "get") else None
        if registry is None:
            return False
        try:
            return registry.get(self.agent.id) is self.agent \
                and any(a is self.agent for a in registry.roots())
        except AttributeError:
            return False

    # ---------- 内部 ----------

    def _request_drive(self) -> None:
        if self._stopping or self._faulted:
            return
        self._clear_timer()
        self._requested = True
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            # 无运行 loop（同步装配/CLI 阶段）：不调度，等首个 loop 内
            # request_drive() 兜住（驱动面本质是异步的，对齐上游 runRequested）。
            return
        if self._run is not None and not self._run.done():
            return
        self._run = asyncio.ensure_future(self._run_requested())

    def _is_runnable(self) -> bool:
        return not self._stopping and self.is_live()

    def _clear_timer(self) -> None:
        if self._timer_handle is not None:
            self._timer_handle.cancel()
            self._timer_handle = None

    def _arm(self, target_ms: int, now_ms: int) -> None:
        """启动一段有界定时器分段；每次唤醒都重新核对墙钟（上游 arm）。"""
        delay = max(0, min(target_ms - now_ms, MAX_TIMER_DELAY_MS))
        loop = asyncio.get_running_loop()

        def _fire() -> None:
            self._timer_handle = None
            self._request_drive()

        self._timer_handle = loop.call_later(delay / 1000.0, _fire)

    def _wait_for_idle(self) -> None:
        """等待一个公开 idle 边界，不持有准入也不造重试定时器（上游 waitForIdle）。"""
        if self._idle_waiter is not None:
            return
        try:
            idle = self.agent.when_idle_async()
        except RuntimeError:
            # 非 driver 模式：无公共异步 idle 面，不等待（已停止时自会退）。
            return
        idle_task: asyncio.Future = asyncio.ensure_future(idle)
        stop_task: asyncio.Future = asyncio.ensure_future(self._stop_event.wait())

        async def _race() -> None:
            try:
                await asyncio.wait([idle_task, stop_task],
                                   return_when=asyncio.FIRST_COMPLETED)
            except BaseException as error:
                if self.is_live():
                    self._warn(
                        f"idle wait failed for agent \"{self.agent.id}\": {self._render(error)}")
            finally:
                self._idle_waiter = None
                for item in (idle_task, stop_task):
                    if not item.done():
                        item.cancel()
                if self.is_live() and not self._stopping:
                    self._request_drive()

        self._idle_waiter = asyncio.ensure_future(_race())

    async def _run_requested(self) -> None:
        """串行排干合并触发（上游 runRequested）。"""
        while self._requested and not self._stopping and not self._faulted:
            self._requested = False
            try:
                await run_schedule_transaction(self.agent, self._drive_once)
            except BaseException as error:
                if self.is_live():
                    self._warn(f"runtime failed for agent \"{self.agent.id}\": {self._render(error)}")
                self._faulted = True
                self._retire()
                return

    def _retire(self) -> None:
        """结算一个精确 run 并尊重其最后微任务窗口内落地的触发（上游 retire）。"""
        if self._run is None:
            return
        self._run = None
        if self._requested and not self._stopping and not self._faulted:
            self._request_drive()

    def _read_folded(self):
        """折叠当前精确 runtime 后缀并容纳 corrupt durable 流（上游 readFolded）。"""
        try:
            return fold_schedule_events(self.agent.session.own_events())
        except BaseException as error:
            self._faulted = True
            self._warn(f"corrupt schedule log for agent \"{self.agent.id}\": {self._render(error)}")
            return None

    def _decide(self, folded, now_ms):
        """容纳一个非法墙钟决策而不永久熔断该 runtime（上游 decide）。"""
        try:
            return self._due_decision(folded, now_ms)
        except BaseException as error:
            self._warn(f"fixed-rate decision failed for agent \"{self.agent.id}\": {self._render(error)}")
            return None

    @staticmethod
    def _due_decision(folded, now_ms):
        """选出一个到期的单次 / 完整固定周期批 / 下次唤醒（上游 dueDecision）。"""
        indexed = [(record, index) for index, record in enumerate(folded["active"])]

        def target(record):
            return ScheduleRuntime._parse_target(record)

        one_shot = [
            entry for entry in indexed
            if entry[0]["kind"] != "every" and target(entry[0]) <= now_ms
        ]
        if one_shot:
            one_shot.sort(key=lambda entry: (target(entry[0]), entry[1]))
            return {"kind": "one-shot", "record": one_shot[0][0]}

        every = [
            entry for entry in indexed
            if entry[0]["kind"] == "every" and target(entry[0]) <= now_ms
        ]
        if every:
            every.sort(key=lambda entry: (target(entry[0]), entry[1]))
            return {
                "kind": "every",
                "acceptedAt": _format_epoch_ms(now_ms),
                "reminders": [
                    {
                        "record": record,
                        "occurrenceAt": resolve_every_occurrence(record, now_ms)["occurrenceAt"],
                    }
                    for record, _ in every
                ],
            }

        future = [target(record) for record in folded["active"] if target(record) > now_ms]
        if not future:
            return {"kind": "wait"}
        return {"kind": "wait", "target": min(future)}

    async def _drive_once(self) -> None:
        """preflight → fold → arm 或派发下一次单次/固定周期批（上游 driveOnce）。"""
        self._clear_timer()
        if not self._is_runnable():
            return
        try:
            flush_schedule_persistence(self.ctx, self.agent.session)
        except BaseException as error:
            if self.is_live():
                self._warn(f"preflight failed for agent \"{self.agent.id}\": {self._render(error)}")
            return
        if not self._is_runnable():
            return

        folded = self._read_folded()
        if folded is None:
            return
        wake_now = _now_ms()
        wake_decision = self._decide(folded, wake_now)
        if wake_decision is None:
            return
        if wake_decision["kind"] == "wait":
            if wake_decision.get("target") is not None:
                self._arm(wake_decision["target"], wake_now)
            return

        try:
            claimed = self.agent.run_maintenance(lambda: self._maintenance(wake_decision))
        except BaseException:
            # 同步拒绝（另一 agent 活动拥有 idle 阶段）→ 等 idle（上游 catch _busy）
            if self.is_live():
                self._wait_for_idle()
            return
        if not claimed:
            return

        try:
            flush_schedule_persistence(self.ctx, self.agent.session)
        except BaseException as error:
            if self.is_live():
                self._warn(f"dispatch barrier failed for agent \"{self.agent.id}\": {self._render(error)}")
            return
        if self._is_runnable():
            self._request_drive()

    def _maintenance(self, wake_decision):
        """维护回调体（同步；上游 runMaintenance(async) 的 mini 同步载体）。"""
        if not self._is_runnable():
            return False
        claimed = self._read_folded()
        if claimed is None:
            return False
        decision_now = _now_ms()
        decision = self._decide(claimed, decision_now)
        if decision is None:
            return False
        if decision["kind"] == "wait":
            if decision.get("target") is not None:
                self._arm(decision["target"], decision_now)
            return False
        try:
            text = (render_reminder_framing(decision["record"])
                    if decision["kind"] == "one-shot"
                    else render_every_reminder_batch_framing(decision["reminders"]))
            self.agent.followup(
                create_message("user", [text_block(text)],
                               {"kind": "schedule"})
            )
        except BaseException as error:
            if self.is_live():
                self._warn(f"framing or followup failed for agent \"{self.agent.id}\": {self._render(error)}")
            return False
        try:
            if decision["kind"] == "one-shot":
                self.agent.session.append("schedule/change", {
                    "version": 1,
                    "operation": "dispatch",
                    "id": decision["record"]["id"],
                })
            else:
                for reminder in decision["reminders"]:
                    self.agent.session.append("schedule/change", {
                        "version": 1,
                        "operation": "dispatch",
                        "id": reminder["record"]["id"],
                        "acceptedAt": decision["acceptedAt"],
                    })
        except BaseException as error:
            self._faulted = True
            self._clear_timer()
            self._warn(f"dispatch append failed for agent \"{self.agent.id}\": {self._render(error)}")
            return False
        return True

    def _warn(self, message: str) -> None:
        logger = getattr(self.ctx, "logger", None)
        if logger is not None and hasattr(logger, "warn"):
            try:
                logger.warn(f"schedule: {message}")
            except BaseException:
                pass

    @staticmethod
    def _render(value: BaseException) -> str:
        return getattr(value, "message", None) or str(value)

    @staticmethod
    def _parse_target(record) -> int:
        return int(_parse_canonical(record["scheduledAt"]).timestamp() * 1000)


def _now_ms() -> int:
    return int(time.time() * 1000)