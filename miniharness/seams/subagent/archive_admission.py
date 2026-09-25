"""subagent 家族并入 Workspace 归档准入（对齐 subagent/src/archive-admission.ts）。

回答 `workspace/session-activity`：被询问 Session 的、仍在回合内的 subagent
后代（durable lineage：`parentSession` + `origin:'subagent'`，任意深度；fork
无 origin 字段因而不在此列），按 kind `subagent` 前插 `{id, label}` 活动项；
`workspace/session-stop`：把每个运行中的后代以 `cancel({kind:'parent'})` 停掉。
两个监听器与 ctx 同寿，由 subagent 运行时构造点安装。

上游经 `ctx.agents.list()` 读 durable lineage、经 `sessionQuery.observeSession`
折叠子描述符取 label；mini 的活体注册表由 continuation manager 的 `_live`
承载（其成员即 origin='subagent' 的 continuable 子代理），label 取激活簿记
（无 label 时省略，与上游无 sessionQuery 时同名 by id 同形）。

载体说明（登记 verified-diffs）：mini 无 `sessionQuery.observeSession` 观测面，
descendant 集合与 label 取自 manager 活体注册表 / 激活记录，而非从子会话日志
折叠描述符事件。
"""
from __future__ import annotations

from typing import Any, Callable

from ...workspace import SessionActivity, SessionActivityItem

__all__ = ["install_subagent_archive_admission"]


def _running_descendants(live: list, root_id: str) -> list:
    """自 root 沿 durable lineage 向下的运行中 subagent 后代（BFS，防损坏环路）。"""
    children: dict = {}
    for loop in live:
        session = getattr(loop, "session", None)
        meta = getattr(session, "meta", None) or {}
        if not isinstance(meta, dict):
            continue
        parent = meta.get("parentSession")
        if not isinstance(parent, str) or meta.get("origin") != "subagent":
            continue
        children.setdefault(parent, []).append(loop)
    running: list = []
    pending = [root_id]
    visited: set = set()
    while pending:
        parent_id = pending.pop(0)
        if parent_id in visited:
            continue
        visited.add(parent_id)
        for child in children.get(parent_id, []):
            if getattr(child, "status", None) == "running":
                running.append(child)
            pending.append(child.id)
    return running


def install_subagent_archive_admission(
    ctx: Any,
    live_lookup: Callable[[], list],
    label_lookup: Callable[[str], str | None] | None = None,
) -> None:
    """安装 subagent 归档准入：activity 询问 + session-stop 取消。

    @param ctx - subagent 运行时的注册上下文。
    @param live_lookup - 返回当前活体 subagent 列表（含委托父）的回调。
    @param label_lookup - 按 child id 取 durable 创建 label 的回调；缺省省略 label。
    """

    def on_session_activity(payload: Any, next_: Any) -> list:
        session_id = payload.get("sessionId") if isinstance(payload, dict) else None
        running = _running_descendants(live_lookup(), session_id) if session_id is not None else []
        rest = next_()
        if not running:
            return rest
        items = [
            SessionActivityItem(
                loop.id,
                label_lookup(loop.id) if label_lookup is not None else None,
            )
            for loop in running
        ]
        return [SessionActivity("subagent", items), *(rest or [])]

    def on_session_stop(payload: Any) -> None:
        session_id = payload.get("sessionId") if isinstance(payload, dict) else None
        if session_id is None:
            return
        for child in _running_descendants(live_lookup(), session_id):
            try:
                child.cancel("parent")
            except Exception as error:  # noqa: BLE001 - 一个子拒绝取消不能扣住其余兄弟
                logger = getattr(ctx, "logger", None)
                if logger is not None:
                    logger.warn(
                        f'subagent: cancelling "{child.id}" for an archived Session failed: {error}')

    ctx.on("workspace/session-activity", on_session_activity)
    ctx.on("workspace/session-stop", on_session_stop)
