"""Schedule 域类型：会话内持久一次性/固定周期提醒（对齐 packages/schedule/schedule/src/types.ts）。

契约（上游证据 types.ts:1-228）：
  * `ScheduleId` 为会话内稳定 id（schedule-N，永不复用）；Python 侧不引入
    Branded 运行时包装，注释即契约。
  * 记录三变体：after（正整数延迟）、at（绝对目标）、every（固定周期 ≥300s）；
    全部已 trim，scheduledAt 为四位数年 canonical RFC3339 UTC 时刻。
  * `ScheduleChange` v1 四形状：create{schedule} / delete{id} /
    dispatch{id}（单次，无 acceptedAt）/ dispatch{id, acceptedAt}（every 批量）。
  * 错误码闭集：invalid_prompt / invalid_selector / invalid_rule /
    invalid_time_zone / not_future / time_out_of_range / frequency_too_high /
    corrupt_schedule_log / persistence_uncertain / internal_error /
    schedule_not_found（delete 半成功）。
"""
from __future__ import annotations

from typing import TypeAlias

__all__ = [
    "MIN_EVERY_INTERVAL_SECONDS",
    "SCHEDULE_CHANGE_VERSION",
    "ScheduleId",
    "ERROR_CODE_CREATE",
    "INPUT_ERROR_CODES",
    "PERSISTENCE_OPERATIONS",
]

#: 会话内稳定 id（schedule-N，永不复用；上游 Branded<ScheduleId>，Python 侧
#: 注解即契约，无运行时包装）。
ScheduleId: TypeAlias = str

#: schedule/change 持久协议版本（上游 domain.ts:22 SCHEDULE_CHANGE_VERSION）。
SCHEDULE_CHANGE_VERSION = 1

#: v1 固定周期下界（秒，上游 domain.ts:25 MIN_EVERY_INTERVAL_SECONDS）。
MIN_EVERY_INTERVAL_SECONDS = 300

#: persistence_uncertain 的 operation 闭集（上游 types.ts:122）。
PERSISTENCE_OPERATIONS = ("create", "list", "delete")

#: 模型输入错误闭集（上游 ScheduleInputError code 并集）。
INPUT_ERROR_CODES = frozenset({
    "invalid_prompt",
    "invalid_rule",
    "invalid_time_zone",
    "not_future",
    "time_out_of_range",
    "frequency_too_high",
})

#: schedule_create 输出错误码闭集（tools.ts ERROR_SCHEMAS）。
ERROR_CODE_CREATE = tuple(sorted(INPUT_ERROR_CODES | {
    "invalid_selector",
    "corrupt_schedule_log",
    "persistence_uncertain",
    "internal_error",
}))