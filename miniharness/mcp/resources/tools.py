"""共享资源工具定义（对齐上游 mcp-resources/src/tools.ts）。"""
from __future__ import annotations

from typing import Any, Callable

from ...core.scope import Context
from ...core.tools import Tool

__all__ = ["register_resource_tools"]

_SERVER_PARAM = {
    "type": "string",
    "required": True,
    "description": "Configured MCP server name.",
}
_CURSOR_PARAM = {
    "type": "string",
    "description": "Continuation cursor returned by this server.",
}

_LIST_PARAMETERS = {"server": _SERVER_PARAM, "cursor": _CURSOR_PARAM}
_READ_PARAMETERS = {
    "server": _SERVER_PARAM,
    "uri": {"type": "string", "required": True, "description": "Resource URI to read."},
}


def register_resource_tools(runtime: Any, ctx: Context) -> list[Callable]:
    """注册三个共享资源工具到 ctx tools registry；返回每个工具的注销 disposer。"""
    from ...core.dsh_scope import ScopedLayers  # noqa: F401 - 形态对齐预留

    registry = ctx.get("tools")
    if registry is None:
        raise RuntimeError("no tools registry is mounted")

    def make_tool(name: str, params: dict, build_request: Callable[[dict], dict]) -> Tool:
        def execute(args: dict, exec_: Any) -> Any:
            server = args.get("server")
            if not isinstance(server, str) or not server:
                raise RuntimeError("server is required and must name a configured MCP server")
            return runtime.request(server, build_request(args), exec_)

        def render(args: dict, value: Any) -> str:
            return render_resource_result(args.get("server"), value)

        return Tool(
            name=name,
            description=_DESCRIPTIONS[name],
            execute=execute,
            parameters=params,
            output={"schema": {"type": "json"}},
            render=render,
        )

    tools = [
        make_tool("list_mcp_resources", _LIST_PARAMETERS,
                  lambda args: {"method": "resources/list", **({"cursor": args["cursor"]} if args.get("cursor") is not None else {})}),
        make_tool("list_mcp_resource_templates", _LIST_PARAMETERS,
                  lambda args: {"method": "resources/templates/list", **({"cursor": args["cursor"]} if args.get("cursor") is not None else {})}),
        make_tool("read_mcp_resource", _READ_PARAMETERS,
                  lambda args: {"method": "resources/read", "uri": args["uri"]}),
    ]
    return [registry.register(tool) for tool in tools]


_DESCRIPTIONS = {
    "list_mcp_resources": "List the resources exposed by a connected MCP server.",
    "list_mcp_resource_templates": "List the resource templates exposed by a connected MCP server.",
    "read_mcp_resource": "Read one resource by URI from a connected MCP server.",
}


def render_resource_result(server: str, value: Any) -> str:
    """委托给 render 模块（避免循环导入的轻量再导出）。"""
    from .render import render_resource_result as _impl

    return _impl(server, value)