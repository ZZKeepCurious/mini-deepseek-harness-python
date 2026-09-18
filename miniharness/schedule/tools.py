"""Agent 作用域 Schedule 管理工具（对齐 packages/schedule/schedule/src/tools.ts）。

契约：
  * 三工具注册进精确 agent 的 ToolRegistry（agent.tools），随 agent scope 拆解
    自动注销；render = JSON.stringify（canonical 值直接模型可见）。
  * 每工具 FIFO 事务：cancelled 占位（等价上游 cancellationPlaceholder——
    mini 载体为 ``exec_.signal.is_set()``，threading.Event）→ preflight flush →
    fold → append → 第二道 barrier → notifyDurableChange。
  * 错误并集闭集：六个输入码 + corrupt_schedule_log + persistence_uncertain +
    internal_error；delete 未命中返回 ``{id, deleted:false, code:
    'schedule_not_found'}``（非错误）。
"""
from __future__ import annotations

import time
from typing import Any

from ..core.tools import Tool
from .domain import (
    MIN_EVERY_INTERVAL_SECONDS,
    ScheduleInputError,
    ScheduleLogError,
    _json_stringify,
    allocate_schedule_id,
    create_after_schedule_record,
    create_at_schedule_record,
    create_every_schedule_record,
    fold_schedule_events,
    schedule_view,
)
from .persistence import SchedulePersistenceError, flush_schedule_persistence
from .transaction import run_schedule_transaction

__all__ = ["register_schedule_tools"]

_PERSISTENCE_MESSAGE = (
    "Schedule persistence is uncertain; retry with schedule_list before relying on this result."
)
_CORRUPT_MESSAGE = "The session schedule log is corrupt."
_INTERNAL_MESSAGE = "The schedule operation failed."


def _render(_args: Any, value: Any) -> list:
    return [{"type": "text", "text": _json_stringify(value)}]


def _present(title: str, kind: str, raw_input: Any = None) -> dict:
    card = {"card": "generic", "title": title, "kind": kind}
    if raw_input is not None:
        card["rawInput"] = raw_input
    return card


def _schedule_create_tool(root_ctx: Any, agent: Any, on_durable_change) -> Tool:
    async def execute(args: dict, exec_: Any):
        if exec_.agent is not agent:
            return {"code": "internal_error", "message": _INTERNAL_MESSAGE}
        invalid = _validate_create_args(args)
        if invalid is not None:
            return invalid

        def operation():
            return _create_operation(root_ctx, agent, exec_, args, on_durable_change)

        return await run_schedule_transaction(agent, operation)

    return Tool(
        name="schedule_create",
        description=(
            "Create one reminder in the current session. Supply a non-empty prompt and exactly "
            f"one selector: a positive safe-integer after_seconds delay, at as a strict offset "
            f"date-time or local date/time object, or safe-integer every_seconds of at least "
            f"{MIN_EVERY_INTERVAL_SECONDS}. Fixed-rate reminders stay creation-aligned, skip "
            "missed occurrences, and batch one latest occurrence per overdue rule. Delivery is "
            "session-local: the reminder runs on time only while this session is live and "
            "otherwise becomes overdue until the session is resumed."
        ),
        parameters={
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Reminder content to present when the target becomes due.",
                },
                "after_seconds": {
                    "type": "number",
                    "description": "Positive safe-integer delay in seconds.",
                },
                "every_seconds": {
                    "type": "number",
                    "description": (
                        f"Fixed-rate safe-integer interval in seconds, at least "
                        f"{MIN_EVERY_INTERVAL_SECONDS}."
                    ),
                },
                "at": {
                    "description": (
                        "Absolute target as strict offset RFC 3339 or local date/time with an "
                        "explicit IANA zone."
                    ),
                    "oneOf": [
                        {"type": "string"},
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "date": {"type": "string"},
                                "time": {"type": "string"},
                                "time_zone": {"type": "string"},
                            },
                            "required": ["date", "time", "time_zone"],
                        },
                    ],
                },
            },
            "required": ["prompt"],
        },
        execute=execute,
        render=_render,
        present_call=lambda args: _present(
            "Create reminder", "other", args.get("prompt")),
    )


def _validate_create_args(args: dict) -> dict | None:
    keys = set(args)
    if any(key not in ("prompt", "after_seconds", "at", "every_seconds") for key in keys) \
            or sum(key in args for key in ("after_seconds", "at", "every_seconds")) != 1:
        return {
            "code": "invalid_selector",
            "message": "schedule_create accepts exactly one of after_seconds, at, or every_seconds.",
        }
    prompt = args.get("prompt", "")
    if not isinstance(prompt, str) or not prompt.strip():
        return {"code": "invalid_prompt", "message": "prompt must be non-empty after trimming."}
    after_seconds = args.get("after_seconds")
    if after_seconds is not None and (
            not isinstance(after_seconds, int) or isinstance(after_seconds, bool)
            or after_seconds <= 0 or after_seconds > 2**53 - 1):
        return {
            "code": "invalid_rule", "message": "after_seconds must be a positive safe integer."}
    every_seconds = args.get("every_seconds")
    if every_seconds is not None and (
            not isinstance(every_seconds, int) or isinstance(every_seconds, bool)
            or every_seconds > 2**53 - 1):
        return {"code": "invalid_rule", "message": "every_seconds must be a safe integer."}
    if every_seconds is not None and every_seconds < MIN_EVERY_INTERVAL_SECONDS:
        return {
            "code": "frequency_too_high",
            "message": f"every_seconds must be at least {MIN_EVERY_INTERVAL_SECONDS}.",
        }
    return None


def _create_operation(root_ctx, agent, exec_, args, on_durable_change):
    cancelled = _cancelled(exec_)
    if cancelled is not None:
        return cancelled
    uncertain = _preflight(root_ctx, agent, "create")
    if uncertain is not None:
        return uncertain
    _notify(on_durable_change, root_ctx)
    folded = _fold_for_tool(agent)
    if _is_tool_error(folded):
        return folded
    id_ = allocate_schedule_id(folded)
    try:
        if args.get("at") is not None:
            record = create_at_schedule_record(id_, args["prompt"], args["at"], int(time.time() * 1000))
        elif args.get("after_seconds") is not None:
            record = create_after_schedule_record(
                id_, args["prompt"], args["after_seconds"], int(time.time() * 1000))
        else:
            record = create_every_schedule_record(
                id_, args["prompt"], args["every_seconds"], int(time.time() * 1000))
    except ScheduleInputError as error:
        return {"code": error.code, "message": error.message}
    except BaseException:
        return {"code": "internal_error", "message": _INTERNAL_MESSAGE}
    if _cancelled(exec_) is not None:
        return _cancelled(exec_)
    try:
        agent.session.append("schedule/change", {
            "version": 1, "operation": "create", "schedule": record})
    except BaseException:
        return {"code": "internal_error", "message": _INTERNAL_MESSAGE}
    barrier = _preflight(root_ctx, agent, "create", id_)
    if barrier is not None:
        return barrier
    _notify(on_durable_change, root_ctx)
    return schedule_view(record, int(time.time() * 1000))


def _schedule_list_tool(root_ctx: Any, agent: Any, on_durable_change) -> Tool:
    async def execute(_args: dict, exec_: Any):
        if exec_.agent is not agent:
            return {"code": "internal_error", "message": _INTERNAL_MESSAGE}

        def operation():
            cancelled = _cancelled(exec_)
            if cancelled is not None:
                return cancelled
            uncertain = _preflight(root_ctx, agent, "list")
            if uncertain is not None:
                return uncertain
            _notify(on_durable_change, root_ctx)
            folded = _fold_for_tool(agent)
            if _is_tool_error(folded):
                return folded
            now = int(time.time() * 1000)
            return [schedule_view(record, now) for record in folded["active"]]

        return await run_schedule_transaction(agent, operation)

    return Tool(
        name="schedule_list",
        description=(
            "List every active reminder in the current session in creation order, including its "
            "exact id, UTC target, scheduled or overdue state, and session-local delivery mode."
        ),
        parameters={"type": "object", "properties": {}},
        execute=execute,
        render=_render,
        present_call=lambda _args: _present("List reminders", "read"),
    )


def _schedule_delete_tool(root_ctx: Any, agent: Any, on_durable_change) -> Tool:
    async def execute(args: dict, exec_: Any):
        raw_id = args.get("id")
        if not isinstance(raw_id, str) or not raw_id or raw_id.strip() != raw_id:
            return {
                "code": "invalid_rule",
                "message": "schedule_delete id must be non-empty without surrounding whitespace.",
            }
        if exec_.agent is not agent:
            return {"code": "internal_error", "message": _INTERNAL_MESSAGE}

        def operation():
            cancelled = _cancelled(exec_)
            if cancelled is not None:
                return cancelled
            uncertain = _preflight(root_ctx, agent, "delete", raw_id)
            if uncertain is not None:
                return uncertain
            _notify(on_durable_change, root_ctx)
            folded = _fold_for_tool(agent)
            if _is_tool_error(folded):
                return folded
            if not any(record["id"] == raw_id for record in folded["active"]):
                return {"id": raw_id, "deleted": False, "code": "schedule_not_found"}
            if _cancelled(exec_) is not None:
                return _cancelled(exec_)
            try:
                agent.session.append("schedule/change", {
                    "version": 1, "operation": "delete", "id": raw_id})
            except BaseException:
                return {"code": "internal_error", "message": _INTERNAL_MESSAGE}
            barrier = _preflight(root_ctx, agent, "delete", raw_id)
            if barrier is not None:
                return barrier
            _notify(on_durable_change, root_ctx)
            return {"id": raw_id, "deleted": True}

        return await run_schedule_transaction(agent, operation)

    return Tool(
        name="schedule_delete",
        description=(
            "Delete one active reminder in the current session by the exact id returned by "
            "schedule_create or schedule_list. Unknown or already-finished ids return deleted false."
        ),
        parameters={
            "type": "object",
            "properties": {
                "id": {"type": "string", "description": "Exact session-local schedule id."},
            },
            "required": ["id"],
        },
        execute=execute,
        render=_render,
        present_call=lambda args: _present("Delete reminder", "other", args.get("id")),
    )


# ---------- 辅助 ----------

def _fold_for_tool(agent):
    """仅在成功 preflight 后折叠，把损坏映射为稳定值（tools.ts foldForTool）。"""
    try:
        return fold_schedule_events(agent.session.own_events())
    except ScheduleLogError:
        return {"code": "corrupt_schedule_log", "message": _CORRUPT_MESSAGE}
    except BaseException:
        return {"code": "internal_error", "message": _INTERNAL_MESSAGE}


def _is_tool_error(value: Any) -> bool:
    """fold 结果是否错误而非重放状态（tools.ts isToolError：'code' in value）。"""
    return isinstance(value, dict) and "code" in value


def _cancelled(exec_: Any) -> dict | None:
    """等价上游 cancellationPlaceholder：FIFO 轮前已取消 → internal_error。"""
    if exec_.signal.is_set():
        return {"code": "internal_error", "message": _INTERNAL_MESSAGE}
    return None


def _preflight(root_ctx, agent, operation: str, id_: str | None = None) -> dict | None:
    try:
        flush_schedule_persistence(root_ctx, agent.session)
        return None
    except SchedulePersistenceError:
        out = {
            "code": "persistence_uncertain",
            "message": _PERSISTENCE_MESSAGE,
            "operation": operation,
        }
        if id_ is not None:
            out["id"] = id_
        return out


def _notify(on_durable_change, root_ctx) -> None:
    try:
        on_durable_change()
    except BaseException as error:
        logger = getattr(root_ctx, "logger", None)
        if logger is not None and hasattr(logger, "warn"):
            logger.warn(
                f"schedule: durable-change observer failed: "
                f"{getattr(error, 'message', None) or str(error)}")


def register_schedule_tools(root_ctx: Any, agent: Any, on_durable_change) -> Any:
    """在精确 agent scope 注册全部三个 Schedule 工具并返回幂等 aggregate disposer。

    @param root_ctx - 拥有 sessions 与持久化的全局上下文。
    @param agent - 精确 agent；其 scope 为注册载体（agent.tools ToolRegistry）。
    @param on_durable_change - 每次成功 preflight 再一次 create/delete barrier
        成功后调用（对齐 index.ts registerScheduleTools 的 durable-change 通知）。
    @returns 三个注册的 aggregate disposer。
    """
    reg = getattr(agent, "tools", None) or getattr(agent, "reg", None)
    if reg is None:
        raise RuntimeError("schedule tools require agent.tools (ToolRegistry)")

    def notify() -> None:
        _notify(on_durable_change, root_ctx)

    tools = [
        _schedule_create_tool(root_ctx, agent, notify),
        _schedule_list_tool(root_ctx, agent, notify),
        _schedule_delete_tool(root_ctx, agent, notify),
    ]
    disposers = [reg.register(tool) for tool in tools]

    def disposer() -> None:
        for dispose in reversed(disposers):
            dispose()

    return disposer