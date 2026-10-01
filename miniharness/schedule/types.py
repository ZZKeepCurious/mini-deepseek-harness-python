"""Schedule 域类型与常量（对齐 packages/schedule/schedule/src/types.ts）。

契约：
  * 记录变体六类：after/at（单次）、every/daily/weekly/cron（周期）；全部已
    trim，scheduledAt 为四位数年 canonical RFC3339 UTC 时刻，title 必填（≤120）。
  * Host 任务行 {sessionId, record, status, lastDelivery?, deliveryHistory?}；
    status 缺省 active；lastDelivery 必须等于历史最后一条回执（storage.py 校验）。
  * 历史 `schedule/change` v1 事件只保留 after/at/every 三变体（legacy 只读），
    新任务不再写会话事件。
  * 错误码闭集：invalid_prompt / invalid_selector / invalid_rule /
    invalid_time_zone / not_future / time_out_of_range / frequency_too_high /
    internal_error；管理面另有 schedule_not_found / schedule_conflict /
    schedule_ended / delivery_cursor_not_found。
"""
from __future__ import annotations

from typing import Any, TypeAlias

__all__ = [
    "MAX_TITLE_LENGTH",
    "MIN_EVERY_INTERVAL_SECONDS",
    "CRON_SEARCH_HORIZON_YEARS",
    "SCHEDULE_CHANGE_VERSION",
    "DEFAULT_DELIVERY_HISTORY_DAYS",
    "DEFAULT_DELIVERY_HISTORY_RECORDS",
    "ScheduleId",
    "INPUT_ERROR_CODES",
    "TOOL_ERROR_CODES",
    "ONE_SHOT_KINDS",
    "RECURRING_KINDS",
]

#: 会话内稳定 id（上游 Branded<ScheduleId>；Python 侧注解即契约）。
ScheduleId: TypeAlias = str

#: schedule/change 持久协议版本（上游 domain.ts SCHEDULE_CHANGE_VERSION）。
SCHEDULE_CHANGE_VERSION = 1

#: v1 固定周期下界（秒，上游 domain.ts MIN_EVERY_INTERVAL_SECONDS）。
MIN_EVERY_INTERVAL_SECONDS = 60

#: 存储任务名上界（上游 domain.ts MAX_TITLE_LENGTH）。
MAX_TITLE_LENGTH = 120

#: cron 日期搜索的前后向年界（上游 domain.ts CRON_SEARCH_HORIZON_YEARS）：
#: 四百年的闰年/星期对齐循环保证任何可满足规则都在此窗口内有匹配。
CRON_SEARCH_HORIZON_YEARS = 400

#: delivery-history 保留窗口天数缺省（上游 index.ts 缺省 30）。
DEFAULT_DELIVERY_HISTORY_DAYS = 30

#: delivery-history 保留记录条数缺省（上游 index.ts 缺省 200）。
DEFAULT_DELIVERY_HISTORY_RECORDS = 200

#: 单次规则判别式。
ONE_SHOT_KINDS = ("after", "at")

#: 周期规则判别式。
RECURRING_KINDS = ("every", "daily", "weekly", "cron")

#: ScheduleInputError code 闭集（上游 domain.ts ScheduleInputError）。
INPUT_ERROR_CODES = frozenset({
    "invalid_prompt",
    "invalid_selector",
    "invalid_rule",
    "invalid_time_zone",
    "not_future",
    "time_out_of_range",
    "frequency_too_high",
})

#: 工具输出错误码闭集（上游 tools.ts ERROR_SCHEMAS，加 internal_error 兜底）。
TOOL_ERROR_CODES = tuple(sorted(INPUT_ERROR_CODES | {"internal_error"}))


def schedule_task(session_id: str, record: dict, status: str = "active",
                  last_delivery: Any = None, delivery_history: Any = None) -> dict:
    """构造一个 Host 任务行（缺省不含可选键）。"""
    task: dict = {"sessionId": session_id, "record": record, "status": status}
    if last_delivery is not None:
        task["lastDelivery"] = last_delivery
    if delivery_history is not None:
        task["deliveryHistory"] = delivery_history
    return task
