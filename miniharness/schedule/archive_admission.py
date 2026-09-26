"""Schedule 家族并入 Workspace 归档准入（对齐 schedule/src/index.ts:82-112）。

回答 `workspace/session-activity`：该 Session 的 active reminders 按 kind
`schedule` 前插（items = [{id, label: prompt}]）；`workspace/session-stop`：
逐一删除这些 active reminders（落 `schedule/change {operation:'delete'}`）。

载体差异（登记）：
  * mini 的 `workspace/session-activity` 是同步 waterfall（next_() 委派），
    上游 async；active reminders 从 `fold_schedule_events` 折叠（同上游
    activeReminders 的 fold）。
  * 两个监听器与 install_schedule 的 fiber 同寿（同 jobs/subagent 归档准入）。
  * `SessionActivityKindMap` 需登记 `schedule` 键（workspace/__init__.py）。
"""
from __future__ import annotations

from typing import Any

__all__ = ["install_schedule_archive_admission"]


class _ActivityItem:
    """活动项（`.id`/`.label`），对齐上游 SessionActivityItem 的消费面。"""

    __slots__ = ("id", "label")

    def __init__(self, id_: str, label) -> None:
        self.id = id_
        self.label = label


class _ScheduleActivity:
    """`schedule` 活动家族（`.kind`/`.items`），对齐上游 SessionActivity 消费面。"""

    __slots__ = ("kind", "items")

    def __init__(self, items: list) -> None:
        self.kind = "schedule"
        self.items = items


def _session_events(ctx: Any, session_id: str) -> list | None:
    """定位 session 的既有事件（经 agents 或 sessions 服务）。"""
    agents = ctx.get("agents")
    if agents is not None:
        try:
            agent = agents.get(session_id)
        except Exception:  # noqa: BLE001 - 定位失败按无事件处理
            agent = None
        if agent is not None and getattr(agent, "session", None) is not None:
            return list(agent.session.snapshot_events())
    sessions = ctx.get("sessions")
    if sessions is not None:
        try:
            session = sessions.get(session_id)
        except Exception:  # noqa: BLE001 - 未知会话按无事件处理
            session = None
        if session is not None:
            return list(session.snapshot_events())
    return None


def _active_reminders(ctx: Any, session_id: str) -> list:
    """该 Session 的 active reminders（fold_schedule_events 的 active）。"""
    from .domain import fold_schedule_events
    events = _session_events(ctx, session_id)
    if events is None:
        return []
    try:
        folded = fold_schedule_events(events)
    except Exception:  # noqa: BLE001 - 折叠失败按无 reminders 处理
        return []
    return list(folded.get("active") or [])


def install_schedule_archive_admission(ctx: Any) -> None:
    """安装 schedule 归档准入：activity 询问 + session-stop 删除 active reminders。

    @param ctx install_schedule 的装配上下文。
    """
    async def on_session_activity(payload: dict, next_) -> list:
        session_id = payload.get("sessionId") if isinstance(payload, dict) else None
        reminders = _active_reminders(ctx, session_id) if session_id is not None else []
        rest = await next_()
        if not reminders:
            return rest
        own = _ScheduleActivity([
            _ActivityItem(record["id"], record.get("prompt") or record.get("label") or record["id"])
            for record in reminders
        ])
        return [own, *(rest or [])]

    async def on_session_stop(payload: dict) -> None:
        session_id = payload.get("sessionId") if isinstance(payload, dict) else None
        if session_id is None:
            return
        reminders = _active_reminders(ctx, session_id)
        if not reminders:
            return
        session = None
        agents = ctx.get("agents")
        if agents is not None:
            try:
                agent = agents.get(session_id)
            except Exception:  # noqa: BLE001 - 定位失败按无 session 处理
                agent = None
            if agent is not None and getattr(agent, "session", None) is not None:
                session = agent.session
        if session is None:
            sessions = ctx.get("sessions")
            if sessions is not None:
                try:
                    session = sessions.get(session_id)
                except Exception:  # noqa: BLE001 - 未知会话按无 session 处理
                    session = None
        if session is None:
            return
        for record in reminders:
            try:
                session.append("schedule/change", {
                    "version": 1, "operation": "delete", "id": record["id"]})
            except Exception as error:  # noqa: BLE001 - 单条失败不扣住其余
                logger = getattr(ctx, "logger", None)
                if logger is not None:
                    logger.warn(
                        f'schedule: deleting reminder "{record["id"]}" for an archived '
                        f"Session failed: {error}")

    ctx.on("workspace/session-activity", on_session_activity)
    ctx.on("workspace/session-stop", on_session_stop)