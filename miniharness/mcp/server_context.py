"""server 上下文接线：mcp-resources provider + systemPrompt 指令节。

对应 dsh 真实源码：packages/mcp/mcp-client/src/server-context.ts。上游经
`ctx.inject(['mcpResources'])` + `ctx.inject(['systemPrompt'])` 强制要求两
服务存在；mini 以 opt-in（ctx.get）承载（缺服务则跳过，简化登记）。
"""
from __future__ import annotations

from ..core.scope import Context

__all__ = ["MCP_SERVERS_ORDER", "register_server_context"]

#: MCP 相关 system prompt 节的排序锚点（上游 system-prompt SECTION_ORDERS 的
#: MCP_SERVERS: 3100；mini 无 getSectionOrder，用字面量并登记简化）。
MCP_SERVERS_ORDER = 3100


def register_server_context(ctx: Context, server_name: str, handle: Any) -> Any:
    """把服务器上下文联进 ctx：资源 provider（若有）+ `mcp:{server}` 节。

    @param handle - McpServerConnection 句柄（instructions + resources_request）。
    @returns provider disposer（mcp-resources 已挂载时）。
    """
    provider_disposer = None
    resources = ctx.get("mcpResources")
    if resources is not None:
        provider_disposer = resources.register(server_name, _connection_provider(handle))
    system_prompt = ctx.get("systemPrompt")
    if system_prompt is not None:
        system_prompt.section(
            f"mcp:{server_name}",
            MCP_SERVERS_ORDER,
            lambda context: handle.instructions(),
            complete=False,
        )
    return provider_disposer


def _connection_provider(handle: Any) -> Any:
    """把连接句柄适配成 mcp-resources provider（request(method, ...)）。"""

    def request(full_request: dict, exec_: Any) -> Any:
        return handle.resources_request(full_request, exec_)

    return type("_ConnectionProvider", (), {"request": request})()