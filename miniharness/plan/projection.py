"""Plan 投影单元：纯双事件折叠（session-projection 的 `plan` 键，上游 index.ts:244-266）。

`command/run`(name='plan') 记录用户已落盘的 /plan 选择（args.trim() !== 'off'）；
`plan/mode` 记录该选择并清空 pending。pending 因此是纯重放量：宿主重启、其他
tab、冷读都能只从日志恢复（上游同注释）。

view 语义（上游 index.ts:260-263）：
  {active: 生效状态, pending: wanted != null 且 wanted != active}
"""
from __future__ import annotations

__all__ = ["fold_plan_projection"]


def fold_plan_projection(events: tuple, end: int | None = None) -> dict:
    """沿日志前缀折叠 plan 投影；返回新 dict {active, pending}。

    @param events - 会话日志或其任意前缀。
    @param end - 只折叠 events[0, end)，默认全量。
    @returns {active: bool, pending: bool}（上游 PlanProjection）。
    """
    active = False
    wanted = None  # None = 无未决选择；否则为目标模式
    for index, event in enumerate(events):
        if end is not None and index >= end:
            break
        if event["type"] == "command/run" and event["data"].get("name") == "plan":
            args = event["data"].get("args")
            if args is None:
                continue  # args 缺失（recordInput: false）不构成选择（上游 index.ts:251-252）
            target = args.strip() != "off"
            if target == wanted:
                continue
            wanted = target
        elif event["type"] == "plan/mode":
            active = event["data"]["active"]
            wanted = None
    return {
        "active": active,
        "pending": wanted is not None and wanted != active,
    }
