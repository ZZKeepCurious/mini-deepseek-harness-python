"""模型侧会话检索工具（对齐 packages/session-query/tool-session-query）。

五工具：`session_search`（跨会话最佳命中）、`session_event_search`（会话内事件命中）、
`session_trace`（世系）、`session_event_trace`（某事件的替换/来源关系）、
`session_event_read`（原始事件窗口）。

载体差异（登记）：上游按调用方 workspace/授权作用域收敛可查会话（workspace-access +
sessionProjections）；mini 无 workspace 实体（M5），工具按服务全局可查——调用方 scope 由
宿主另行约束。`systemPrompt.section` 引导未承载（mini systemPrompt 无该面）。
"""
from __future__ import annotations

from typing import Any

from ..core.scope import Context
from ..core.tools import Tool

__all__ = ["install_session_query_tools"]

DEFAULT_MAX_RESULTS = 20


def _session_id_of(exec: Any) -> str | None:
    agent = getattr(exec, "agent", None)
    session = getattr(agent, "session", None)
    return getattr(session, "session_id", None)


def _render_items(title: str, items: list, fmt) -> str:
    lines = [title]
    for item in items:
        lines.append(fmt(item))
    return "\n".join(lines)


def install_session_query_tools(ctx: Context, service: Any = None, *,
                                max_search_results: int = DEFAULT_MAX_RESULTS) -> dict:
    registry = ctx.get("tools")
    if registry is None:
        raise RuntimeError("tool-session-query: ctx.tools is required")
    svc = service or ctx.get("sessionQuery")
    if svc is None:
        raise RuntimeError("tool-session-query: ctx.sessionQuery is required")

    def add(tool: Tool) -> None:
        if registry.resolve(tool.name) is None:
            registry.register(tool)

    add(Tool(
        name="session_search",
        description=("Search prior sessions and return the strongest matching event from "
                     "each session."),
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Literal full-text query over prior session history."},
                "session_ids": {"type": "array", "items": {"type": "string"}},
                "cwd": {"type": "string", "description": "Restrict to sessions in this working directory."},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
        execute=lambda args, exec: _run(svc.search, {
            "query": args.get("query"), "limit": args.get("limit") or max_search_results,
            "sessionFilters": _session_filters(args),
        }),
        render=lambda args, value: [{"type": "text", "text": _render_items(
            f"session_search({args.get('query')!r}):",
            value["items"],
            lambda hit: (f"- {hit['header'].get('id')} "
                         f"[{hit['bestMatch']['type']}#{hit['bestMatch']['seq']}] "
                         f"{hit['bestMatch']['snippet']}"))}],
    ))

    add(Tool(
        name="session_event_search",
        description="Search prior events in one session.",
        parameters={
            "type": "object",
            "properties": {
                "session_id": {"type": "string", "description": "Target session id. Omit for the current session."},
                "query": {"type": "string"},
                "event_types": {"type": "array", "items": {"type": "string"}},
                "surface": {"type": "array", "items": {"type": "string", "enum": ["current", "shadowed", "log-only"]}},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
        execute=lambda args, exec: _run(svc.search_events, {
            "sessionId": args.get("session_id") or _session_id_of(exec),
            "query": args.get("query"), "limit": args.get("limit") or max_search_results,
            "filters": _event_filters(args),
        }),
        render=lambda args, value: [{"type": "text", "text": _render_items(
            f"session_event_search({args.get('query')!r}):",
            value["items"],
            lambda hit: f"- {hit['type']}#{hit['seq']} [{hit['surface']}] {hit['snippet']}")}],
    ))

    add(Tool(
        name="session_event_read",
        description="Read one full unabridged event and optional neighboring raw events.",
        parameters={
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "seq": {"type": "integer"},
                "before": {"type": "integer"},
                "after": {"type": "integer"},
            },
            "required": ["seq"],
        },
        execute=lambda args, exec: _run(svc.read_event, {
            "sessionId": args.get("session_id") or _session_id_of(exec),
            "seq": args.get("seq"), "before": args.get("before"), "after": args.get("after"),
        }),
        render=lambda args, value: [{"type": "text", "text":
            f"event {value['target'].get('type')}#{value['target'].get('seq')} "
            f"(window {value['startSeq']}-{value['endSeq']}):\n"
            + "\n".join(str(event) for event in value["events"])}],
    ))

    add(Tool(
        name="session_event_trace",
        description="Read every direct replacement and relationship to a cited source event.",
        parameters={
            "type": "object",
            "properties": {"session_id": {"type": "string"}, "seq": {"type": "integer"}},
            "required": ["seq"],
        },
        execute=lambda args, exec: _run(svc.trace_event, {
            "sessionId": args.get("session_id") or _session_id_of(exec),
            "seq": args.get("seq"),
        }),
        render=lambda args, value: [{"type": "text", "text":
            f"event #{value['target']['seq']}: sources={value['sourceEventSeqs']} "
            f"derived={value['derivedEventSeqs']} replaced={value['replacedEventSeqs']}"}],
    ))

    add(Tool(
        name="session_trace",
        description="Read the session lineage around one session (ancestors and descendants).",
        parameters={
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
        },
        execute=lambda args, exec: _run(svc.lineage, {
            "sessionId": args.get("session_id") or _session_id_of(exec)}),
        render=lambda args, value: [{"type": "text", "text":
            f"lineage of {value['target']['header'].get('id')}: "
            f"ancestors={[a['header'].get('id') for a in value['ancestors']]} "
            f"descendants={[d['session']['header'].get('id') for d in value['descendants']]} "
            f"complete={value['complete']}"}],
    ))
    return {name: registry.resolve(name) for name in (
        "session_search", "session_event_search", "session_event_read",
        "session_event_trace", "session_trace")}


def _run(handler, request: dict) -> Any:
    return handler(request)


def _session_filters(args: dict) -> list:
    filters: list = []
    if args.get("session_ids"):
        filters.append({"kind": "id", "values": list(args["session_ids"])})
    if args.get("cwd"):
        filters.append({"kind": "cwd", "values": [args["cwd"]]})
    if args.get("parent_session_ids"):
        filters.append({"kind": "parent", "values": list(args["parent_session_ids"])})
    return filters


def _event_filters(args: dict) -> list:
    filters: list = []
    if args.get("event_types"):
        filters.append({"kind": "type", "values": list(args["event_types"])})
    if args.get("surface"):
        filters.append({"kind": "surface", "values": list(args["surface"])})
    return filters
