"""Schedule 家族并入 Workspace 归档准入（对齐 schedule/src/index.ts:82-112）。

Host 任务在会话 Agent 之外存活，故归档准入读存储行而非 live runtime：
`workspace/session-activity` 报告该会话仍 armed 的 reminders（kind `schedule`）；
`workspace/session-stop` 删除这些行，避免其投递进已关闭会话。
"""
from __future__ import annotations

import inspect
from typing import Any

__all__ = ["install_schedule_archive_admission"]


class _ActivityItem:
    """`schedule` 活动项（`.id`/`.label`），对齐 workspace SessionActivityItem 消费面。"""

    __slots__ = ("id", "label")

    def __init__(self, id_: str, label: Any) -> None:
        self.id = id_
        self.label = label


class _ScheduleActivity:
    """`schedule` 活动家族（`.kind`/`.items`），对齐 workspace SessionActivity 消费面。"""

    __slots__ = ("kind", "items")

    def __init__(self, items: list) -> None:
        self.kind = "schedule"
        self.items = items


def install_schedule_archive_admission(ctx: Any, service: Any) -> None:
    """安装两个归档准入监听器（与装配 fiber 同寿）。"""

    async def on_session_activity(payload: Any, next_: Any) -> list:
        session_id = payload.get("sessionId") if isinstance(payload, dict) else None
        active = await service.list({"sessionId": session_id}) if session_id is not None else []
        rest = next_()
        if inspect.isawaitable(rest):
            rest = await rest
        if not active:
            return rest or []
        own = _ScheduleActivity([
            _ActivityItem(record["id"], record.get("title"))
            for record in active
        ])
        return [own, *(rest or [])]

    async def on_session_stop(payload: Any) -> None:
        session_id = payload.get("sessionId") if isinstance(payload, dict) else None
        if session_id is not None:
            await service.stop_session_tasks(session_id)

    ctx.on("workspace/session-activity", on_session_activity)
    ctx.on("workspace/session-stop", on_session_stop)
