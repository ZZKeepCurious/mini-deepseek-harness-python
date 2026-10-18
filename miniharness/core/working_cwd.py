"""会话当前工作目录的消费者面解析（零 I/O）。

上游各消费者以 `await ctx.workingDirectory.ensure(agent, signal)` / `get(session)`
取"有效工作目录"（会话日志里最后一条 `working-directory/change`，无则投影初值
header.cwd，再无则部署缺省）。mini 把这个读取收敛成一个同步、零文件系统 I/O 的
帮助函数：目录的有效性校验/恢复由 working-directory 服务的装配期 ensure 承担
（每次请求准入），消费者按同一份投影值解析相对路径与起始目录。

不 import working-directory 服务模块（避免 L1 引 L2）：按服务名 `workingDirectory`
经 `ctx.get` 鸭子读取；服务未装时回退会话 header（`session.meta['cwd']`）——
与"目录从未被本服务变更"等价。
"""
from __future__ import annotations

__all__ = ["working_directory_of"]


def working_directory_of(agent_or_session, ctx=None) -> str | None:
    """一个 agent/session 当前有效工作目录；无法解析时 None。

    @param agent_or_session - AgentLoop（带 `.ctx`/`.session`）或裸 Session。
    @param ctx - 裸 Session 时可显式给出的上下文（用于查找服务）。
    """
    session = getattr(agent_or_session, "session", agent_or_session)
    agent_ctx = getattr(agent_or_session, "ctx", None) or ctx
    service = agent_ctx.get("workingDirectory") if agent_ctx is not None else None
    if service is not None:
        return service.get(session)
    meta = getattr(session, "meta", None)
    return meta.get("cwd") if isinstance(meta, dict) else None
