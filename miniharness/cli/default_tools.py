"""headless 默认工具集（教学扩展）。

上游无对应模块：headless 的工具经插件树配置（tools 插件注册），mini 未复现
插件装配路径，故以内置默认工具集收编（架构文档 §4.1 树中 cli/default_tools.py
一行即此文件；`_default_tools` 提为公开 `default_tools` 是迁移步骤 3 的约定）。
"""
from __future__ import annotations

from ..core.scope import Context
from ..core.tools import Tool, ToolRegistry

__all__ = ["default_tools", "bash_tool"]


def bash_tool(shell, policy_service=None) -> Tool:
    """真实 bash 工具（上游 tool-bash 消费者角色的 mini 形态）。

    execute 逐调用解析沙箱策略：调用方会话（exec_.agent.session）的 cwd
    即 workspace-write 边界，`sandbox/mode` 覆盖随之生效；无会话/无策略
    服务时回退部署决议（shell 内部再兜底）。结算文本附三路归因事实：
    runner 失败以 SandboxUnavailableError 抛出（命令没跑，fail loud），
    denial 与普通非零退出在结果中可区分。
    """
    def _format(result: dict, cmd: str) -> str | dict:
        sandbox = result.get("sandbox")
        parts = []
        if result.get("stdout"):
            parts.append(f"stdout: {result['stdout'].rstrip(chr(10))}")
        if result.get("stderr"):
            parts.append(f"stderr: {result['stderr'].rstrip(chr(10))}")
        exit_code = result.get("exitCode")
        if sandbox is not None:
            enforcement = (f" enforcement={sandbox['enforcement']}"
                           if "enforcement" in sandbox else "")
            parts.append(f"[sandbox mode={sandbox['mode']}{enforcement}"
                         f" denied={str(sandbox['denied']).lower()}]")
        if exit_code:
            parts.append(f"exit code: {exit_code}")
        text = "\n".join(parts) if parts else f"(no output; exit {exit_code})"
        # 普通非零退出是正常结算（模型自行判读）；仅 denial 是工具级错误
        if bool(sandbox and sandbox.get("denied")):
            return {"content": text, "isError": True,
                    "error": f"sandbox denied command: {cmd}"}
        return text

    def execute(args: dict, exec_) -> str | dict:
        agent = getattr(exec_, "agent", None)
        session = getattr(agent, "session", None)
        request: dict = {"command": args["cmd"]}
        if policy_service is not None:
            request["sandboxPolicy"] = policy_service.resolve(
                {"session": session} if session is not None else {})
        return _format(shell.run(request), args["cmd"])

    return Tool(
        name="bash",
        description="Run a shell command.",
        parameters={
            "type": "object",
            "properties": {"cmd": {"type": "string", "description": "command to run"}},
            "required": ["cmd"],
        },
        execute=execute,
    )


def default_tools(ctx: Context) -> ToolRegistry:
    reg = ToolRegistry(ctx)
    shell = ctx.get("shell")
    if shell is not None:
        # 真实 bash 执行器已装配（上游 tool-bash 消费 ctx.shell 的角色）：
        # 逐调用以调用方会话决议沙箱策略，结算报告三路归因事实
        reg.register(bash_tool(shell, ctx.get("sandboxPolicy")))
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
    return reg