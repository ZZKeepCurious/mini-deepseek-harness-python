"""保留任务的乐观比较-更新（对齐 packages/schedule/schedule/src/update.ts）。

比较-更新契约：``expected`` 是调用方开始编辑时观察到的完整记录，deep-equal
当前记录才继续；否则返回 ``schedule_conflict``。省略的 change/title/prompt 保留
存储值；等价规范化时序保留 committed target；kind 可改变（以同一 ``now`` 重算）。
"""
from __future__ import annotations

from typing import Any

from .domain import (
    ScheduleInputError,
    ScheduleLogError,
    canonicalize_cron_expression,
    canonicalize_time_zone,
    create_at_schedule_record,
    create_cron_schedule_record,
    create_daily_schedule_record,
    create_every_schedule_record,
    create_weekly_schedule_record,
    decode_schedule_record,
    decode_stored_title,
    parse_at_input,
    parse_cron_input,
    parse_daily_input,
    parse_weekly_input,
    schedule_title,
    _epoch_from_canonical,
)

__all__ = ["retained_title", "resolve_schedule_update"]

_TIMING_SELECTORS = {
    "at": "at", "every": "every_seconds", "daily": "daily",
    "weekly": "weekly", "cron": "cron",
}


def _validate_change(value: Any) -> dict:
    if not isinstance(value, dict):
        raise ScheduleInputError(
            "invalid_rule",
            "Timing change must be an object with exactly one supported timing selector.")
    selector = _TIMING_SELECTORS.get(value.get("kind"))
    keys = list(value.keys())
    if (selector is None or len(keys) != 2
            or "kind" not in keys or selector not in keys):
        raise ScheduleInputError(
            "invalid_rule",
            "Timing change must contain exactly kind and its matching timing selector.")
    return value


def _schedule_prompt(prompt: Any) -> str:
    if not isinstance(prompt, str) or prompt.strip() == "":
        raise ScheduleInputError(
            "invalid_prompt", "prompt must be non-empty after trimming.")
    return prompt.strip()


def retained_title(record: dict) -> str:
    """取出存储 title；缺失/空白/未 trim/超长抛 ScheduleLogError。"""
    return decode_stored_title(record["title"])


def _with_content(current: dict, title: str, prompt: str) -> dict:
    if title == current["title"] and prompt == current["prompt"]:
        return current
    return {**current, "title": title, "prompt": prompt}


def _changed_record(current: dict, title: str, prompt: str, value: Any,
                    now: int) -> dict:
    if value is None:
        return _with_content(current, title, prompt)
    change = _validate_change(value)
    kind = change["kind"]
    if kind == "at":
        if (current["kind"] in ("after", "at")
                and parse_at_input(change["at"]) == _epoch_from_canonical(current["scheduledAt"])):
            return _with_content(current, title, prompt)
        return create_at_schedule_record(current["id"], prompt, change["at"], now, title)
    if kind == "every":
        if (current["kind"] == "every"
                and change["every_seconds"] == current["everySeconds"]):
            return _with_content(current, title, prompt)
        return create_every_schedule_record(
            current["id"], prompt, change["every_seconds"], now, title)
    if kind == "daily":
        if current["kind"] == "daily":
            normalized = parse_daily_input(change["daily"])
            if (normalized["time"] == current["time"]
                    and normalized["timeZone"] == canonicalize_time_zone(current["timeZone"])):
                return _with_content(current, title, prompt)
        return create_daily_schedule_record(current["id"], prompt, change["daily"], now, title)
    if kind == "weekly":
        if current["kind"] == "weekly":
            normalized = parse_weekly_input(change["weekly"])
            if (normalized["time"] == current["time"]
                    and normalized["timeZone"] == canonicalize_time_zone(current["timeZone"])
                    and normalized["weekdays"] == list(current["weekdays"])):
                return _with_content(current, title, prompt)
        return create_weekly_schedule_record(current["id"], prompt, change["weekly"], now, title)
    if kind == "cron":
        if current["kind"] == "cron":
            normalized = parse_cron_input(change["cron"])
            if (normalized["expression"] == canonicalize_cron_expression(current["expression"])
                    and normalized["timeZone"] == canonicalize_time_zone(current["timeZone"])):
                return _with_content(current, title, prompt)
        return create_cron_schedule_record(current["id"], prompt, change["cron"], now, title)
    raise ScheduleInputError(
        "invalid_rule", "Timing change must contain exactly kind and its matching timing selector.")


def resolve_schedule_update(current: dict, expected: Any, change: Any, now: int,
                            content: Any = None) -> dict:
    """比较完整 observed 记录并解析一次名称/指令/时序更新（上游 resolveScheduleUpdate）。"""
    try:
        decoded = decode_schedule_record(expected)
    except ScheduleLogError:
        return {"code": "invalid_rule",
                "message": "expected must be a complete valid Schedule record."}
    if current != decoded:
        return {"id": current["id"], "updated": False, "code": "schedule_conflict"}
    try:
        retained = retained_title(current)
        content = content or {}
        title = retained if content.get("title") is None \
            else schedule_title(content["title"])
        prompt = current["prompt"] if content.get("prompt") is None \
            else _schedule_prompt(content["prompt"])
        record = _changed_record(current, title, prompt, change, now)
        return {"id": current["id"], "updated": record is not current, "record": record}
    except ScheduleInputError as error:
        return {"code": error.code, "message": error.message}
