"""MCP 资源工具族：把可达 MCP server 的资源能力暴露为共享工具。

对应 dsh 真实源码：packages/mcp/mcp-resources。三个共享工具
（list_mcp_resources / list_mcp_resource_templates / read_mcp_resource）在
首个 server 注册时安装、最后一个移除时注销；per-agent scope 分层（上游
ResourceLayer ScopedLayers）以全局层 + opt-in 简化登记（见 verified-diffs）。
"""
from __future__ import annotations

from .runtime import McpResourceRuntime, install_mcp_resources  # noqa: F401
from .render import render_resource_result  # noqa: F401
from .tools import register_resource_tools  # noqa: F401

__all__ = ["McpResourceRuntime", "install_mcp_resources", "render_resource_result",
           "register_resource_tools"]