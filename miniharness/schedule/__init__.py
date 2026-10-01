"""Schedule 域：Host 全局持久提醒 + 共享人/模型管理面。

上游对照：packages/schedule/schedule/src/{types,domain,index,runtime,tools,
update,delivery-history,storage}.ts。

导出：
  * 域纯函数族（domain.py）——解码/折叠/周期解析/framing。
  * `ScheduleService` + `install_schedule`（service.py）——Host 全局管理服务，
    权威存储是 `schedule` storage-domain 表。
  * `ScheduleRuntime`（runtime.py）——单一 Host 定时器。
  * storage/update/delivery-history 的公开面。
"""
from __future__ import annotations

from .delivery_history import append_delivery, delivery_history_page
from .domain import (
    CRON_SEARCH_HORIZON_YEARS,
    MAX_TITLE_LENGTH,
    MIN_EVERY_INTERVAL_SECONDS,
    REQUIRED_TITLE_MESSAGE,
    SCHEDULE_CHANGE_VERSION,
    SCHEDULED_MESSAGE_FRAMING,
    ScheduleInputError,
    ScheduleLogError,
    allocate_schedule_id,
    apply_schedule_changes,
    canonicalize_cron_expression,
    canonicalize_time_zone,
    create_after_schedule_record,
    create_at_schedule_record,
    create_cron_schedule_record,
    create_daily_schedule_record,
    create_every_schedule_record,
    create_weekly_schedule_record,
    decode_schedule_change,
    decode_schedule_record,
    decode_stored_title,
    fold_schedule_events,
    is_recurring_schedule_record,
    normalize_weekdays,
    parse_at_input,
    parse_cron_input,
    parse_daily_input,
    parse_weekly_input,
    render_recurring_reminder_batch_framing,
    render_reminder_framing,
    resolve_cron_occurrence,
    resolve_daily_occurrence,
    resolve_every_occurrence,
    resolve_recurring_occurrence,
    resolve_weekly_occurrence,
    schedule_title,
    schedule_view,
    weekly_time,
)
from .runtime import ScheduleRuntime
from .service import ScheduleService, install_schedule
from .storage import schedule_domain, schedule_task_schema
from .tools import register_schedule_tools
from .update import resolve_schedule_update, retained_title
from .types import (
    DEFAULT_DELIVERY_HISTORY_DAYS,
    DEFAULT_DELIVERY_HISTORY_RECORDS,
    ONE_SHOT_KINDS,
    RECURRING_KINDS,
    ScheduleId,
)

__all__ = [
    "CRON_SEARCH_HORIZON_YEARS",
    "DEFAULT_DELIVERY_HISTORY_DAYS",
    "DEFAULT_DELIVERY_HISTORY_RECORDS",
    "MAX_TITLE_LENGTH",
    "MIN_EVERY_INTERVAL_SECONDS",
    "ONE_SHOT_KINDS",
    "RECURRING_KINDS",
    "REQUIRED_TITLE_MESSAGE",
    "SCHEDULED_MESSAGE_FRAMING",
    "SCHEDULE_CHANGE_VERSION",
    "ScheduleId",
    "ScheduleInputError",
    "ScheduleLogError",
    "ScheduleRuntime",
    "ScheduleService",
    "allocate_schedule_id",
    "append_delivery",
    "apply_schedule_changes",
    "canonicalize_cron_expression",
    "canonicalize_time_zone",
    "create_after_schedule_record",
    "create_at_schedule_record",
    "create_cron_schedule_record",
    "create_daily_schedule_record",
    "create_every_schedule_record",
    "create_weekly_schedule_record",
    "decode_schedule_change",
    "decode_schedule_record",
    "decode_stored_title",
    "delivery_history_page",
    "fold_schedule_events",
    "install_schedule",
    "is_recurring_schedule_record",
    "normalize_weekdays",
    "parse_at_input",
    "parse_cron_input",
    "parse_daily_input",
    "parse_weekly_input",
    "register_schedule_tools",
    "render_recurring_reminder_batch_framing",
    "render_reminder_framing",
    "resolve_cron_occurrence",
    "resolve_daily_occurrence",
    "resolve_every_occurrence",
    "resolve_recurring_occurrence",
    "resolve_schedule_update",
    "resolve_weekly_occurrence",
    "retained_title",
    "schedule_domain",
    "schedule_task_schema",
    "schedule_title",
    "schedule_view",
    "weekly_time",
]
