"""模型可见持久 `bash` 工具（对齐 packages/shell/tool-bash-persistent）。

经 owner 作用域的持久 PTY 会话运行命令：shell 状态（当前目录、导出环境变量）跨
调用保持。共享引擎在 `persistent_shell/`（bash/pwsh 方言面）。**opt-in**：与
`tool_bash` 的同名 `bash` 工具互为替代（组合按需挂载其一），不由 `default_tools`
自动收编。
"""
from __future__ import annotations

from typing import Any

from ..core.tools import Tool, ToolRegistry
from ..persistent_shell import BASH_DIALECT, install_persistent_tool

__all__ = ["create_persistent_bash", "install_persistent_bash"]


def create_persistent_bash(ctx: Any, config: dict | None = None) -> Tool:
    """构造持久 `bash` 工具（不注册）。"""
    from ..persistent_shell import create_persistent_tool

    return create_persistent_tool(ctx, BASH_DIALECT, config)


def install_persistent_bash(ctx: Any, registry: ToolRegistry,
                            config: dict | None = None) -> Tool | None:
    """把持久 `bash` 工具注册进 `registry`（幂等）。"""
    return install_persistent_tool(ctx, registry, BASH_DIALECT, config)
