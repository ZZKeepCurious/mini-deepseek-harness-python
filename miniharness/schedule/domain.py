"""Strict Schedule 解码、重放、时区校验与 framing 纯函数域。

上游对照：packages/schedule/schedule/src/domain.ts（逐函数移植）。

载体差异（登记）：
  * 时区用 stdlib ``zoneinfo.ZoneInfo``（九三年上游用 Temporal + Intl）；DST
    消歧 ``earlier``、间隙跳过、重叠只发一次，逐字对齐。canonical 名取
    ``ZoneInfo(value).key``——对文档化别名（如 US/Eastern）保留输入拼写，
    不做 Intl 的别名归一（等价时序比较两侧都过同一函数，故一致）。
  * 日期算术用 :mod:`datetime` + 显式 civil 算法（``_from_epoch_ms``），无平台
    CRT 年范围限制（Windows 负时间戳安全，上游 ``new Date`` 同域）。
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from typing import Any
from collections.abc import Mapping

__all__ = [
    "CRON_SEARCH_HORIZON_YEARS",
    "MAX_TITLE_LENGTH",
    "MIN_EVERY_INTERVAL_SECONDS",
    "SCHEDULE_CHANGE_VERSION",
    "ScheduleInputError",
    "ScheduleLogError",
    "REQUIRED_TITLE_MESSAGE",
    "allocate_schedule_id",
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
    "fold_schedule_events",
    "is_recurring_schedule_record",
    "normalize_weekdays",
    "parse_at_input",
    "parse_cron_input",
    "parse_daily_input",
    "parse_weekly_input",
    "render_recurring_reminder_batch_framing",
    "render_reminder_framing",
    "resolve_cron_occurrence",
    "resolve_daily_occurrence",
    "resolve_every_occurrence",
    "resolve_recurring_occurrence",
    "resolve_weekly_occurrence",
    "schedule_title",
    "schedule_view",
    "weekly_time",
]

from .types import (
    CRON_SEARCH_HORIZON_YEARS,
    MAX_TITLE_LENGTH,
    MIN_EVERY_INTERVAL_SECONDS,
    SCHEDULE_CHANGE_VERSION,
)

#: 稳定诊断：缺失/空白 title。
REQUIRED_TITLE_MESSAGE = "title is required and must be non-empty after trimming."

_UTC_INSTANT = re.compile(
    r"(?!0000)\d{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12]\d|3[01])"
    r"T(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d\.\d{3}Z$",
)
_OFFSET_INSTANT = re.compile(
    r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})"
    r"T(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})"
    r"(?:\.(?P<fraction>\d{1,3}))?(?P<zone>Z|(?P<sign>[+-])"
    r"(?P<offsetHour>\d{2}):(?P<offsetMinute>\d{2}))$",
)
_LOCAL_DATE = re.compile(r"(?P<year>\d{4})-(?P<month>\d{2})-(?P<day>\d{2})$")
_LOCAL_TIME = re.compile(
    r"(?P<hour>\d{2}):(?P<minute>\d{2}):(?P<second>\d{2})(?:\.(?P<fraction>\d{1,3}))?$",
)
_LOCAL_CLOCK_TIME = re.compile(
    r"(?:[01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d{1,3})?$")
_IANA_ZONE = re.compile(r"[A-Za-z][A-Za-z0-9_+.-]*(?:\/[A-Za-z0-9_+.-]+)+$")

_UTC = timezone.utc
_EPOCH = datetime(1970, 1, 1, tzinfo=_UTC)
_MAX_SAFE_INTEGER = 2**53 - 1

_ZONE_CACHE: dict[str, ZoneInfo] = {}


def _zone(name: str) -> ZoneInfo:
    zone = _ZONE_CACHE.get(name)
    if zone is None:
        zone = ZoneInfo(name)
        _ZONE_CACHE[name] = zone
    return zone


def _safe_int(value: Any) -> bool:
    """等价上游 Number.isSafeInteger：真 int、非 bool、|v| ≤ 2^53-1。"""
    return (isinstance(value, int) and not isinstance(value, bool)
            and -_MAX_SAFE_INTEGER <= value <= _MAX_SAFE_INTEGER)


def _epoch_ms(dt: datetime) -> int:
    """aware datetime → 毫秒 epoch（整型精确，无浮点舍入）。"""
    delta = dt - _EPOCH
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def _from_epoch_ms(epoch: int) -> datetime:
    """毫秒 epoch → UTC datetime，平台无关（Howard Hinnant civil_from_days）。"""
    seconds, millis = divmod(epoch, 1_000)
    days, secs = divmod(seconds, 86_400)
    z = days + 719_468
    era = (z if z >= 0 else z - 146_096) // 146_097
    doe = z - era * 146_097
    yoe = (doe - doe // 1_460 + doe // 36_524 - doe // 146_096) // 365
    year = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    year = year + 1 if month <= 2 else year
    return datetime(year, month, day, tzinfo=_UTC) + timedelta(
        seconds=secs, milliseconds=millis)


def _format_epoch_ms(epoch: int) -> str:
    dt = _from_epoch_ms(epoch)
    return (f"{dt.year:04d}-{dt.month:02d}-{dt.day:02d}T{dt.hour:02d}:"
            f"{dt.minute:02d}:{dt.second:02d}.{dt.microsecond // 1000:03d}Z")


def _days_from_civil(year: int, month: int, day: int) -> int:
    year -= month <= 2
    era = (year if year >= 0 else year - 399) // 400
    yoe = year - era * 400
    doy = (153 * (month - 3 if month > 2 else month + 9) + 2) // 5 + day - 1
    doe = yoe * 365 + yoe // 4 - yoe // 100 + doy
    return era * 146_097 + doe - 719_468


def _civil_from_days(z: int) -> tuple:
    z += 719_468
    era = (z if z >= 0 else z - 146_096) // 146_097
    doe = z - era * 146_097
    yoe = (doe - doe // 1_460 + doe // 36_524 - doe // 146_096) // 365
    year = yoe + era * 400
    doy = doe - (365 * yoe + yoe // 4 - yoe // 100)
    mp = (5 * doy + 2) // 153
    day = doy - (153 * mp + 2) // 5 + 1
    month = mp + 3 if mp < 10 else mp - 9
    year += month <= 2
    return (year, month, day)


def _civil_fields(epoch_ms: int) -> tuple:
    """毫秒 epoch → (y, mo, d, h, mi, s, ms) 的 civil 分解（无 datetime 年界）。"""
    days, rem = divmod(epoch_ms, 86_400_000)
    year, month, day = _civil_from_days(days)
    seconds, millis = divmod(rem, 1_000)
    hour, rem2 = divmod(seconds, 3_600)
    minute, second = divmod(rem2, 60)
    return (year, month, day, hour, minute, second, millis)


#: 偏移查询的安全秒级窗口（避免 datetime 年界；年份 1/9999 附近的规则是固定的）。
_MIN_SAFE_LOOKUP_MS = _epoch_ms(datetime(1, 1, 2, tzinfo=_UTC))
_MAX_SAFE_LOOKUP_MS = _epoch_ms(datetime(9999, 12, 30, tzinfo=_UTC))


def _utc_offset_ms(time_zone: str, epoch: int) -> int:
    zone = _zone(time_zone)
    try:
        local = _from_epoch_ms(epoch).astimezone(zone)
    except (OverflowError, ValueError, OSError):
        safe = min(max(epoch, _MIN_SAFE_LOOKUP_MS), _MAX_SAFE_LOOKUP_MS)
        local = _from_epoch_ms(safe).astimezone(zone)
    return int(local.utcoffset().total_seconds() * 1000)


class _CivilDate:
    """序数日承载的 proleptic Gregorian 日期（支持四位数年之外的边界，对齐
    Temporal.PlainDate；stdlib ``datetime.date`` 上限 9999-12-31）。"""

    __slots__ = ("days",)

    def __init__(self, days: int) -> None:
        self.days = days

    @classmethod
    def from_ymd(cls, year: int, month: int, day: int) -> "_CivilDate":
        return cls(_days_from_civil(year, month, day))

    @property
    def year(self) -> int:
        return _civil_from_days(self.days)[0]

    @property
    def month(self) -> int:
        return _civil_from_days(self.days)[1]

    @property
    def day(self) -> int:
        return _civil_from_days(self.days)[2]

    @property
    def isoweekday(self) -> int:
        """ISO 星期，周一 1 至周日 7（对齐 Temporal dayOfWeek）。"""
        return ((self.days + 3) % 7) + 1

    def add_days(self, count: int) -> "_CivilDate":
        return _CivilDate(self.days + count)

    def add_years(self, count: int) -> "_CivilDate":
        year = self.year + count
        day = 28 if (self.month == 2 and self.day == 29 and not _is_leap_year(year)) \
            else self.day
        return _CivilDate.from_ymd(year, self.month, day)

    def first_of_next_month(self) -> "_CivilDate":
        year, month, _day = _civil_from_days(self.days)
        return _CivilDate.from_ymd(year + (month == 12), (month % 12) + 1, 1)

    def last_day_before_month_start(self) -> "_CivilDate":
        year, month, _day = _civil_from_days(self.days)
        first = _CivilDate.from_ymd(year, month, 1)
        return first.add_days(-1)

    def __eq__(self, other: Any) -> bool:
        return isinstance(other, _CivilDate) and self.days == other.days

    def __lt__(self, other: "_CivilDate") -> bool:
        return self.days < other.days

    def __le__(self, other: "_CivilDate") -> bool:
        return self.days <= other.days

    def __ge__(self, other: "_CivilDate") -> bool:
        return self.days >= other.days

    def __gt__(self, other: "_CivilDate") -> bool:
        return self.days > other.days

    def __hash__(self) -> int:
        return hash(self.days)


_MIN_FOUR_DIGIT_YEAR_MS = _epoch_ms(datetime(1, 1, 1, tzinfo=_UTC))
_MAX_FOUR_DIGIT_YEAR_MS = _epoch_ms(
    datetime(9999, 12, 31, 23, 59, 59, 999_000, tzinfo=_UTC))


class ScheduleLogError(Exception):
    """持久 schedule 数据损坏（上游 domain.ts ScheduleLogError）。"""

    code = "corrupt_schedule_log"


class ScheduleInputError(Exception):
    """模型输入无法成为记录（code 稳定，上游 ScheduleInputError）。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _is_record(value: Any) -> bool:
    return isinstance(value, Mapping)


def _has_exact_keys(value: Mapping, wanted: tuple) -> bool:
    return len(value) == len(wanted) and all(key in value for key in wanted)


def _has_exact_keys_with_optional(value: Mapping, required: tuple,
                                  optional: tuple) -> bool:
    allowed = set(required) | set(optional)
    return all(key in value for key in required) and all(key in allowed for key in value)


# ---------- title ----------

def schedule_title(title: str) -> str:
    """校验创建期 title（上游 domain.ts scheduleTitle）。"""
    if not isinstance(title, str) or title.strip() == "":
        raise ScheduleInputError("invalid_prompt", REQUIRED_TITLE_MESSAGE)
    normalized = title.strip()
    if len(normalized) > MAX_TITLE_LENGTH:
        raise ScheduleInputError(
            "invalid_prompt", f"title must be at most {MAX_TITLE_LENGTH} characters.")
    return normalized


def decode_stored_title(value: Any) -> str:
    """持久边界校验一条 required title（上游 decodeStoredTitle）。"""
    if not isinstance(value, str) or value.strip() == "":
        raise ScheduleLogError(REQUIRED_TITLE_MESSAGE)
    if len(value) > MAX_TITLE_LENGTH:
        raise ScheduleLogError(
            f"title must be at most {MAX_TITLE_LENGTH} characters")
    if value.strip() != value:
        raise ScheduleLogError("title must be a trimmed string")
    return value


def _decode_record_title(value: Mapping, expected: tuple, message: str) -> str:
    title = decode_stored_title(value.get("title"))
    if not _has_exact_keys(value, expected):
        raise ScheduleLogError(message)
    return title


def _decode_historical_title(value: Mapping, expected: tuple,
                             message: str) -> Any:
    keys = tuple(key for key in expected if key != "title")
    if not _has_exact_keys_with_optional(value, keys, ("title",)):
        raise ScheduleLogError(message)
    raw = value.get("title")
    return None if raw is None else decode_stored_title(raw)


# ---------- 基础字段 ----------

def _decode_id(value: Any) -> str:
    if not isinstance(value, str) or value == "" or value.strip() != value:
        raise ScheduleLogError(
            "schedule id must be a non-empty string without surrounding whitespace")
    return value


def _epoch_from_canonical(value: str) -> int:
    dt = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=_UTC)
    return _epoch_ms(dt)


def _decode_instant(value: Any) -> str:
    if not isinstance(value, str) or _UTC_INSTANT.match(value) is None:
        raise ScheduleLogError(
            "scheduledAt must be a canonical four-digit-year RFC 3339 UTC instant")
    try:
        roundtrip = _format_epoch_ms(_epoch_from_canonical(value))
    except (ValueError, OverflowError):
        raise ScheduleLogError("scheduledAt is not a real UTC calendar instant")
    if roundtrip != value:
        raise ScheduleLogError("scheduledAt is not a real UTC calendar instant")
    return value


def _future_instant(epoch: int, now: int) -> str:
    if (not _safe_int(now) or not _safe_int(epoch)
            or epoch < _MIN_FOUR_DIGIT_YEAR_MS or epoch > _MAX_FOUR_DIGIT_YEAR_MS):
        raise ScheduleInputError(
            "time_out_of_range",
            "The scheduled time must be representable as a four-digit-year RFC 3339 UTC instant.")
    if epoch <= now:
        raise ScheduleInputError(
            "not_future", "The scheduled time must be strictly in the future.")
    return _format_epoch_ms(epoch)


def _group_number(groups: Mapping, name: str) -> int:
    value = groups.get(name)
    if value is None:
        raise ScheduleInputError("invalid_rule", "The at value has an invalid shape.")
    return int(value)


def _milliseconds(value: Any) -> int:
    return 0 if value is None else int((value + "000")[:3])


def _parse_offset_instant(value: str) -> int:
    match = _OFFSET_INSTANT.match(value)
    if match is None:
        raise ScheduleInputError(
            "invalid_rule",
            "at must use YYYY-MM-DDTHH:mm:ss with optional 1-3 digit fractional "
            "seconds and an explicit Z or numeric offset.")
    groups = match.groupdict()
    parts = (
        _group_number(groups, "year"), _group_number(groups, "month"),
        _group_number(groups, "day"), _group_number(groups, "hour"),
        _group_number(groups, "minute"), _group_number(groups, "second"),
        _milliseconds(groups.get("fraction")),
    )
    if parts[0] == 0 or parts[3] > 23 or parts[4] > 59 or parts[5] > 59:
        raise ScheduleInputError(
            "invalid_rule", "The at value must be a real ISO calendar date and time.")
    try:
        local = datetime(parts[0], parts[1], parts[2], parts[3], parts[4],
                         parts[5], parts[6] * 1_000, tzinfo=_UTC)
    except ValueError:
        raise ScheduleInputError(
            "invalid_rule", "The at value must be a real ISO calendar date and time.")
    if groups["zone"] == "Z":
        return _epoch_ms(local)
    offset_hour = _group_number(groups, "offsetHour")
    offset_minute = _group_number(groups, "offsetMinute")
    if (offset_hour > 23 or offset_minute > 59
            or (groups["sign"] == "-" and offset_hour == 0 and offset_minute == 0)):
        raise ScheduleInputError("invalid_rule", "The at numeric offset is invalid.")
    direction = 1 if groups["sign"] == "+" else -1
    return _epoch_ms(local) - direction * (offset_hour * 60 + offset_minute) * 60_000


def canonicalize_time_zone(value: str) -> str:
    """校验并规范化 IANA 时区选择器（上游 domain.ts canonicalizeTimeZone）。"""
    if (not value or value.strip() != value
            or (value != "UTC" and _IANA_ZONE.match(value) is None)):
        raise ScheduleInputError(
            "invalid_time_zone", "time_zone must be UTC or a valid IANA Area/Location name.")
    try:
        canonical = _zone(value).key
    except (ZoneInfoNotFoundError, ValueError, OSError):
        raise ScheduleInputError(
            "invalid_time_zone", "time_zone must be UTC or a valid IANA Area/Location name.")
    if canonical != "UTC" and _IANA_ZONE.match(canonical) is None:
        raise ScheduleInputError(
            "invalid_time_zone", "time_zone must resolve to UTC or an IANA Area/Location name.")
    return canonical


def _parse_local_at(value: Mapping) -> tuple:
    date_match = _LOCAL_DATE.match(value.get("date")) if isinstance(value.get("date"), str) else None
    time_text = value.get("time")
    time_match = _LOCAL_TIME.match(time_text) if isinstance(time_text, str) else None
    if date_match is None or time_match is None:
        raise ScheduleInputError(
            "invalid_rule",
            "Local at requires date YYYY-MM-DD and time HH:mm:ss with optional "
            "one-to-three digit milliseconds.")
    dg = date_match.groupdict()
    tg = time_match.groupdict()
    parts = (
        _group_number(dg, "year"), _group_number(dg, "month"),
        _group_number(dg, "day"), _group_number(tg, "hour"),
        _group_number(tg, "minute"), _group_number(tg, "second"),
        _milliseconds(tg.get("fraction")),
    )
    if parts[0] == 0 or parts[3] > 23 or parts[4] > 59 or parts[5] > 59:
        raise ScheduleInputError(
            "invalid_rule", "The local at value must be a real ISO calendar date and time.")
    try:
        datetime(parts[0], parts[1], parts[2], parts[3], parts[4], parts[5],
                 parts[6] * 1_000, tzinfo=_UTC)
    except ValueError:
        raise ScheduleInputError(
            "invalid_rule", "The local at value must be a real ISO calendar date and time.")
    return parts


def _local_instant(parts: tuple, time_zone: str) -> int | None:
    """本地墙钟 → UTC instant；重叠取更早（fold=0），间隙返回 None。

    对齐 Temporal ``toZonedDateTime(tz, {disambiguation:'earlier'})`` 后要求
    墙钟字段回读相等：间隙时间回读会前移，故不等即判间隙。

    墙钟→instant 手算（civil days + 时区偏移），不经 ``datetime`` 构造：
    Python ``datetime`` 限 year ≥ 1，而 Temporal.PlainDateTime 支持
    proleptic Gregorian year 0——负偏移时区在四位数年份下限附近的墙钟会落进
    year 0（``0001-01-01T00:01Z`` 在 ``Etc/GMT+1`` 的本地是 ``0000-12-31T23:01``），
    经 ``datetime`` 构造会抛 ValueError 被误当间隙跳过，把本应落在
    ``0001-01-01T00:02Z`` 的目标推到 ``01:00Z``。
    """
    local_ms = (_days_from_civil(parts[0], parts[1], parts[2]) * 86_400_000
                + parts[3] * 3_600_000 + parts[4] * 60_000
                + parts[5] * 1_000 + parts[6])
    # 偏移依赖 instant：往返回代一次即定点（此后回读校验兜底其余分歧）。
    epoch = local_ms
    for _ in range(3):
        shifted = local_ms - _utc_offset_ms(time_zone, epoch)
        if shifted == epoch:
            break
        epoch = shifted
    if _civil_fields(epoch + _utc_offset_ms(time_zone, epoch)) != parts:
        return None
    return epoch


def parse_at_input(at: Any) -> int:
    """解析绝对选择器（不要求 future；上游 parseAtInput）。"""
    if isinstance(at, str):
        return _parse_offset_instant(at)
    if _is_record(at):
        if not _has_exact_keys(at, ("date", "time", "time_zone")):
            raise ScheduleInputError(
                "invalid_rule", "Local at must contain exactly date, time, and time_zone.")
        if not isinstance(at["date"], str) or not isinstance(at["time"], str):
            raise ScheduleInputError(
                "invalid_rule", "Local at date and time must be strings.")
        if not isinstance(at["time_zone"], str):
            raise ScheduleInputError("invalid_time_zone", "time_zone must be a string.")
        parts = _parse_local_at(at)
        target = _local_instant(parts, canonicalize_time_zone(at["time_zone"]))
        if target is None:
            raise ScheduleInputError(
                "invalid_rule", "The local at time does not exist in the selected time zone.")
        return target
    raise ScheduleInputError(
        "invalid_rule", "at must be an explicit-offset string or local calendar object.")


# ---------- 本地钟点 / 周期选择器 ----------

def _local_clock(value: str, selector: str) -> tuple:
    if not isinstance(value, str) or _LOCAL_CLOCK_TIME.match(value) is None:
        raise ScheduleInputError(
            "invalid_rule",
            f"{selector}.time must use HH:mm:ss with optional 1-3 fractional digits, "
            "without leap seconds or 24:00.")
    hour, minute, second = int(value[0:2]), int(value[3:5]), int(value[6:8])
    fraction = value[9:] if len(value) > 8 else None
    return (hour, minute, second, _milliseconds(fraction))


def _clock_string(parts: tuple) -> str:
    return f"{parts[0]:02d}:{parts[1]:02d}:{parts[2]:02d}.{parts[3]:03d}"


def weekly_time(value: str) -> tuple:
    """解析 weekly 时钟（上游 weeklyTime）。"""
    return _local_clock(value, "weekly")


def parse_daily_input(daily: Any) -> dict:
    """规范化 daily 选择器（上游 parseDailyInput）。"""
    if (not _is_record(daily) or not _has_exact_keys(daily, ("time", "time_zone"))
            or not isinstance(daily.get("time"), str)):
        raise ScheduleInputError(
            "invalid_rule",
            "daily must contain exactly time and time_zone, with time HH:mm:ss and "
            "optional 1-3 fractional digits.")
    if not isinstance(daily.get("time_zone"), str):
        raise ScheduleInputError("invalid_time_zone", "time_zone must be a string.")
    return {
        "time": _clock_string(_local_clock(daily["time"], "daily")),
        "timeZone": canonicalize_time_zone(daily["time_zone"]),
    }


def normalize_weekdays(weekdays: Any) -> list:
    """规范化 ISO 星期集合（唯一升序 1-7；上游 normalizeWeekdays）。"""
    if not isinstance(weekdays, list) or len(weekdays) == 0:
        raise ScheduleInputError(
            "invalid_rule", "weekly.weekdays must be a non-empty array of ISO weekday numbers.")
    unique: set = set()
    for weekday in weekdays:
        if (not isinstance(weekday, int) or isinstance(weekday, bool)
                or weekday < 1 or weekday > 7):
            raise ScheduleInputError(
                "invalid_rule",
                "Each weekly.weekdays entry must be an integer from 1 (Monday) through 7 (Sunday).")
        if weekday in unique:
            raise ScheduleInputError(
                "invalid_rule", f"weekly.weekdays must not repeat weekday {weekday}.")
        unique.add(weekday)
    return sorted(unique)


def parse_weekly_input(weekly: Any) -> dict:
    """规范化 weekly 选择器（上游 parseWeeklyInput）。"""
    if (not _is_record(weekly)
            or not _has_exact_keys(weekly, ("time", "time_zone", "weekdays"))
            or not isinstance(weekly.get("time"), str)):
        raise ScheduleInputError(
            "invalid_rule",
            "weekly must contain exactly time, time_zone, and weekdays, with time "
            "HH:mm:ss and optional 1-3 fractional digits.")
    if not isinstance(weekly.get("time_zone"), str):
        raise ScheduleInputError("invalid_time_zone", "time_zone must be a string.")
    return {
        "time": _clock_string(_local_clock(weekly["time"], "weekly")),
        "timeZone": canonicalize_time_zone(weekly["time_zone"]),
        "weekdays": normalize_weekdays(weekly["weekdays"]),
    }


# ---------- cron ----------

_CRON_FIELDS = (
    ("minute", 0, 59, 59),
    ("hour", 0, 23, 23),
    ("day-of-month", 1, 31, 31),
    ("month", 1, 12, 12),
    ("day-of-week", 0, 7, 6),
)


def _invalid_cron_field(spec: tuple, element: str) -> ScheduleInputError:
    return ScheduleInputError(
        "invalid_rule",
        f"cron.expression {spec[0]} field element {json.dumps(element)} must be *, "
        "a value, a-b, */n, a-b/n, or a comma-separated list of those.")


def _cron_field_value(text: str, spec: tuple) -> int:
    value = int(text)
    if value < spec[1] or value > spec[2]:
        raise ScheduleInputError(
            "invalid_rule",
            f"cron.expression {spec[0]} field value {text} is outside {spec[1]}-{spec[2]}.")
    return value


def _cron_field_step(text: str, spec: tuple) -> int:
    step = int(text)
    if step < 1:
        raise ScheduleInputError(
            "invalid_rule", f"cron.expression {spec[0]} field step must be a positive integer.")
    return step


def _cron_field_values(raw: str, spec: tuple) -> list:
    _, minimum, maximum, canonical_max = spec
    matched: set = set()
    for element in raw.split(","):
        if element == "":
            raise ScheduleInputError(
                "invalid_rule",
                f"cron.expression {spec[0]} field must not contain an empty list element.")
        if element == "*":
            for value in range(minimum, maximum + 1):
                matched.add(value)
            continue
        if element.startswith("*"):
            stepped = re.fullmatch(r"\*/(\d+)", element)
            if stepped is None:
                raise _invalid_cron_field(spec, element)
            step = _cron_field_step(stepped.group(1), spec)
            for value in range(minimum, maximum + 1, step):
                matched.add(value)
            continue
        parsed = re.fullmatch(r"(\d+)(?:-(\d+))?(?:/(\d+))?", element)
        if parsed is None:
            raise _invalid_cron_field(spec, element)
        start_text, end_text, step_text = parsed.group(1), parsed.group(2), parsed.group(3)
        start = _cron_field_value(start_text, spec)
        if end_text is None:
            if step_text is not None:
                raise _invalid_cron_field(spec, element)
            matched.add(start)
            continue
        last = _cron_field_value(end_text, spec)
        if start > last:
            raise ScheduleInputError(
                "invalid_rule",
                f"cron.expression {spec[0]} field range {start}-{last} is inverted.")
        step = 1 if step_text is None else _cron_field_step(step_text, spec)
        for value in range(start, last + 1, step):
            matched.add(value)
    values = set()
    for value in matched:
        values.add(minimum if canonical_max != maximum and value == maximum else value)
    return sorted(values)


def _encode_cron_field(values: list) -> str:
    first = values[0]
    if len(values) == 1:
        return str(first)
    last = values[-1]
    step = values[1] - first
    uniform = all(values[index] - values[index - 1] == step
                  for index in range(2, len(values)))
    if uniform:
        return f"{first}-{last}" if step == 1 else f"{first}-{last}/{step}"
    parts = []
    run_start = first
    for index in range(1, len(values)):
        current, previous = values[index], values[index - 1]
        if current == previous + 1:
            continue
        parts.append(str(run_start) if run_start == previous else f"{run_start}-{previous}")
        run_start = current
    final = values[-1]
    parts.append(str(run_start) if run_start == final else f"{run_start}-{final}")
    return ",".join(parts)


def _star_walk(spec: tuple, step: int) -> list:
    _, minimum, maximum, canonical_max = spec
    walked = set()
    for value in range(minimum, maximum + 1, step):
        walked.add(minimum if canonical_max != maximum and value == maximum else value)
    return sorted(walked)


def _encode_star_cron_field(values: list, spec: tuple) -> str:
    _, minimum, _, canonical_max = spec
    if len(values) == canonical_max - minimum + 1:
        return "*"
    present = set(values)
    best_step: int | None = None
    best_walk: list = []
    for step in range(1, spec[2] - minimum + 2):
        walk = _star_walk(spec, step)
        if len(walk) > len(best_walk) and all(value in present for value in walk):
            best_step = step
            best_walk = walk
    if best_step is None:
        raise ScheduleInputError(
            "invalid_rule", f"{spec[0]} cannot keep a leading '*'.")
    walked = set(best_walk)
    remaining = [value for value in values if value not in walked]
    walk_text = f"*/{best_step}"
    return walk_text if not remaining else f"{walk_text},{_encode_cron_field(remaining)}"


def _parse_cron_field(raw: str, spec: tuple) -> dict:
    values = _cron_field_values(raw, spec)
    star = raw.startswith("*")
    canonical = (_encode_star_cron_field(values, spec) if star
                 else _encode_cron_field(values))
    return {"values": values, "canonical": canonical, "star": star}


def _parse_cron_expression(expression: str) -> dict:
    if (not isinstance(expression, str) or expression == ""
            or expression.strip() != expression):
        raise ScheduleInputError(
            "invalid_rule", "cron.expression must be a non-empty trimmed string.")
    fields = re.split(r"\s+", expression)
    if len(fields) != 5:
        raise ScheduleInputError(
            "invalid_rule",
            "cron.expression must contain exactly five whitespace-separated fields: "
            "minute hour day-of-month month day-of-week.")
    parsed = [_parse_cron_field(fields[index], _CRON_FIELDS[index]) for index in range(5)]
    minute, hour, day_of_month, month, day_of_week = parsed
    return {
        "expression": " ".join(field["canonical"] for field in parsed),
        "minutes": minute["values"],
        "hours": hour["values"],
        "daysOfMonth": day_of_month["values"],
        "months": month["values"],
        "daysOfWeek": day_of_week["values"],
        "dayOfMonthStar": day_of_month["star"],
        "dayOfWeekStar": day_of_week["star"],
    }


def canonicalize_cron_expression(expression: str) -> str:
    return _parse_cron_expression(expression)["expression"]


def parse_cron_input(cron: Any) -> dict:
    if (not _is_record(cron) or not _has_exact_keys(cron, ("expression", "time_zone"))
            or not isinstance(cron.get("expression"), str)):
        raise ScheduleInputError(
            "invalid_rule",
            "cron must contain exactly expression and time_zone, with a five-field "
            "cron expression.")
    if not isinstance(cron.get("time_zone"), str):
        raise ScheduleInputError("invalid_time_zone", "time_zone must be a string.")
    return {
        "expression": _parse_cron_expression(cron["expression"])["expression"],
        "timeZone": canonicalize_time_zone(cron["time_zone"]),
    }


# ---------- 本地日期 / instant 工具 ----------

def _is_leap_year(year: int) -> bool:
    return year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)


def _local_date_of(epoch: int, time_zone: str) -> _CivilDate:
    fields = _civil_fields(epoch + _utc_offset_ms(time_zone, epoch))
    return _CivilDate.from_ymd(fields[0], fields[1], fields[2])


def _local_time_of(epoch: int, time_zone: str) -> tuple:
    fields = _civil_fields(epoch + _utc_offset_ms(time_zone, epoch))
    return fields[3:]


def _combine(day: _CivilDate, time_parts: tuple) -> tuple:
    return (day.year, day.month, day.day, time_parts[0], time_parts[1],
            time_parts[2], time_parts[3])


def _time_cmp(left: tuple, right: tuple) -> int:
    return (left > right) - (left < right)


_EVERY_DATE = lambda _day: True  # noqa: E731 - 单值谓词


def _weekday_set(weekdays: list):
    selected = set(weekdays)
    return lambda day: day.isoweekday in selected


def _next_matching_target(time_parts: tuple, time_zone: str, now: int, matches,
                          after_date: "_CivilDate | None" = None) -> str | None:
    day = _local_date_of(now, time_zone)
    last = _local_date_of(_MAX_FOUR_DIGIT_YEAR_MS, "UTC").add_days(1)
    if after_date is not None and day <= after_date:
        day = after_date.add_days(1)
    while day <= last:
        if matches(day):
            target = _local_instant(_combine(day, time_parts), time_zone)
            if target is not None:
                if target > _MAX_FOUR_DIGIT_YEAR_MS:
                    return None
                if target >= _MIN_FOUR_DIGIT_YEAR_MS and target > now:
                    return _format_epoch_ms(target)
        day = day.add_days(1)
    return None


def _latest_due_occurrence(time_zone: str, matches, time_parts: tuple,
                           accepted_at: int, saved_target: int) -> int:
    day = _local_date_of(accepted_at, "UTC").add_days(1)
    while True:
        if matches(day):
            candidate = _local_instant(_combine(day, time_parts), time_zone)
            if candidate is not None and candidate <= accepted_at:
                return max(candidate, saved_target)
        day = day.add_days(-1)


def _accepted_decision(selector: str, accepted_at: int) -> int:
    if (not _safe_int(accepted_at) or accepted_at < _MIN_FOUR_DIGIT_YEAR_MS
            or accepted_at > _MAX_FOUR_DIGIT_YEAR_MS):
        raise ScheduleLogError(
            f"{selector} acceptedAt must be a representable four-digit-year instant")
    return accepted_at


def _next_daily_target(time_parts: tuple, time_zone: str, now: int,
                       after_date: "_CivilDate | None" = None) -> str | None:
    return _next_matching_target(time_parts, time_zone, now, _EVERY_DATE, after_date)


# ---------- 周期解析 ----------

def resolve_every_occurrence(record: Mapping, accepted_at: int) -> dict:
    """不枚举积压地求一次固定周期决定（上游 resolveEveryOccurrence）。"""
    target = _epoch_from_canonical(record["scheduledAt"])
    interval = record["everySeconds"] * 1_000
    if (not _safe_int(accepted_at) or accepted_at < _MIN_FOUR_DIGIT_YEAR_MS
            or accepted_at > _MAX_FOUR_DIGIT_YEAR_MS):
        raise ScheduleLogError(
            "every acceptedAt must be a representable four-digit-year instant")
    if not _safe_int(interval) or interval <= 0:
        raise ScheduleLogError("every interval milliseconds must be a positive safe integer")
    if accepted_at < target:
        raise ScheduleLogError("every dispatch cannot precede the active scheduledAt")
    steps = (accepted_at - target) // interval
    occurrence = target + steps * interval
    if (not _safe_int(occurrence) or occurrence < target or occurrence > accepted_at):
        raise ScheduleLogError(
            "every occurrence arithmetic must stay within the accepted interval")
    occurrence_at = _format_epoch_ms(occurrence)
    nxt = occurrence + interval
    if not _safe_int(nxt) or nxt > _MAX_FOUR_DIGIT_YEAR_MS:
        return {"occurrenceAt": occurrence_at}
    return {"occurrenceAt": occurrence_at, "nextScheduledAt": _format_epoch_ms(nxt)}


def resolve_daily_occurrence(record: Mapping, accepted_at: int) -> dict:
    decision = _accepted_decision("daily", accepted_at)
    saved = _epoch_from_canonical(record["scheduledAt"])
    if decision < saved:
        raise ScheduleLogError("daily dispatch cannot precede the active scheduledAt")
    time_parts = _local_clock(record["time"], "daily")
    occurrence = _latest_due_occurrence(
        record["timeZone"], _EVERY_DATE, time_parts, decision, saved)
    occurrence_at = _format_epoch_ms(occurrence)
    nxt = _next_daily_target(time_parts, record["timeZone"], decision,
                             _local_date_of(occurrence, record["timeZone"]))
    return ({"occurrenceAt": occurrence_at} if nxt is None
            else {"occurrenceAt": occurrence_at, "nextScheduledAt": nxt})


def _next_weekly_target(time_parts: tuple, time_zone: str, weekdays: list,
                        now: int, after_date: "_CivilDate | None" = None) -> str | None:
    return _next_matching_target(time_parts, time_zone, now,
                                 _weekday_set(weekdays), after_date)


def resolve_weekly_occurrence(record: Mapping, accepted_at: int) -> dict:
    decision = _accepted_decision("weekly", accepted_at)
    saved = _epoch_from_canonical(record["scheduledAt"])
    if decision < saved:
        raise ScheduleLogError("weekly dispatch cannot precede the active scheduledAt")
    time_parts = _local_clock(record["time"], "weekly")
    weekdays = normalize_weekdays(list(record["weekdays"]))
    occurrence = _latest_due_occurrence(
        record["timeZone"], _weekday_set(weekdays), time_parts, decision, saved)
    occurrence_at = _format_epoch_ms(occurrence)
    nxt = _next_weekly_target(time_parts, record["timeZone"], weekdays, decision,
                              _local_date_of(occurrence, record["timeZone"]))
    return ({"occurrenceAt": occurrence_at} if nxt is None
            else {"occurrenceAt": occurrence_at, "nextScheduledAt": nxt})


def _cron_date_match(parsed: Mapping):
    months = set(parsed["months"])
    days_of_month = set(parsed["daysOfMonth"])
    days_of_week = set(parsed["daysOfWeek"])
    dom_restricted = not parsed["dayOfMonthStar"]
    dow_restricted = not parsed["dayOfWeekStar"]

    def match(day: _CivilDate) -> bool:
        if day.month not in months:
            return False
        dom = day.day in days_of_month
        dow = (day.isoweekday % 7) in days_of_week
        if dom_restricted and dow_restricted:
            return dom or dow
        return dom and dow

    return match


def _cron_times(parsed: Mapping) -> list:
    return [(hour, minute, 0, 0) for hour in parsed["hours"] for minute in parsed["minutes"]]


def _next_cron_target(parsed: Mapping, time_zone: str, floor: int) -> str | None:
    times = _cron_times(parsed)
    matches = _cron_date_match(parsed)
    months = set(parsed["months"])
    first_date = _local_date_of(floor, time_zone)
    day = first_date
    floor_time = _local_time_of(floor, time_zone)
    ceiling = _local_date_of(_MAX_FOUR_DIGIT_YEAR_MS, "UTC").add_days(1)
    horizon = day.add_years(CRON_SEARCH_HORIZON_YEARS)
    last_date = horizon if horizon < ceiling else ceiling
    while day <= last_date:
        if day.month not in months:
            day = day.first_of_next_month()
            continue
        if matches(day):
            for time_parts in times:
                if day == first_date and _time_cmp(time_parts, floor_time) < 0:
                    continue
                target = _local_instant(_combine(day, time_parts), time_zone)
                if target is None:
                    continue
                if target > _MAX_FOUR_DIGIT_YEAR_MS:
                    return None
                if target >= _MIN_FOUR_DIGIT_YEAR_MS and target > floor:
                    return _format_epoch_ms(target)
        day = day.add_days(1)
    return None


def _latest_cron_occurrence(parsed: Mapping, time_zone: str, decision: int,
                            saved_target: int) -> int:
    times = _cron_times(parsed)
    matches = _cron_date_match(parsed)
    months = set(parsed["months"])
    day = _local_date_of(decision, "UTC").add_days(1)
    floor_date = _local_date_of(_MIN_FOUR_DIGIT_YEAR_MS, time_zone)
    horizon = day.add_years(-CRON_SEARCH_HORIZON_YEARS)
    first_date = horizon if horizon > floor_date else floor_date
    while day >= first_date:
        if day.month not in months:
            day = day.last_day_before_month_start()
            continue
        if matches(day):
            earliest: int | None = None
            for time_parts in times:
                candidate = _local_instant(_combine(day, time_parts), time_zone)
                if candidate is not None:
                    earliest = candidate
                    break
            if earliest is not None and earliest > decision:
                day = day.add_days(-1)
                continue
            for time_parts in reversed(times):
                candidate = _local_instant(_combine(day, time_parts), time_zone)
                if (candidate is None or candidate > decision
                        or candidate < _MIN_FOUR_DIGIT_YEAR_MS):
                    continue
                return max(candidate, saved_target)
        day = day.add_days(-1)
    return saved_target


def resolve_cron_occurrence(record: Mapping, accepted_at: int) -> dict:
    decision = _accepted_decision("cron", accepted_at)
    saved = _epoch_from_canonical(record["scheduledAt"])
    if decision < saved:
        raise ScheduleLogError("cron dispatch cannot precede the active scheduledAt")
    parsed = _parse_cron_expression(record["expression"])
    occurrence = _latest_cron_occurrence(parsed, record["timeZone"], decision, saved)
    occurrence_at = _format_epoch_ms(occurrence)
    nxt = _next_cron_target(parsed, record["timeZone"], decision)
    return ({"occurrenceAt": occurrence_at} if nxt is None
            else {"occurrenceAt": occurrence_at, "nextScheduledAt": nxt})


def is_recurring_schedule_record(record: Mapping) -> bool:
    return record.get("kind") in ("every", "daily", "weekly", "cron")


def resolve_recurring_occurrence(record: Mapping, accepted_at: int) -> dict:
    kind = record.get("kind")
    if kind == "every":
        return resolve_every_occurrence(record, accepted_at)
    if kind == "daily":
        return resolve_daily_occurrence(record, accepted_at)
    if kind == "weekly":
        return resolve_weekly_occurrence(record, accepted_at)
    if kind == "cron":
        return resolve_cron_occurrence(record, accepted_at)
    raise ScheduleLogError("recurring schedule kind must be every, daily, weekly, or cron")


# ---------- 记录解码 ----------

def _decode_after_record(value: Mapping) -> dict:
    title = _decode_historical_title(
        value, ("id", "kind", "title", "prompt", "afterSeconds", "scheduledAt"),
        "after schedule must contain exactly id, kind, title, prompt, afterSeconds, "
        "and scheduledAt")
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or prompt == "" or prompt.strip() != prompt:
        raise ScheduleLogError("after prompt must be non-empty and already trimmed")
    after_seconds = value.get("afterSeconds")
    if not _safe_int(after_seconds) or after_seconds <= 0:
        raise ScheduleLogError("afterSeconds must be a positive safe integer")
    record = {
        "id": _decode_id(value["id"]), "kind": "after", "prompt": prompt,
        "afterSeconds": after_seconds, "scheduledAt": _decode_instant(value["scheduledAt"]),
    }
    if title is not None:
        record["title"] = title
    return {**record}


def _decode_at_record(value: Mapping) -> dict:
    title = _decode_historical_title(
        value, ("id", "kind", "title", "prompt", "scheduledAt"),
        "at schedule must contain exactly id, kind, title, prompt, and scheduledAt")
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or prompt == "" or prompt.strip() != prompt:
        raise ScheduleLogError("at prompt must be non-empty and already trimmed")
    record = {
        "id": _decode_id(value["id"]), "kind": "at", "prompt": prompt,
        "scheduledAt": _decode_instant(value["scheduledAt"]),
    }
    if title is not None:
        record["title"] = title
    return {**record}


def _decode_every_record(value: Mapping) -> dict:
    title = _decode_historical_title(
        value, ("id", "kind", "title", "prompt", "everySeconds", "scheduledAt"),
        "every schedule must contain exactly id, kind, title, prompt, everySeconds, "
        "and scheduledAt")
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or prompt == "" or prompt.strip() != prompt:
        raise ScheduleLogError("every prompt must be non-empty and already trimmed")
    every_seconds = value.get("everySeconds")
    interval = every_seconds * 1_000 if isinstance(every_seconds, int) \
        and not isinstance(every_seconds, bool) else None
    if (not _safe_int(every_seconds) or every_seconds < MIN_EVERY_INTERVAL_SECONDS
            or interval is None or not _safe_int(interval)):
        raise ScheduleLogError(
            f"everySeconds must be a safe integer of at least {MIN_EVERY_INTERVAL_SECONDS}")
    record = {
        "id": _decode_id(value["id"]), "kind": "every", "prompt": prompt,
        "everySeconds": every_seconds, "scheduledAt": _decode_instant(value["scheduledAt"]),
    }
    if title is not None:
        record["title"] = title
    return {**record}


def _rethrow_log(error: BaseException) -> Any:
    if isinstance(error, ScheduleLogError):
        raise error
    raise ScheduleLogError(str(error))


def _decode_daily_record(value: Mapping) -> dict:
    title = _decode_record_title(
        value, ("id", "kind", "title", "prompt", "time", "timeZone", "scheduledAt"),
        "daily schedule must contain exactly id, kind, title, prompt, time, timeZone, "
        "and scheduledAt")
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or prompt == "" or prompt.strip() != prompt:
        raise ScheduleLogError("daily prompt must be non-empty and already trimmed")
    time_value = value.get("time")
    time_zone = value.get("timeZone")
    if not isinstance(time_value, str) or not isinstance(time_zone, str):
        raise ScheduleLogError("daily time and timeZone must be strings")
    try:
        normalized = _clock_string(_local_clock(time_value, "daily"))
        canonicalize_time_zone(time_zone)
    except BaseException as error:  # noqa: BLE001 - durable boundary normalizes
        _rethrow_log(error)
    if time_value != normalized:
        raise ScheduleLogError("daily time must be normalized to HH:mm:ss.SSS")
    return {
        "id": _decode_id(value["id"]), "kind": "daily", "title": title, "prompt": prompt,
        "time": time_value, "timeZone": time_zone,
        "scheduledAt": _decode_instant(value["scheduledAt"]),
    }


def _decode_weekly_record(value: Mapping) -> dict:
    title = _decode_record_title(
        value, ("id", "kind", "title", "prompt", "time", "timeZone", "weekdays",
                "scheduledAt"),
        "weekly schedule must contain exactly id, kind, title, prompt, time, timeZone, "
        "weekdays, and scheduledAt")
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or prompt == "" or prompt.strip() != prompt:
        raise ScheduleLogError("weekly prompt must be non-empty and already trimmed")
    time_value = value.get("time")
    time_zone = value.get("timeZone")
    if not isinstance(time_value, str) or not isinstance(time_zone, str):
        raise ScheduleLogError("weekly time and timeZone must be strings")
    try:
        canonicalize_time_zone(time_zone)
        normalized = _clock_string(_local_clock(time_value, "weekly"))
    except BaseException as error:  # noqa: BLE001
        _rethrow_log(error)
    if time_value != normalized:
        raise ScheduleLogError("weekly time must be normalized to HH:mm:ss.SSS")
    stored = value.get("weekdays")
    try:
        weekdays = normalize_weekdays(stored)
    except BaseException as error:  # noqa: BLE001
        _rethrow_log(error)
    if len(weekdays) != len(stored) or any(
            weekdays[index] != stored[index] for index in range(len(weekdays))):
        raise ScheduleLogError(
            "weekly weekdays must be normalized to unique ascending ISO weekday numbers")
    return {
        "id": _decode_id(value["id"]), "kind": "weekly", "title": title, "prompt": prompt,
        "time": time_value, "timeZone": time_zone, "weekdays": weekdays,
        "scheduledAt": _decode_instant(value["scheduledAt"]),
    }


def _decode_cron_record(value: Mapping) -> dict:
    title = _decode_record_title(
        value, ("id", "kind", "title", "prompt", "expression", "timeZone", "scheduledAt"),
        "cron schedule must contain exactly id, kind, title, prompt, expression, "
        "timeZone, and scheduledAt")
    prompt = value.get("prompt")
    if not isinstance(prompt, str) or prompt == "" or prompt.strip() != prompt:
        raise ScheduleLogError("cron prompt must be non-empty and already trimmed")
    expression = value.get("expression")
    time_zone = value.get("timeZone")
    if not isinstance(expression, str) or not isinstance(time_zone, str):
        raise ScheduleLogError("cron expression and timeZone must be strings")
    try:
        canonicalize_time_zone(time_zone)
        canonical = _parse_cron_expression(expression)["expression"]
    except BaseException as error:  # noqa: BLE001
        _rethrow_log(error)
    if expression != canonical:
        raise ScheduleLogError("cron expression must be canonical")
    return {
        "id": _decode_id(value["id"]), "kind": "cron", "title": title, "prompt": prompt,
        "expression": expression, "timeZone": time_zone,
        "scheduledAt": _decode_instant(value["scheduledAt"]),
    }


def _decode_legacy_schedule_record(value: Any) -> dict:
    if not _is_record(value):
        raise ScheduleLogError("schedule record must be an object")
    kind = value.get("kind")
    if kind == "after":
        return _decode_after_record(value)
    if kind == "at":
        return _decode_at_record(value)
    if kind == "every":
        return _decode_every_record(value)
    raise ScheduleLogError('v1 schedule kind must be "after", "at", or "every"')


def decode_schedule_record(value: Any) -> dict:
    """解码一个当前 Host 记录（保留 committed target 与存储 zone 拼写）。"""
    if _is_record(value) and value.get("kind") == "daily":
        return _decode_daily_record(value)
    if _is_record(value) and value.get("kind") == "weekly":
        return _decode_weekly_record(value)
    if _is_record(value) and value.get("kind") == "cron":
        return _decode_cron_record(value)
    record = _decode_legacy_schedule_record(value)
    if record.get("title") is None:
        raise ScheduleLogError(REQUIRED_TITLE_MESSAGE)
    return record


def decode_schedule_change(value: Any) -> dict:
    if not _is_record(value):
        raise ScheduleLogError("schedule/change payload must be an object")
    if value.get("version") != SCHEDULE_CHANGE_VERSION:
        raise ScheduleLogError("schedule/change version must be 1")
    operation = value.get("operation")
    if operation == "create":
        if not _has_exact_keys(value, ("version", "operation", "schedule")):
            raise ScheduleLogError(
                "schedule create must contain exactly version, operation, and schedule")
        return {
            "version": SCHEDULE_CHANGE_VERSION, "operation": "create",
            "schedule": _decode_legacy_schedule_record(value["schedule"]),
        }
    if operation == "delete":
        if not _has_exact_keys(value, ("version", "operation", "id")):
            raise ScheduleLogError(
                "schedule delete must contain exactly version, operation, and id")
        return {"version": SCHEDULE_CHANGE_VERSION, "operation": "delete",
                "id": _decode_id(value["id"])}
    if operation == "dispatch":
        if _has_exact_keys(value, ("version", "operation", "id")):
            return {"version": SCHEDULE_CHANGE_VERSION, "operation": "dispatch",
                    "id": _decode_id(value["id"])}
        if _has_exact_keys(value, ("version", "operation", "id", "acceptedAt")):
            return {"version": SCHEDULE_CHANGE_VERSION, "operation": "dispatch",
                    "id": _decode_id(value["id"]),
                    "acceptedAt": _decode_instant(value["acceptedAt"])}
        raise ScheduleLogError("schedule dispatch must contain id and optional acceptedAt only")
    raise ScheduleLogError("schedule/change operation must be create, delete, or dispatch")


# ---------- 折叠 / 分配 ----------

def _dispatched_record(record: dict, change: Mapping) -> dict | None:
    has_accepted_at = "acceptedAt" in change
    if record.get("kind") != "every":
        if has_accepted_at:
            raise ScheduleLogError("one-shot dispatch must not contain acceptedAt")
        return None
    if not has_accepted_at:
        raise ScheduleLogError("every dispatch must contain acceptedAt")
    occurrence = resolve_every_occurrence(
        record, _epoch_from_canonical(change["acceptedAt"]))
    if "nextScheduledAt" not in occurrence:
        return None
    return {**record, "scheduledAt": occurrence["nextScheduledAt"]}


def apply_schedule_changes(folded: Mapping, changes) -> dict:
    active = {record["id"]: record for record in folded["active"]}
    seen = set(folded["seenIds"])
    for change in changes:
        operation = change.get("operation")
        if operation == "create":
            record = change["schedule"]
            if record["id"] in seen:
                raise ScheduleLogError(
                    f"schedule id {json.dumps(record['id'])} was reused")
            seen.add(record["id"])
            active[record["id"]] = record
        elif operation == "delete":
            if change["id"] not in active:
                raise ScheduleLogError(
                    f"schedule delete targets inactive id {json.dumps(change['id'])}")
            del active[change["id"]]
        elif operation == "dispatch":
            record = active.get(change["id"])
            if record is None:
                raise ScheduleLogError(
                    f"schedule dispatch targets inactive id {json.dumps(change['id'])}")
            nxt = _dispatched_record(record, change)
            if nxt is None:
                del active[change["id"]]
            else:
                active[change["id"]] = nxt
        else:
            raise ScheduleLogError(f"unknown decoded schedule change {operation!r}")
    return {"active": tuple(active.values()), "seenIds": tuple(seen)}


def fold_schedule_events(events, inherited_event_count: int = 0) -> dict:
    """折叠包自有流（seed 边界后；上游 foldScheduleEvents）。"""
    if (not _safe_int(inherited_event_count) or inherited_event_count < 0
            or inherited_event_count > len(events)):
        raise ScheduleLogError(
            "schedule inheritedEventCount must be within the supplied event log")
    changes = [decode_schedule_change(event["data"])
               for event in events[inherited_event_count:]
               if event.get("type") == "schedule/change"]
    return apply_schedule_changes({"active": (), "seenIds": ()}, changes)


def allocate_schedule_id(folded: Mapping) -> str:
    seen = set(folded["seenIds"])
    sequence = len(seen) + 1
    candidate = f"schedule-{sequence}"
    while candidate in seen:
        sequence += 1
        candidate = f"schedule-{sequence}"
    return candidate


# ---------- 记录创建 ----------

def _require_prompt(prompt: str) -> str:
    normalized = prompt.strip() if isinstance(prompt, str) else ""
    if not normalized:
        raise ScheduleInputError(
            "invalid_prompt", "prompt must be non-empty after trimming.")
    return normalized


def _require_now(now: int, label: str) -> int:
    if not _safe_int(now) or now < _MIN_FOUR_DIGIT_YEAR_MS or now > _MAX_FOUR_DIGIT_YEAR_MS:
        raise ScheduleInputError(
            "time_out_of_range",
            f"{label} creation time must be a representable four-digit-year UTC instant.")
    return now


def create_after_schedule_record(id_: str, prompt: str, after_seconds: int,
                                 now: int, title: str) -> dict:
    normalized = _require_prompt(prompt)
    if not _safe_int(after_seconds) or after_seconds <= 0:
        raise ScheduleInputError(
            "invalid_rule", "after_seconds must be a positive safe integer.")
    return {
        "id": id_, "kind": "after", "title": schedule_title(title), "prompt": normalized,
        "afterSeconds": after_seconds,
        "scheduledAt": _future_instant(now + after_seconds * 1_000, now),
    }


def create_at_schedule_record(id_: str, prompt: str, at: Any, now: int, title: str) -> dict:
    normalized = _require_prompt(prompt)
    return {
        "id": id_, "kind": "at", "title": schedule_title(title), "prompt": normalized,
        "scheduledAt": _future_instant(parse_at_input(at), now),
    }


def create_every_schedule_record(id_: str, prompt: str, every_seconds: int,
                                 now: int, title: str) -> dict:
    normalized = _require_prompt(prompt)
    if not _safe_int(every_seconds):
        raise ScheduleInputError("invalid_rule", "every_seconds must be a safe integer.")
    if every_seconds < MIN_EVERY_INTERVAL_SECONDS:
        raise ScheduleInputError(
            "frequency_too_high",
            f"every_seconds must be at least {MIN_EVERY_INTERVAL_SECONDS}.")
    return {
        "id": id_, "kind": "every", "title": schedule_title(title), "prompt": normalized,
        "everySeconds": every_seconds,
        "scheduledAt": _future_instant(now + every_seconds * 1_000, now),
    }


def create_daily_schedule_record(id_: str, prompt: str, daily: Any, now: int,
                                 title: str) -> dict:
    normalized = _require_prompt(prompt)
    parsed = parse_daily_input(daily)
    _require_now(now, "Daily")
    scheduled_at = _next_daily_target(
        _local_clock(parsed["time"], "daily"), parsed["timeZone"], now)
    if scheduled_at is None:
        raise ScheduleInputError(
            "time_out_of_range",
            "No future daily occurrence is representable as a four-digit-year UTC instant.")
    return {
        "id": id_, "kind": "daily", "title": schedule_title(title), "prompt": normalized,
        "time": parsed["time"], "timeZone": parsed["timeZone"], "scheduledAt": scheduled_at,
    }


def create_weekly_schedule_record(id_: str, prompt: str, weekly: Any, now: int,
                                  title: str) -> dict:
    normalized = _require_prompt(prompt)
    parsed = parse_weekly_input(weekly)
    _require_now(now, "Weekly")
    scheduled_at = _next_weekly_target(
        _local_clock(parsed["time"], "weekly"), parsed["timeZone"], parsed["weekdays"], now)
    if scheduled_at is None:
        raise ScheduleInputError(
            "time_out_of_range",
            "No future weekly occurrence is representable as a four-digit-year UTC instant.")
    return {
        "id": id_, "kind": "weekly", "title": schedule_title(title), "prompt": normalized,
        "time": parsed["time"], "timeZone": parsed["timeZone"],
        "weekdays": parsed["weekdays"], "scheduledAt": scheduled_at,
    }


def create_cron_schedule_record(id_: str, prompt: str, cron: Any, now: int,
                                title: str) -> dict:
    normalized = _require_prompt(prompt)
    parsed = parse_cron_input(cron)
    _require_now(now, "Cron")
    scheduled_at = _next_cron_target(
        _parse_cron_expression(parsed["expression"]), parsed["timeZone"], now)
    if scheduled_at is None:
        raise ScheduleInputError(
            "time_out_of_range",
            "No future cron occurrence is representable as a four-digit-year UTC instant.")
    return {
        "id": id_, "kind": "cron", "title": schedule_title(title), "prompt": normalized,
        "expression": parsed["expression"], "timeZone": parsed["timeZone"],
        "scheduledAt": scheduled_at,
    }


# ---------- 视图 / framing ----------

_U2028 = "\u2028"
_U2029 = "\u2029"


def _json_stringify(value: Any) -> str:
    """对齐 JS ``JSON.stringify``：紧凑分隔符、不转义非 ASCII、转义 U+2028/29。"""
    return json.dumps(value, ensure_ascii=False, separators=(",", ":")).replace(
        _U2028, "\\u2028").replace(_U2029, "\\u2029")


def schedule_view(record: Mapping, now: int) -> dict:
    """派生一个执行局域管理视图（上游 scheduleView）。"""
    scheduled = _epoch_from_canonical(record["scheduledAt"])
    return {**record, "state": "overdue" if now >= scheduled else "scheduled",
            "deliveryMode": "host"}


#: 单次与周期提醒共享的模型可见来源行（上游 domain.ts SCHEDULED_MESSAGE_FRAMING）。
SCHEDULED_MESSAGE_FRAMING = "This is a scheduled message from the user"


def render_reminder_framing(record: Mapping) -> str:
    """渲染到期单次提醒 framing（上游 renderReminderFraming）。"""
    return "\n".join([
        "[SCHEDULE REMINDER]",
        SCHEDULED_MESSAGE_FRAMING,
        f"schedule_id_json: {_json_stringify(record['id'])}",
        f"occurrence_at: {record['scheduledAt']}",
        f"reminder_prompt_json: {_json_stringify(record['prompt'])}",
    ])


def render_recurring_reminder_batch_framing(reminders) -> str:
    """渲染一批周期提醒 framing（上游 renderRecurringReminderBatchFraming）。"""
    payload = [
        {
            "schedule_id": entry["record"]["id"],
            "occurrence_at": entry["occurrenceAt"],
            "reminder_prompt": entry["record"]["prompt"],
        }
        for entry in reminders
    ]
    return "\n".join([
        "[SCHEDULE REMINDER BATCH]",
        SCHEDULED_MESSAGE_FRAMING,
        f"reminders_json: {_json_stringify(payload)}",
    ])


def now_ms() -> int:
    """平台墙钟（毫秒）。生产用之；测试注入显式样本。"""
    return int(time.time() * 1000)
