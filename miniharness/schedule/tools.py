"""Agent 作用域的 Schedule 管理工具（对齐 packages/schedule/schedule/src/tools.ts）。

四工具注册进精确 agent 的 ToolRegistry，随 agent scope 拆解自动注销；
render = JSON.stringify（canonical 值直接模型可见）。错误闭集为六个输入码 +
internal_error；delete/update 未命中返回非错误的管理结果。
"""
from __future__ import annotations

from typing import Any

from ..core.tools import Tool
from . import domain as _domain
from .domain import (
    MAX_TITLE_LENGTH,
    MIN_EVERY_INTERVAL_SECONDS,
    REQUIRED_TITLE_MESSAGE,
    ScheduleInputError,
    _json_stringify,
    schedule_view,
)

__all__ = ["register_schedule_tools"]

_SELECTOR_KEYS = ("after_seconds", "at", "every_seconds", "daily", "weekly", "cron")

_SHARED_VIEW_PROPERTIES = {
    "id": {"type": "string"},
    "title": {"type": "string"},
    "prompt": {"type": "string"},
    "scheduledAt": {"type": "string"},
    "state": {"type": "string", "enum": ["scheduled", "overdue"]},
    "deliveryMode": {"type": "string", "const": "host"},
}
_SHARED_REQUIRED = ["id", "title", "prompt", "scheduledAt", "state", "deliveryMode"]


def _view_schema(kind: str, extra: dict, required_extra: list) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {**_SHARED_VIEW_PROPERTIES, "kind": {"const": kind}, **extra},
        "required": [*_SHARED_REQUIRED, "kind", *required_extra],
    }


_VIEW_SCHEMA = {"oneOf": [
    _view_schema("after", {"afterSeconds": {"type": "integer"}}, ["afterSeconds"]),
    _view_schema("at", {}, []),
    _view_schema("every", {"everySeconds": {"type": "integer"}}, ["everySeconds"]),
    _view_schema("daily", {"time": {"type": "string"}, "timeZone": {"type": "string"}},
                 ["time", "timeZone"]),
    _view_schema("weekly", {"time": {"type": "string"}, "timeZone": {"type": "string"},
                            "weekdays": {"type": "array", "items": {"type": "integer"}}},
                 ["time", "timeZone", "weekdays"]),
    _view_schema("cron", {"expression": {"type": "string"}, "timeZone": {"type": "string"}},
                 ["expression", "timeZone"]),
]}


def _error_schema(code: str) -> dict:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {"code": {"const": code}, "message": {"type": "string"}},
        "required": ["code", "message"],
    }


_ERROR_SCHEMAS = [_error_schema(code) for code in (
    "invalid_prompt", "invalid_selector", "invalid_rule", "invalid_time_zone",
    "not_future", "time_out_of_range", "frequency_too_high", "internal_error",
)]

_CREATE_OUTPUT_SCHEMA = {"oneOf": [_VIEW_SCHEMA, *_ERROR_SCHEMAS]}
_LIST_OUTPUT_SCHEMA = {"oneOf": [{"type": "array", "items": _VIEW_SCHEMA}, *_ERROR_SCHEMAS]}
_DELETE_OUTPUT_SCHEMA = {"oneOf": [
    {"type": "object", "additionalProperties": False,
     "properties": {"id": {"type": "string"}, "deleted": {"const": True}},
     "required": ["id", "deleted"]},
    {"type": "object", "additionalProperties": False,
     "properties": {"id": {"type": "string"}, "deleted": {"const": False},
                    "code": {"const": "schedule_not_found"}},
     "required": ["id", "deleted", "code"]},
    *_ERROR_SCHEMAS,
]}
_UPDATE_OUTPUT_SCHEMA = {"oneOf": [
    _VIEW_SCHEMA,
    {"type": "object", "additionalProperties": False,
     "properties": {"id": {"type": "string"}, "updated": {"const": False},
                    "code": {"enum": ["schedule_not_found", "schedule_ended",
                                      "schedule_conflict"]}},
     "required": ["id", "updated", "code"]},
    *_ERROR_SCHEMAS,
]}

_CREATE_DESCRIPTION = (
    "Create a reminder in the current session that delivers prompt when it becomes due. "
    "Supply exactly one timing parameter: after_seconds, at, every_seconds, daily, weekly, "
    "or cron. Local times that do not exist in the zone are skipped; repeated local times "
    "fire once, at the earlier instant. After downtime, a recurring reminder delivers only "
    "its latest missed occurrence. Delivery can repeat after a crash.")

_LIST_DESCRIPTION = "List the active reminders in the current session."

_DELETE_DESCRIPTION = (
    "Delete a reminder in the current session, active or inactive. Deletion does not "
    "retract a reminder message that is already queued.")

_UPDATE_DESCRIPTION = (
    "Change a reminder in place, keeping its id. Supply a new title, prompt, or at most "
    "one timing parameter; omitted fields keep their stored values. To change a relative "
    "delay, create a new reminder.")

_SELECTOR_PARAMETERS = {
    "after_seconds": {"type": "number", "description": "Delay in whole seconds."},
    "every_seconds": {
        "type": "number",
        "description": (
            f"Fixed-rate interval in whole seconds, at least {MIN_EVERY_INTERVAL_SECONDS}, "
            "aligned to the creation time; changing it with schedule_update re-aligns it "
            "to the save time."),
    },
    "daily": {
        "type": "object", "additionalProperties": False,
        "description": "Every day at a local time.",
        "properties": {
            "time": {"type": "string", "description": "HH:mm:ss with optional 1-3 fractional digits, for example 23:00:00."},
            "time_zone": {"type": "string", "description": "UTC or IANA Area/Location, for example Asia/Shanghai."},
        },
        "required": ["time", "time_zone"],
    },
    "weekly": {
        "type": "object", "additionalProperties": False,
        "description": "On the given weekdays at a local time.",
        "properties": {
            "time": {"type": "string", "description": "HH:mm:ss with optional 1-3 fractional digits, for example 09:00:00."},
            "time_zone": {"type": "string", "description": "UTC or IANA Area/Location, for example Asia/Shanghai."},
            "weekdays": {"type": "array", "items": {"type": "integer"},
                         "description": "ISO weekdays, Monday 1 through Sunday 7, without repetitions."},
        },
        "required": ["time", "time_zone", "weekdays"],
    },
    "cron": {
        "type": "object", "additionalProperties": False,
        "description": "Five-field Vixie cron expression in a time zone.",
        "properties": {
            "expression": {
                "type": "string",
                "description": "minute hour day-of-month month day-of-week, for example "
                               "\"*/15 9-17 * * 1-5\". When both day fields are restricted, "
                               "a date matches if either one matches."},
            "time_zone": {"type": "string", "description": "UTC or IANA Area/Location, for example Asia/Shanghai."},
        },
        "required": ["expression", "time_zone"],
    },
    "at": {
        "description": "Absolute target: an RFC 3339 date-time with offset, or a local date, time, and IANA time_zone.",
        "oneOf": [
            {"type": "string"},
            {"type": "object", "additionalProperties": False,
             "properties": {"date": {"type": "string"}, "time": {"type": "string"},
                            "time_zone": {"type": "string"}},
             "required": ["date", "time", "time_zone"]},
        ],
    },
}


def _render(_args: Any, value: Any) -> list:
    return [{"type": "text", "text": _json_stringify(value)}]


def _present(title: str, kind: str, raw_input: Any = None) -> dict:
    card = {"card": "generic", "title": title, "kind": kind}
    if raw_input is not None:
        card["rawInput"] = raw_input
    return card


def _internal_error() -> dict:
    return {"code": "internal_error", "message": "The schedule operation failed."}


def _operation_error(error: BaseException) -> dict:
    if isinstance(error, ScheduleInputError):
        return {"code": error.code, "message": error.message}
    return _internal_error()


def _invalid_interval(every_seconds: Any) -> dict | None:
    if every_seconds is None:
        return None
    if not _domain._safe_int(every_seconds):
        return {"code": "invalid_rule", "message": "every_seconds must be a safe integer."}
    if every_seconds < MIN_EVERY_INTERVAL_SECONDS:
        return {"code": "frequency_too_high",
                "message": f"every_seconds must be at least {MIN_EVERY_INTERVAL_SECONDS}."}
    return None


def _validate_create_args(args: dict) -> dict | None:
    allowed = ("prompt", "title", *_SELECTOR_KEYS)
    if (any(key not in allowed for key in args)
            or sum(args.get(key) is not None for key in _SELECTOR_KEYS) != 1):
        return {"code": "invalid_selector",
                "message": "schedule_create accepts exactly one of after_seconds, at, "
                           "every_seconds, daily, weekly, or cron."}
    prompt = args.get("prompt")
    if not isinstance(prompt, str) or prompt.strip() == "":
        return {"code": "invalid_prompt", "message": "prompt must be non-empty after trimming."}
    title = args.get("title")
    if not isinstance(title, str) or title.strip() == "":
        return {"code": "invalid_prompt", "message": REQUIRED_TITLE_MESSAGE}
    if len(title.strip()) > MAX_TITLE_LENGTH:
        return {"code": "invalid_prompt",
                "message": f"title must be at most {MAX_TITLE_LENGTH} characters."}
    after_seconds = args.get("after_seconds")
    if (after_seconds is not None
            and (not _domain._safe_int(after_seconds) or after_seconds <= 0)):
        return {"code": "invalid_rule", "message": "after_seconds must be a positive safe integer."}
    return _invalid_interval(args.get("every_seconds"))


_UPDATE_SELECTOR_KEYS = ("at", "every_seconds", "daily", "weekly", "cron")


def _validate_update_args(args: dict) -> dict | None:
    allowed = ("id", "title", "prompt", *_UPDATE_SELECTOR_KEYS)
    selectors = sum(args.get(key) is not None for key in _UPDATE_SELECTOR_KEYS)
    if (any(key not in allowed for key in args) or selectors > 1):
        return {"code": "invalid_selector",
                "message": "schedule_update accepts at most one of at, every_seconds, "
                           "daily, weekly, or cron."}
    id_ = args.get("id")
    if not isinstance(id_, str) or id_ == "" or id_.strip() != id_:
        return {"code": "invalid_rule",
                "message": "schedule_update id must be non-empty without surrounding whitespace."}
    if selectors == 0 and args.get("title") is None and args.get("prompt") is None:
        return {"code": "invalid_selector",
                "message": "schedule_update needs a new title, prompt, or one of at, "
                           "every_seconds, daily, weekly, or cron."}
    title = args.get("title")
    if title is not None and (not isinstance(title, str) or title.strip() == ""):
        return {"code": "invalid_prompt", "message": REQUIRED_TITLE_MESSAGE}
    if title is not None and len(title.strip()) > MAX_TITLE_LENGTH:
        return {"code": "invalid_prompt",
                "message": f"title must be at most {MAX_TITLE_LENGTH} characters."}
    prompt = args.get("prompt")
    if prompt is not None and (not isinstance(prompt, str) or prompt.strip() == ""):
        return {"code": "invalid_prompt", "message": "prompt must be non-empty after trimming."}
    return _invalid_interval(args.get("every_seconds"))


def _timing_change(args: dict) -> dict | None:
    if args.get("at") is not None:
        return {"kind": "at", "at": args["at"]}
    if args.get("every_seconds") is not None:
        return {"kind": "every", "every_seconds": args["every_seconds"]}
    if args.get("daily") is not None:
        return {"kind": "daily", "daily": args["daily"]}
    if args.get("weekly") is not None:
        return {"kind": "weekly", "weekly": args["weekly"]}
    if args.get("cron") is not None:
        return {"kind": "cron", "cron": args["cron"]}
    return None


def register_schedule_tools(service: Any, agent: Any) -> Any:
    """把四个 Schedule 工具注册进精确 agent scope，返回幂等 aggregate disposer。"""
    reg = getattr(agent, "tools", None)
    if reg is None:
        raise RuntimeError("schedule tools require agent.tools (ToolRegistry)")

    async def create_execute(args: dict, exec_: Any):
        if exec_.agent is not agent:
            return _internal_error()
        invalid = _validate_create_args(args)
        if invalid is not None:
            return invalid
        try:
            record = await service.create(
                agent.session.session_id, args, getattr(exec_, "signal", None))
            return schedule_view(record, _domain.now_ms())
        except BaseException as error:  # noqa: BLE001
            return _operation_error(error)

    async def list_execute(_args: dict, exec_: Any):
        if exec_.agent is not agent:
            return _internal_error()
        try:
            records = await service.list({"sessionId": agent.session.session_id})
            now = _domain.now_ms()
            return [schedule_view(record, now) for record in records]
        except BaseException as error:  # noqa: BLE001
            return _operation_error(error)

    async def delete_execute(args: dict, exec_: Any):
        id_ = args.get("id")
        if not isinstance(id_, str) or id_ == "" or id_.strip() != id_:
            return {"code": "invalid_rule",
                    "message": "schedule_delete id must be non-empty without surrounding whitespace."}
        if exec_.agent is not agent:
            return _internal_error()
        try:
            return await service.delete(
                {"sessionId": agent.session.session_id, "id": id_},
                getattr(exec_, "signal", None))
        except BaseException as error:  # noqa: BLE001
            return _operation_error(error)

    async def update_execute(args: dict, exec_: Any):
        if exec_.agent is not agent:
            return _internal_error()
        invalid = _validate_update_args(args)
        if invalid is not None:
            return invalid
        id_ = args["id"]
        try:
            session_id = agent.session.session_id
            expected = next(
                (record for record in await service.list({"sessionId": session_id})
                 if record["id"] == id_), None)
            if expected is None:
                ended = any(entry["sessionId"] == session_id and entry["id"] == id_
                            for entry in await service.catalog())
                return {"id": id_, "updated": False,
                        "code": "schedule_ended" if ended else "schedule_not_found"}
            change = _timing_change(args)
            request = {"sessionId": session_id, "id": id_, "expected": expected}
            if change is not None:
                request["change"] = change
            if args.get("title") is not None:
                request["title"] = args["title"]
            if args.get("prompt") is not None:
                request["prompt"] = args["prompt"]
            result = await service.update(request, getattr(exec_, "signal", None))
            return schedule_view(result["record"], _domain.now_ms()) \
                if "record" in result else result
        except BaseException as error:  # noqa: BLE001
            return _operation_error(error)

    tools = [
        Tool(
            name="schedule_create",
            description=_CREATE_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "prompt": {"type": "string",
                               "description": "Reminder content to present when the target becomes due."},
                    "title": {"type": "string",
                              "description": f"Task name of at most {MAX_TITLE_LENGTH} characters, shown on the task card and in task lists."},
                    **_SELECTOR_PARAMETERS,
                },
                "required": ["prompt", "title"],
            },
            output={"schema": _CREATE_OUTPUT_SCHEMA, "render": _render},
            execute=create_execute,
            render=_render,
            present_call=lambda args: _present("Create reminder", "other", args.get("prompt")),
        ),
        Tool(
            name="schedule_list",
            description=_LIST_DESCRIPTION,
            parameters={"type": "object", "properties": {}},
            output={"schema": _LIST_OUTPUT_SCHEMA, "render": _render},
            execute=list_execute,
            render=_render,
            present_call=lambda _args: _present("List reminders", "read"),
        ),
        Tool(
            name="schedule_delete",
            description=_DELETE_DESCRIPTION,
            parameters={"type": "object",
                        "properties": {"id": {"type": "string",
                                              "description": "Schedule id returned by schedule_list."}},
                        "required": ["id"]},
            output={"schema": _DELETE_OUTPUT_SCHEMA, "render": _render},
            execute=delete_execute,
            render=_render,
            present_call=lambda args: _present("Delete reminder", "other", args.get("id")),
        ),
        Tool(
            name="schedule_update",
            description=_UPDATE_DESCRIPTION,
            parameters={
                "type": "object",
                "properties": {
                    "id": {"type": "string",
                           "description": "Schedule id returned by schedule_list."},
                    "title": {"type": "string",
                              "description": f"New task name of at most {MAX_TITLE_LENGTH} characters."},
                    "prompt": {"type": "string", "description": "New reminder content."},
                    **_SELECTOR_PARAMETERS,
                },
                "required": ["id"],
            },
            output={"schema": _UPDATE_OUTPUT_SCHEMA, "render": _render},
            execute=update_execute,
            render=_render,
            present_call=lambda args: _present("Update reminder", "other", args.get("id")),
        ),
    ]
    disposers = [reg.register(tool) for tool in tools]

    def disposer() -> None:
        for dispose in reversed(disposers):
            dispose()

    return disposer
