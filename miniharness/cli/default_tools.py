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
    # ctx.jobs 服务存在时收编后台作业三工具（job_output/job_list/job_kill）
    try:
        jobs = ctx.inject("jobs")
    except KeyError:
        jobs = None
    if jobs is not None:
        from ..jobs import register_job_tools
        register_job_tools(reg, jobs)
    # ctx.skills 服务存在时收编 `skill` 工具（catalog/手势注入已由 install_skills 接线）
    try:
        skills = ctx.inject("skills")
    except KeyError:
        skills = None
    if skills is not None:
        from ..skills import register_skill_tools
        register_skill_tools(reg, skills)
    return reg