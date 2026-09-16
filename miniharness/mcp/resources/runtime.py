"""McpResourceRuntime：注册的 MCP server provider 生命周期 + 共享工具与节。

对应 dsh 真实源码：packages/mcp/mcp-resources/src/index.ts（ResourceLayer /
McpResourceRuntime）。上游 `register(server, provider)` 经 ctx.effect 生成器
先登记注销回调再替换工具代；mini 以引用计数等价物承载：首个 provider 注册时
安装 3 个共享工具 + `mcp-resources` 节，最后一个移除时全部注销。上游按
agent scope 分层（layers.merge(exec.agent, ...)）；mini 登记为全局层
（简化，见 verified-diffs）。
"""
from __future__ import annotations

import json
import threading
from typing import Any, Callable

from ...core.scope import Context
from ..server_context import MCP_SERVERS_ORDER

__all__ = ["McpResourceRuntime", "install_mcp_resources"]


class McpResourceRuntime:
    """已注册 MCP server 的 provider 生命周期 + 请求路由。"""

    def __init__(self, ctx: Context):
        self._ctx = ctx
        self._providers: dict[str, Any] = {}
        self._tool_disposers: list[Callable] | None = None
        self._section_disposer: Callable | None = None
        self._lock = threading.RLock()

    # ---------- 生命周期 ----------

    def register(self, server: str, provider: Any) -> Callable:
        """注册一个 server provider；第一个注册安装共享工具与节。

        @returns 注销 disposer（最后一个移除时卸载共享工具与节）。
        """
        with self._lock:
            if server in self._providers:
                raise RuntimeError(f"MCP server {server!r} is already registered")
            self._providers[server] = provider
            if self._tool_disposers is None:
                self._install_tools_and_section()

            def dispose() -> None:
                with self._lock:
                    self._providers.pop(server, None)
                    if self._tool_disposers is not None and not self._providers:
                        for d in reversed(self._tool_disposers):
                            d()
                        self._tool_disposers = None
                        if self._section_disposer is not None:
                            self._section_disposer()
                            self._section_disposer = None

            return dispose

    def _install_tools_and_section(self) -> None:
        from .tools import register_resource_tools

        from ...core.dsh_scope import scope_of  # noqa: F401 - 形态对齐预留

        self._tool_disposers = register_resource_tools(self, self._ctx)
        system_prompt = self._ctx.get("systemPrompt")
        if system_prompt is not None:
            self._section_disposer = system_prompt.section(
                "mcp-resources",
                MCP_SERVERS_ORDER,
                lambda context: json.dumps(sorted(self.server_names())),
                complete=False,
            )

    def server_names(self) -> list[str]:
        with self._lock:
            return sorted(self._providers.keys())

    # ---------- 请求路由 ----------

    def request(self, server: str, full_request: dict, exec_: Any) -> Any:
        """把资源操作路由到对应 server 的 provider。

        @param full_request - {method: 'resources/list'|'resources/templates/list'|'resources/read', ...}。
        """
        with self._lock:
            provider = self._providers.get(server)
        if provider is None:
            available = ", ".join(self.server_names()) or "none"
            raise RuntimeError(
                f'MCP server "{server}" is not registered; available servers: {available}')
        return provider.request(full_request, exec_)


def install_mcp_resources(ctx: Context) -> McpResourceRuntime:
    """装配 mcpResources 服务（重复安装 fail loud，对齐 provide 冲突语义）。"""
    runtime = McpResourceRuntime(ctx)
    ctx.provide("mcpResources", runtime)
    return runtime