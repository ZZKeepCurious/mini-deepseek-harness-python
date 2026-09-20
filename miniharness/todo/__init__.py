"""待办清单域（对齐 packages/todo/tool-todo）。

- `TodoItem`：`{content, status}`（pending/in_progress/completed），整表替换无 id；
- `to_todo_list`：校验（trim 非空、内容唯一、非并行策略下至多一个 in_progress）；
- `fold_todos`：`todos` 投影折叠——最新 `todo/write` 胜出，`turn/start` 清空；
- `install_todo_tool`：注册模型工具 `todo_write`（append `todo/write` 到调用会话）。

载体差异（登记）：上游把投影单元注册进 `ctx.sessionProjections`（M7），mini 无投影注册表
——提供 `fold_todos` 纯函数供 wire/UI 现场折叠；`tool-todo` invariant 伴生（schema 校验）
不承载（mini 无 invariant 注册表）。
"""
from __future__ import annotations

from typing import Any

from ..core.scope import Context
from ..core.tools import Tool

__all__ = ["TODO_STATUSES", "fold_todos", "install_todo_tool", "to_todo_list"]

TODO_STATUSES = ("pending", "in_progress", "completed")

_DESCRIPTION_HEAD = (
    "Record and update a structured task list for the current work. Send the ENTIRE list every "
    "call — it REPLACES the previous list (there are no partial updates, no per-item edits). Use "
    "it to plan multi-step work and show progress: add one todo per concrete step before you "
    "start. ")
_DESCRIPTION_PARALLEL = (
    "Mark every todo being actively worked on `in_progress` — several at once when work genuinely "
    "runs in parallel, one for sequential work; while work remains, at least one task should be "
    "`in_progress`. ")
_DESCRIPTION_SINGLE = (
    "Keep AT MOST ONE todo `in_progress` at a time; while work remains, exactly one active task "
    "should be `in_progress`. ")
_DESCRIPTION_TAIL = (
    "Mark a todo `completed` the moment it is done (do not batch completions), and allow no "
    "`in_progress` item only once all work is complete. Statuses: `pending` (not started), "
    "`in_progress` (being worked on now), `completed` (finished).")


def _describe(allow_parallel: bool) -> str:
    return (_DESCRIPTION_HEAD
            + (_DESCRIPTION_PARALLEL if allow_parallel else _DESCRIPTION_SINGLE)
            + _DESCRIPTION_TAIL)


def to_todo_list(raw: Any, allow_parallel: bool) -> list[dict]:
    """校验并规范化整表：trim 非空、内容唯一、活跃数上限（对齐 toTodoList，措辞逐字）。"""
    if not isinstance(raw, list):
        raise ValueError("todos must be an array")
    todos: list[dict] = []
    seen: set = set()
    active = 0
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("each todo must be an object")
        content = str(item.get("content", "")).strip()
        if content == "":
            raise ValueError("invalid todo: `content` must be a non-empty string")
        if content in seen:
            raise ValueError(f"invalid todos: duplicate content {content!r}")
        seen.add(content)
        status = item.get("status")
        if status not in TODO_STATUSES:
            raise ValueError(f"invalid todo status: {status!r}")
        if status == "in_progress":
            active += 1
        todos.append({"content": content, "status": status})
    if not allow_parallel and active > 1:
        raise ValueError(
            f"invalid todos: at most one task may be in_progress (got {active})")
    return todos


def fold_todos(events: list) -> list[dict] | None:
    """`todos` 投影：最新 `todo/write` 整表，`turn/start` 清空；首次写前为 None。"""
    state: list[dict] | None = None
    for event in events:
        kind = event.get("type")
        if kind == "todo/write":
            state = list((event.get("data") or {}).get("todos") or [])
        elif kind == "turn/start":
            state = None
    return state


def install_todo_tool(ctx: Context, *, allow_parallel_in_progress: bool = True) -> Tool:
    """注册模型工具 `todo_write`（幂等）。"""
    registry = ctx.get("tools")
    if registry is None:
        raise RuntimeError("tool-todo: ctx.tools is required")
    if registry.resolve("todo_write") is not None:
        return registry.resolve("todo_write")
    allow_parallel = bool(allow_parallel_in_progress)

    def execute(args: dict, exec: Any) -> dict:
        todos = to_todo_list(args.get("todos"), allow_parallel)
        agent = getattr(exec, "agent", None)
        session = getattr(agent, "session", None)
        if session is None:
            raise RuntimeError("todo_write requires an owning agent session")
        session.append("todo/write", {"todos": todos})
        counts = {
            "pending": sum(1 for todo in todos if todo["status"] == "pending"),
            "inProgress": sum(1 for todo in todos if todo["status"] == "in_progress"),
            "completed": sum(1 for todo in todos if todo["status"] == "completed"),
        }
        return {"todos": todos, "counts": counts}

    tool = Tool(
        name="todo_write",
        description=_describe(allow_parallel),
        parameters={
            "type": "object",
            "properties": {
                "todos": {
                    "type": "array",
                    "description": "The COMPLETE task list, replacing any previous list.",
                    "items": {
                        "type": "object",
                        "properties": {
                            "content": {"type": "string", "description": "What the task is — a short imperative line."},
                            "status": {"type": "string", "enum": list(TODO_STATUSES)},
                        },
                        "required": ["content", "status"],
                    },
                },
            },
            "required": ["todos"],
        },
        execute=execute,
        render=lambda args, value: [{"type": "text", "text":
            f"Updated todo list: {value['counts']['pending']} pending, "
            f"{value['counts']['inProgress']} in progress, "
            f"{value['counts']['completed']} completed."}],
    )
    registry.register(tool)
    return tool
