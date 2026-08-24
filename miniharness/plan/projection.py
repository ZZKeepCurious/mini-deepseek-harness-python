"""Plan 投影单元：纯事件折叠（session-projection 的 `plan` 键，上游 index.ts:268-278
@ dsh-v0.1.1-rc.2）。

三事件成功结算制（rc.2）：`command/run`(name='plan') 只记 running 执行
（{commandId, wanted}）；配对 `command/done` 结算——kind:'success' 且目标异于
生效态才升格为 wanted（失败/幂等选择清除）；`plan/mode` 记录该选择并清空
wanted。pending 因此是纯重放量：宿主重启、其他 tab、冷读都能只从日志恢复
（上游同注释）。

view 语义（上游 index.ts wire.view）：
  wanted = running.wanted ?? wanted；
  {active: 生效状态, pending: wanted != null 且 wanted != active}
——执行未结算时 pending 反映运行中意图（上游 PlanUnitState.running，
stateVersion 2；mini 无注册表/持久化缓存，version 仅登记不消费）。
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
    running: dict | None = None  # 最近一次 /plan 执行待其 command/done 结算
    for index, event in enumerate(events):
        if end is not None and index >= end:
            break
        data = event["data"]
        if event["type"] == "command/run" and data.get("name") == "plan":
            args = data.get("args")
            if args is None:
                continue  # args 缺失（recordInput: false）不构成选择（上游同守卫）
            # rc.2：不再与 wanted 去重——每次执行都进入 running 等结算
            running = {"commandId": data["commandId"], "wanted": args.strip() != "off"}
        elif event["type"] == "command/done" and running is not None \
                and data.get("commandId") == running["commandId"]:
            # 只有成功且目标异于生效态的选择才成为 wanted（失败/幂等 → 清除）
            wanted = running["wanted"] if (data.get("kind") == "success"
                                           and running["wanted"] != active) else None
            running = None
        elif event["type"] == "plan/mode":
            active = data["active"]
            wanted = None
    effective = running["wanted"] if running is not None else wanted
    return {
        "active": active,
        "pending": effective is not None and effective != active,
    }
