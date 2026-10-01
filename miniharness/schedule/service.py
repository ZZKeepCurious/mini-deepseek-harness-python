"""Host 全局 Schedule 管理服务（对齐 packages/schedule/schedule/src/index.ts）。

共享管理面：读、删、时序编辑都不激活会话。权威存储是 `schedule` storage-domain
表（见 storage.py）。写操作经一条 asyncio FIFO 链串行化，冲突只回一个结果；
提交后发 `schedule/changed` 并请求运行时重算。旧 `schedule/change` 会话事件仅
用于历史告警（只读）。
"""
from __future__ import annotations

import asyncio
import inspect
import uuid
from typing import Any

from ..core.scope import Context, Service
from . import domain as _domain
from .delivery_history import append_delivery, delivery_history_page
from .runtime import ScheduleRuntime
from .storage import schedule_domain
from .types import (
    DEFAULT_DELIVERY_HISTORY_DAYS,
    DEFAULT_DELIVERY_HISTORY_RECORDS,
)
from .update import resolve_schedule_update

__all__ = ["ScheduleService", "install_schedule"]


class ScheduleService(Service):
    """共享 Schedule 管理服务（`ctx.schedule`）。"""

    provide = "schedule"

    def __init__(self, ctx: Context, config: dict | None = None):
        config = dict(config or {})
        days = config.get("deliveryHistoryDays", DEFAULT_DELIVERY_HISTORY_DAYS)
        records = config.get("deliveryHistoryRecords", DEFAULT_DELIVERY_HISTORY_RECORDS)
        if not isinstance(days, int) or isinstance(days, bool) or days < 1 or days > 3650:
            raise ValueError(
                "schedule: deliveryHistoryDays must be an integer from 1 through 3650")
        if (not isinstance(records, int) or isinstance(records, bool)
                or records < 1 or records > 10_000):
            raise ValueError(
                "schedule: deliveryHistoryRecords must be an integer from 1 through 10000")
        self.retention = {"days": days, "records": records}
        self._stopping = False
        self._disposed = False
        self._chain: asyncio.Future | None = None
        self._attached: set = set()
        self._domain = None
        self._table = None
        self._runtime: ScheduleRuntime | None = None
        self._opening = None
        super().__init__(ctx, "schedule")
        self._logger = self.ctx.logger
        self._install_lifecycle()

    # ---------- 装配 ----------

    async def _ensure_domain(self) -> None:
        """惰性打开权威 storage-domain 表（首次异步使用时）。"""
        if self._table is not None:
            return
        if self._opening is None:
            self._opening = asyncio.ensure_future(self._open_domain())
        await self._opening
        if self._table is None:
            raise RuntimeError("schedule: the storage domain could not be opened")

    async def _open_domain(self) -> None:
        storage = self.ctx.get("storage")
        if storage is None:
            raise RuntimeError(
                "schedule: the storage service is required (install_storage)")
        domain = await storage.domain.open(schedule_domain)
        table = domain.table("tasks")
        for key, task in table.entries():
            if key != task["record"]["id"]:
                raise RuntimeError(
                    f'schedule: stored task key "{key}" differs from record id '
                    f'"{task["record"]["id"]}"')
        self._domain = domain
        self._table = table
        self._runtime = ScheduleRuntime(
            self.ctx, self._tasks_snapshot, self._serialize, self._commit_task,
            self.retention, self._resolve_agent, logger=self._logger)
        self._runtime.request_drive()

    def _install_lifecycle(self) -> None:
        self.ctx.on("session/created", self._on_session_created, global_=True)
        self.ctx.on("agent/created", self._on_agent_created)
        agents = self.ctx.get("agents")
        if agents is not None:
            for agent in agents.roots():
                self._attach(agent)
        from .archive_admission import install_schedule_archive_admission
        install_schedule_archive_admission(self.ctx, self)
        self.ctx.effect(lambda: self.dispose, "schedule.lifecycle()")

    def _tasks_snapshot(self) -> list:
        return [task for _key, task in self._table.entries()]

    def _on_agent_created(self, payload: dict) -> None:
        agent = payload.get("agent") if isinstance(payload, dict) else None
        if agent is not None:
            self._attach(agent)

    def _attach(self, agent: Any) -> None:
        if self._stopping or agent in self._attached:
            return
        from .tools import register_schedule_tools

        agent.ctx.effect(
            lambda: register_schedule_tools(self, agent), "schedule.tools()")
        self._attached.add(agent)

    def _on_session_created(self, payload: dict) -> None:
        session = payload.get("session") if isinstance(payload, dict) else None
        if session is None:
            return
        try:
            active = len(_domain.fold_schedule_events(session.own_events())["active"])
        except _domain.ScheduleLogError as error:
            self._warn(
                f'Session "{session.session_id}" historical events could not be read '
                f"({error}); the legacy reminder is ignored.")
            return
        if active > 0:
            self._warn(
                f'Session "{session.session_id}" contains legacy reminders; recreate '
                "active reminders with schedule_create.")

    # ---------- 公共管理面 ----------

    async def create(self, session_id: str, request: dict, signal: Any = None) -> dict:
        await self._ensure_domain()
        selectors = [
            request.get("at") is not None,
            request.get("after_seconds") is not None,
            request.get("every_seconds") is not None,
            request.get("daily") is not None,
            request.get("weekly") is not None,
            request.get("cron") is not None,
        ]
        if sum(selectors) > 1:
            raise _domain.ScheduleInputError(
                "invalid_selector", "Exactly one reminder selector is required.")
        title = _domain.schedule_title(request.get("title"))
        id_ = f"schedule-{uuid.uuid4()}"
        now = _domain.now_ms()
        if request.get("at") is not None:
            record = _domain.create_at_schedule_record(id_, request["prompt"], request["at"], now, title)
        elif request.get("after_seconds") is not None:
            record = _domain.create_after_schedule_record(
                id_, request["prompt"], request["after_seconds"], now, title)
        elif request.get("every_seconds") is not None:
            record = _domain.create_every_schedule_record(
                id_, request["prompt"], request["every_seconds"], now, title)
        elif request.get("daily") is not None:
            record = _domain.create_daily_schedule_record(
                id_, request["prompt"], request["daily"], now, title)
        elif request.get("weekly") is not None:
            record = _domain.create_weekly_schedule_record(
                id_, request["prompt"], request["weekly"], now, title)
        elif request.get("cron") is not None:
            record = _domain.create_cron_schedule_record(
                id_, request["prompt"], request["cron"], now, title)
        else:
            raise _domain.ScheduleInputError(
                "invalid_selector", "Exactly one reminder selector is required.")

        async def work() -> dict:
            self._assert_signal(signal)
            await self._table.put(id_, {
                "sessionId": session_id, "record": record, "status": "active",
                "deliveryHistory": {"records": [], "earlierRecordsUnavailable": False},
            })
            self._emit_changed()
            if self._runtime is not None:
                self._runtime.request_drive()
            return record

        return await self._serialize(work)

    async def list(self, request: dict) -> list:
        await self._ensure_domain()
        session_id = request["sessionId"]
        return [task["record"] for _key, task in self._table.entries()
                if task["sessionId"] == session_id and task["status"] == "active"]

    async def catalog(self) -> list:
        await self._ensure_domain()
        entries = []
        for _key, task in self._table.entries():
            entry = {**task["record"], "sessionId": task["sessionId"],
                     "status": task["status"]}
            if task.get("lastDelivery") is not None:
                entry["lastDelivery"] = task["lastDelivery"]
            entries.append(entry)
        entries.sort(key=lambda entry: (entry["scheduledAt"], entry["id"]))
        return entries

    async def history(self, request: dict) -> dict:
        await self._ensure_domain()
        limit = request.get("limit")
        if not _domain._safe_int(limit) or limit < 1 or limit > 100:
            raise _domain.ScheduleInputError(
                "invalid_rule", "Delivery history limit must be a safe integer from 1 through 100.")
        task = self._table.get(request["id"])
        if task is None or task["sessionId"] != request["sessionId"]:
            return {"id": request["id"], "code": "schedule_not_found"}
        return delivery_history_page(task, request, self.retention)

    async def delete(self, request: dict, signal: Any = None) -> dict:
        await self._ensure_domain()

        async def work() -> dict:
            self._assert_signal(signal)
            current = self._table.get(request["id"])
            if current is None or current["sessionId"] != request["sessionId"]:
                return {"id": request["id"], "deleted": False, "code": "schedule_not_found"}
            await self._table.delete(request["id"])
            self._emit_changed()
            if self._runtime is not None:
                self._runtime.request_drive()
            return {"id": request["id"], "deleted": True}

        return await self._serialize(work)

    async def update(self, request: dict, signal: Any = None) -> dict:
        await self._ensure_domain()

        async def work() -> dict:
            self._assert_signal(signal)
            current = self._table.get(request["id"])
            if current is None or current["sessionId"] != request["sessionId"]:
                return {"id": request["id"], "updated": False, "code": "schedule_not_found"}
            if current["status"] == "inactive":
                return {"id": request["id"], "updated": False, "code": "schedule_ended"}
            result = resolve_schedule_update(
                current["record"], request.get("expected"), request.get("change"),
                _domain.now_ms(), request)
            if "record" not in result or not result["updated"]:
                return result
            await self._table.put(request["id"], {**current, "record": result["record"]})
            self._emit_changed()
            if self._runtime is not None:
                self._runtime.request_drive()
            return result

        return await self._serialize(work)

    # ---------- 归档准入 / 内部 ----------

    async def stop_session_tasks(self, session_id: str) -> None:
        await self._ensure_domain()

        async def work() -> None:
            active = [task["record"]["id"] for _key, task in self._table.entries()
                      if task["sessionId"] == session_id and task["status"] == "active"]
            if not active:
                return
            removed = False
            failure: BaseException | None = None
            for id_ in active:
                try:
                    await self._table.delete(id_)
                    removed = True
                except BaseException as error:  # noqa: BLE001 - 每行都尝试
                    if failure is None:
                        failure = error
            if removed:
                self._emit_changed()
                if self._runtime is not None:
                    self._runtime.request_drive()
            if failure is not None:
                raise failure

        await self._serialize(work)

    async def _commit_task(self, task: dict) -> None:
        await self._table.put(task["record"]["id"], task)
        self._emit_changed()

    async def _resolve_agent(self, session_id: str) -> Any:
        controller = self.ctx.get("sessionController")
        if controller is not None and hasattr(controller, "resolve_agent"):
            result = controller.resolve_agent(session_id)
            if inspect.isawaitable(result):
                result = await result
            if isinstance(result, dict):
                if result.get("error") is not None:
                    raise result["error"]
                return result.get("agent")
            return result
        agents = self.ctx.get("agents")
        agent = agents.get(session_id) if agents is not None else None
        if agent is None:
            raise RuntimeError(f'no live agent for session "{session_id}"')
        return agent

    def _assert_signal(self, signal: Any) -> None:
        if signal is not None and hasattr(signal, "is_set") and signal.is_set():
            raise asyncio.CancelledError()

    def _emit_changed(self) -> None:
        try:
            self.ctx.emit("schedule/changed")
        except BaseException as error:  # noqa: BLE001 - 通知不是事务参与者
            self._warn(f"schedule/changed listener failed: {error}")

    def _warn(self, message: str) -> None:
        logger = getattr(self, "_logger", None)
        if logger is not None and hasattr(logger, "warn"):
            logger.warn(message)

    def _serialize(self, work) -> Any:
        if self._stopping:
            raise RuntimeError("Schedule service is stopping")
        loop = asyncio.get_running_loop()
        prior = self._chain
        tail = loop.create_future()
        self._chain = tail

        async def run() -> Any:
            if prior is not None:
                try:
                    await prior
                except BaseException:  # noqa: BLE001 - 前序失败不阻断本事务
                    pass
            try:
                return await work()
            finally:
                if not tail.done():
                    tail.set_result(None)

        return run()

    def dispose(self) -> None:
        if self._disposed:
            return
        self._disposed = True
        self._stopping = True
        runtime = self._runtime
        if runtime is not None:
            runtime._stopping = True
            runtime._clear_timer()
        self._attached.clear()


def install_schedule(ctx: Context, config: dict | None = None) -> ScheduleService:
    """幂等装配 Schedule 服务（`ctx.schedule`），要求 storage 与 sessions。"""
    existing = ctx.get("schedule")
    if existing is not None:
        return existing
    if ctx.get("storage") is None:
        raise RuntimeError("install_schedule requires ctx.storage (install_storage)")
    if ctx.get("sessions") is None or ctx.get("agents") is None:
        raise RuntimeError("install_schedule requires ctx.agents and ctx.sessions")
    return ScheduleService(ctx, config)
