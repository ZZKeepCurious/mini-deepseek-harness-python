"""模型可见 `lsp` 工具（对齐 packages/lsp/tool-lsp）。

一个只读工具，四种操作（goToDefinition/findReferences/goToImplementation/hover）：
把模型的一基 UTF-16 光标坐标转成 seam 的零基位置，要求会话工作区（无回退），封顶并渲染
结果，并携带可配置的超时预算。运行期只消费 `ctx.lsp` 与 `ctx.systemPrompt`，不 import
任何 provider。

载体差异（登记）：上游 `apply(ctx, config)` 经 `ctx.tools.register` 注册（插件 inject
tools/lsp/systemPrompt）；mini 以 `install_tool_lsp(ctx, registry, config)` 由
`default_tools` 在 `ctx.lsp` 在场时条件注册（同 web/jobs/skills 先例）。
"""
from __future__ import annotations

from typing import Any

from ..core.scope import Context
from ..core.tools import Tool, ToolRegistry
from ..lsp import LSP_OPERATIONS, LspError
from .render import (
    DEFAULT_MAX_LOCATIONS,
    DEFAULT_MAX_RESULT_CHARS,
    format_hover,
    format_locations,
    parse_lsp_args,
    present_lsp_call,
)

__all__ = [
    "DEFAULT_LSP_TOOL_TIMEOUT_MS",
    "LSP_PROMPT_TEXT",
    "TOOL_LSP_SECTION_ORDER",
    "create_lsp_tool",
    "install_tool_lsp",
]

#: 工具调用超时预算（ms），覆盖排队的 open/查询/close 生命周期。
DEFAULT_LSP_TOOL_TIMEOUT_MS = 60_000
#: `tool:lsp` system prompt 节的顺序（上游 SECTION_ORDERS.TOOL_LSP = 2200）。
TOOL_LSP_SECTION_ORDER = 2200

#: 把 LSP 定位为精确助手的稳定 system-prompt 指引（逐字对齐上游）。
LSP_PROMPT_TEXT = (
    "Use search/read for ordinary navigation. Use lsp when textual matches are "
    "ambiguous or before a change requires precise definitions, implementations, "
    "or references. Positions are one-based line and character (UTF-16) at the "
    "cursor; an off-symbol position may return no results. findReferences always "
    "includes the declaration."
)

_MAX_TIMER_DELAY_MS = 2_147_483_647

_LSP_POSITION_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "line": {"type": "integer"},
        "character": {"type": "integer"},
    },
    "required": ["line", "character"],
}

_LSP_RANGE_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "start": {**_LSP_POSITION_OUTPUT_SCHEMA},
        "end": {**_LSP_POSITION_OUTPUT_SCHEMA},
    },
    "required": ["start", "end"],
}

_LSP_OUTPUT_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"type": "string", "const": "locations"},
                "locations": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "uri": {"type": "string"},
                            "range": {**_LSP_RANGE_OUTPUT_SCHEMA},
                        },
                        "required": ["uri", "range"],
                    },
                },
                "resolvedWorkspaceUri": {"type": "string"},
            },
            "required": ["kind", "locations", "resolvedWorkspaceUri"],
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"type": "string", "const": "hover"},
                "hover": {
                    "oneOf": [
                        {"type": "null"},
                        {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "contents": {"type": "string"},
                                "range": {**_LSP_RANGE_OUTPUT_SCHEMA},
                            },
                            "required": ["contents"],
                        },
                    ],
                },
            },
            "required": ["kind", "hover"],
        },
    ],
}


def _resolve_config(config: dict | None) -> dict:
    cfg = dict(config or {})
    resolved = {
        "maxLocations": cfg.get("maxLocations", DEFAULT_MAX_LOCATIONS),
        "maxResultChars": cfg.get("maxResultChars", DEFAULT_MAX_RESULT_CHARS),
        "timeoutMs": cfg.get("timeoutMs", DEFAULT_LSP_TOOL_TIMEOUT_MS),
    }
    for name in ("maxLocations", "maxResultChars"):
        value = resolved[name]
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"tool-lsp: {name} must be a positive integer")
    timeout = resolved["timeoutMs"]
    if (not isinstance(timeout, int) or isinstance(timeout, bool)
            or timeout < 1 or timeout > _MAX_TIMER_DELAY_MS):
        raise ValueError(
            f"tool-lsp: timeoutMs must be a positive integer no greater than "
            f"{_MAX_TIMER_DELAY_MS}")
    return resolved


def _session_cwd(exec_: Any) -> str | None:
    """调用方 agent 的会话工作区 cwd；非 agent 调用方 → None。"""
    agent = getattr(exec_, "agent", None)
    session = getattr(agent, "session", None) if agent is not None else None
    if session is None:
        return None
    meta = getattr(session, "meta", None)
    if isinstance(meta, dict):
        cwd = meta.get("cwd")
        return cwd if isinstance(cwd, str) and cwd else None
    return None


def create_lsp_tool(ctx: Context, config: dict | None = None) -> Tool:
    """构造 `lsp` 工具（不注册）。"""
    resolved = _resolve_config(config)

    async def execute(args: dict, exec_: Any) -> dict:
        parsed = parse_lsp_args(dict(args))
        workspace_root = _session_cwd(exec_)
        if workspace_root is None:
            raise LspError("the lsp tool requires a session workspace cwd",
                           "LSP_WORKSPACE_REQUIRED")
        lsp = ctx.get("lsp")
        if lsp is None:
            raise RuntimeError("the lsp tool requires the ctx.lsp capability seam")
        result = await lsp.query({
            "operation": parsed["operation"],
            "filePath": parsed["filePath"],
            "position": parsed["position"],
            "workspaceRoot": workspace_root,
        }, getattr(exec_, "signal", None))
        if result["kind"] == "locations":
            return {
                "kind": "locations",
                "locations": [
                    {"uri": location["uri"],
                     "range": {
                         "start": {"line": location["range"]["start"]["line"],
                                   "character": location["range"]["start"]["character"]},
                         "end": {"line": location["range"]["end"]["line"],
                                 "character": location["range"]["end"]["character"]},
                     }}
                    for location in result["locations"]
                ],
                "resolvedWorkspaceUri": result["resolvedWorkspaceUri"],
            }
        hover = result["hover"]
        if hover is None:
            return {"kind": "hover", "hover": None}
        canonical: dict = {"contents": hover["contents"]}
        if "range" in hover:
            canonical["range"] = {
                "start": {"line": hover["range"]["start"]["line"],
                          "character": hover["range"]["start"]["character"]},
                "end": {"line": hover["range"]["end"]["line"],
                        "character": hover["range"]["end"]["character"]},
            }
        return {"kind": "hover", "hover": canonical}

    def render(args: dict, value: dict) -> list[dict]:
        if value["kind"] == "locations":
            return [{"type": "text", "text": format_locations(
                value["locations"], value["resolvedWorkspaceUri"],
                resolved["maxLocations"], resolved["maxResultChars"])}]
        return [{"type": "text", "text": format_hover(
            value["hover"], resolved["maxResultChars"])}]

    return Tool(
        name="lsp",
        description=(
            "Query a language server for precise code navigation. operation is "
            "one of goToDefinition, findReferences, goToImplementation, hover. "
            "line and character are one-based UTF-16 cursor coordinates. "
            "findReferences includes the declaration."
        ),
        parameters={
            "type": "object",
            "properties": {
                "operation": {
                    "type": "string",
                    "enum": list(LSP_OPERATIONS),
                    "description": "goToDefinition, findReferences, "
                                   "goToImplementation, or hover.",
                },
                "file_path": {
                    "type": "string",
                    "description": "The source file to query, relative to the "
                                   "workspace or absolute.",
                },
                "line": {"type": "number", "description": "One-based line of the cursor."},
                "character": {"type": "number",
                              "description": "One-based UTF-16 column of the cursor."},
            },
            "required": ["operation", "file_path", "line", "character"],
        },
        output={"schema": _LSP_OUTPUT_SCHEMA},
        execute=execute,
        render=render,
        present_call=present_lsp_call,
        timeout_ms=resolved["timeoutMs"],
    )


def install_tool_lsp(ctx: Context, registry: ToolRegistry,
                     config: dict | None = None) -> Tool | None:
    """把 `lsp` 工具注册进 `registry` 并注册 system prompt 节（幂等）。

    仅当 `ctx.lsp` 在场时注册；否则返回 None（无 provider seam 则工具不可用）。
    """
    if ctx.get("lsp") is None:
        return None
    if registry.resolve("lsp") is not None:
        return registry.resolve("lsp")
    tool = create_lsp_tool(ctx, config)
    registry.register(tool)
    system_prompt = ctx.get("systemPrompt")
    if system_prompt is not None and not _section_registered(system_prompt):
        system_prompt.section("tool:lsp", TOOL_LSP_SECTION_ORDER, LSP_PROMPT_TEXT)
    return tool


def _section_registered(system_prompt: Any) -> bool:
    sections = getattr(system_prompt, "_sections", None)
    if not isinstance(sections, list):
        return False
    return any(entry.get("name") == "tool:lsp" for entry in sections)
