"""Schedule 选装 seam：Install 装配获取 + 生命周期接线 + 公共导出。

用法（产品化组装）：
    install_schedule(ctx)            # 只挂未来新增 live root agent 的 Schedule
    disposer = install_schedule(ctx) # 幂等；返回生命周期 disposer（可调用可 await）

对齐上游（index.ts:43-85）：
  * ``ctx.effect('schedule.lifecycle()')`` 挂 agent/created：仅 root、仅未来、重复
    跳过；每 owner 以 ``agent.ctx.effect('schedule.runtime()')`` 归属（工具 + status
    监听 + runtime.start + async cleanup），随 agent scope 拆解自动拆除。
  * status idle 且 session 快照含 ``schedule/change`` → requestDrive；cleanup 序 =
    stopStatus → disposeTools → runtime.dispose()，最后按 identity 摘除 runtimes 条目。
  * 全局拆解：stopping → stopCreated → 所有 owner cleanup allSettled。

已登记载体差异（verified-diffs §2.35）：不提供 sessionProjections 子投影注册
（决策：schedule 的 fold 由自身正确性保证，无需额外投影面）；inject 硬依赖收紧为
``ctx.agents`` + ``ctx.sessions`` 装配时校验。

导出：
  域函数族（决策 6 同步 push 现有导出）由 domain.py 提供，此处仅转发。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from .domain import (
    MIN_EVERY_INTERVAL_SECONDS,
    SCHEDULE_CHANGE_VERSION,
    ScheduleInputError,
    ScheduleLogError,
    allocate_schedule_id,
    create_after_schedule_record,
    create_at_schedule_record,
    create_every_schedule_record,
    decode_schedule_change,
    fold_schedule_events,
    render_every_reminder_batch_framing,
    render_reminder_framing,
    resolve_every_occurrence,
    schedule_view,
)
from .types import ScheduleId
from .runtime import ScheduleRuntime
from .tools import register_schedule_tools

__all__ = [
    "SCHEDULE_CHANGE_VERSION",
    "MIN_EVERY_INTERVAL_SECONDS",
    "ScheduleId",
    "ScheduleInputError",
    "ScheduleLogError",
    "allocate_schedule_id",
    "create_after_schedule_record",
    "create_at_schedule_record",
    "create_every_schedule_record",
    "decode_schedule_change",
    "fold_schedule_events",
    "render_every_reminder_batch_framing",
    "render_reminder_framing",
    "resolve_every_occurrence",
    "schedule_view",
    "ScheduleRuntime",
    "register_schedule_tools",
    "install_schedule",
]

#: 幂等标记（对齐 install_agents 的 ctx._miniharness_agents_installed 模式）。
_INSTALLED_ATTR = "_miniharness_schedule_installed"


def _retire_runtime(runtime: Any) -> Any:
    """同步拆解路径上的运行时收敛：无运行 loop 时仅 stop，有 loop 时 await dispose。

    EffectDisposer 的同步结算（无运行 loop）不能 await——此时没有在飞任务，
    ``stop()`` 已完整收敛；有 loop 时返回 coroutine 交给 disposer 的异步路径
    等待在飞事务/任务（上游 cleanup async → await runtime.dispose()）。
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        runtime.stop()
        return None
    return runtime.dispose()


def _retire_cleanup(cleanup: Any) -> Any:
    """逐 owner 收敛：异常收编记日志（对齐上游 allSettled 容器），返回 coroutine。"""
    try:
        return cleanup()
    except BaseException as error:
        _log_teardown_error(error)
        return None


def _log_teardown_error(error: BaseException) -> None:
    logging.getLogger(__name__).warning("schedule: owner cleanup failed: %s", error)


def _teardown_async(pending: list) -> Any:
    """全 owner async cleanup 的 allSettled 收敛（对齐上游 Promise.allSettled）。"""
    async def _join() -> None:
        awaitables = [item for item in pending if item is not None]
        results = await asyncio.gather(
            *(asyncio.ensure_future(asyncio.shield(item)) for item in awaitables),
            return_exceptions=True)
        for item in results:
            if isinstance(item, BaseException):
                _log_teardown_error(item)

    return _join()


def install_schedule(ctx: Any) -> Callable[[], Any]:
    """装配 Schedule：只挂未来 live root agent 的运行时与作用域工具。

    幂等：同一 ctx 二次调用返回既有 disposer 不重复挂接。要求 ctx 已提供
    ``agents`` 与 ``sessions``（缺服务 fail loud，对齐上游 inject 声明）。

    @returns 全局生命周期 disposer（对齐 apply() 的 ctx.effect cleanup 形态）——
        调用即停止挂接并 allSettled 所有 owner cleanup；无运行 loop 时同步执行。
    """
    existing = getattr(ctx, _INSTALLED_ATTR, None)
    if existing is not None:
        return existing

    agents = ctx.get("agents")
    sessions = ctx.get("sessions")
    if agents is None or sessions is None:
        raise RuntimeError("install_schedule requires ctx.agents and ctx.sessions")

    runtimes: dict[Any, Any] = {}
    stopping: list[bool] = [False]

    def on_agent_status(payload: dict) -> None:
        agent = payload.get("agent")
        if agent is None or stopping[0]:
            return
        if payload.get("status") != "idle":
            return
        holder = runtimes.get(agent)
        if holder is None:
            return
        runtime = holder["runtime"]
        try:
            has_change = any(
                event.get("type") == "schedule/change"
                for event in agent.session.snapshot_events()
            )
        except BaseException:
            return
        if has_change:
            runtime.request_drive()

    def on_agent_created(payload: dict) -> None:
        agent = payload.get("agent")
        if agent is None or stopping[0]:
            return
        if agent in runtimes:
            return
        try:
            if not any(root is agent for root in agents.roots()):
                return
        except BaseException:
            return
        runtime = ScheduleRuntime(ctx, agent)
        holder = {"runtime": runtime}

        def build_owner() -> Callable[[], Any]:
            dispose_tools = register_schedule_tools(
                ctx, agent, lambda: runtime.request_drive())
            status_listener = agent.ctx.on("agent/status", on_agent_status)
            runtime.start()

            def cleanup() -> Any:
                status_listener()
                dispose_tools()
                retired = _retire_runtime(runtime)
                if runtimes.get(agent) is holder:
                    runtimes.pop(agent, None)
                return retired

            holder["cleanup"] = cleanup
            return cleanup

        agent.ctx.effect(build_owner, "schedule.runtime()")
        runtimes[agent] = holder

    def build_lifecycle() -> Callable[[], Any]:
        stop_created = ctx.on("agent/created", on_agent_created)

        def teardown() -> Any:
            stopping[0] = True
            stop_created()
            retired = [
                _retire_cleanup(cleanup)
                for cleanup in list(runtimes.values())
            ]
            runtimes.clear()
            pending = [item for item in retired if item is not None]
            if pending:
                return _teardown_async(pending)
            return None

        return teardown

    disposer = ctx.effect(build_lifecycle, "schedule.lifecycle()")
    setattr(ctx, _INSTALLED_ATTR, disposer)
    return disposer