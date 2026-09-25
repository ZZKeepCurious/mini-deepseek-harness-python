"""headless 默认工具集（教学扩展）。

上游无对应模块：headless 的工具经插件树配置（tools 插件注册），mini 未复现
插件装配路径，故以内置默认工具集收编（架构文档 §4.1 树中 cli/default_tools.py
一行即此文件；`_default_tools` 提为公开 `default_tools` 是迁移步骤 3 的约定）。

真实 bash 执行器在场时收编 tool_bash 的真实工具（job 集成 / promoteOnTimeout /
沙箱三路归因）；无 shell 服务时保持教学 stub。
"""
from __future__ import annotations

from ..core.scope import Context
from ..core.tools import Tool, ToolRegistry

__all__ = ["default_tools"]


def default_tools(ctx: Context) -> ToolRegistry:
    reg = ToolRegistry(ctx)
    shell = ctx.get("shell")
    if shell is not None:
        # 真实 bash 执行器已装配（上游 tool-bash 消费者角色）：逐调用决议沙箱
        # 策略；jobs 在场时前台超时提升为后台作业。install_tool_bash 顺带确保
        # ctx.shellEnv 服务在场并提供托管 DSH_* 快照。
        from ..tool_bash import install_tool_bash
        install_tool_bash(ctx)
    else:
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
    jobs = ctx.get("jobs")
    if jobs is not None:
        from ..jobs import register_job_tools
        register_job_tools(reg, jobs)
    # ctx.skills 服务存在时收编 `skill` 工具（catalog/手势注入已由 install_skills 接线）
    skills = ctx.get("skills")
    if skills is not None:
        from ..skills import register_skill_tools
        register_skill_tools(reg, skills)
    # ctx.web 服务存在时收编 web_search/web_fetch 二工具 + 对应 prompt 节
    # （install_web 已装配 provider；节需 systemPrompt 服务，见 register_web_tools）
    web = ctx.get("web")
    if web is not None:
        from ..web_tools import register_web_tools
        register_web_tools(reg, web, ctx.get("systemPrompt"))
    return reg
