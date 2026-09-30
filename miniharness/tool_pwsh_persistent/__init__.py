"""模型可见持久 `pwsh` 工具（对齐 packages/shell/tool-pwsh-persistent）。

经 owner 作用域的持久 PTY 会话运行 PowerShell 命令：shell 状态跨调用保持。共享
引擎在 `persistent_shell/`（pwsh 方言：`Invoke-Expression` 包装 + PSReadLine 回显
剥离）。**opt-in**：与 `tool_pwsh` 的同名 `pwsh` 工具互为替代，不由 `default_tools`
自动收编。
"""
from __future__ import annotations

from typing import Any

from ..core.tools import Tool, ToolRegistry
from ..persistent_shell import PWSH_DIALECT, install_persistent_tool

__all__ = ["create_persistent_pwsh", "install_persistent_pwsh"]


def create_persistent_pwsh(ctx: Any, config: dict | None = None) -> Tool:
    """构造持久 `pwsh` 工具（不注册）。"""
    from ..persistent_shell import create_persistent_tool

    return create_persistent_tool(ctx, PWSH_DIALECT, config)


def install_persistent_pwsh(ctx: Any, registry: ToolRegistry,
                            config: dict | None = None) -> Tool | None:
    """把持久 `pwsh` 工具注册进 `registry`（幂等）。"""
    return install_persistent_tool(ctx, registry, PWSH_DIALECT, config)
