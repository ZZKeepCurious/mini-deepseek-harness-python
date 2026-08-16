"""headless 默认工具集（教学扩展）。

上游无对应模块：headless 的工具经插件树配置（tools 插件注册），mini 未复现
插件装配路径，故以内置默认工具集收编（架构文档 §4.1 树中 cli/default_tools.py
一行即此文件；`_default_tools` 提为公开 `default_tools` 是迁移步骤 3 的约定）。
"""
from __future__ import annotations

from ..core.scope import Context
from ..core.tools import Tool, ToolRegistry

__all__ = ["default_tools"]


def default_tools(ctx: Context) -> ToolRegistry:
    reg = ToolRegistry(ctx)
    reg.register(Tool(
        name="bash",
        description="Run a shell command.",
        parameters={
            "type": "object",
            "properties": {"cmd": {"type": "string", "description": "command to run"}},
            "required": ["cmd"],
        },
        execute=lambda args, e: f"stdout: {args['cmd']}",
    ))
    return reg