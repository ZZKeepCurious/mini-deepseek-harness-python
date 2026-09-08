"""Agent Teams 可选用 seam：Install 装配 + 事件接线 + 公共导出。

用法（产品化组装，root Lead 的 continuation manager 须以该 Lead 为 parent）：
    install_agent_team(ctx, manager, config={...})        # 提供服务 + 事件接线
    install_agent_team_tools(agent.ctx, agent.reg, service)  # 每成员 scope 安装工具

对齐上游：
  * 服务门面（TeamService）等价上游 agent-team/src/index.ts TeamService（去 Remote）；
  * 事件接线（index.ts:110-127）：session/event 观察 mailbox ack、agent/session-start
    recover、agent/status → activity notify、agent/created|disposed 安装/拆除 scope 工具。
  * 工具 + team:policy 节（tool-agent-team/src/index.ts）。
"""

from __future__ import annotations

from typing import Any

from .activity import TeamActivity
from .error import TeamError, error_message
from .journal import TeamJournal
from .lifecycle import TeamRuntimeLifecycle
from .service import DEFAULT_CONFIG, TeamService
from .types import Config

__all__ = [
    "TeamError",
    "error_message",
    "TeamService",
    "TeamActivity",
    "TeamJournal",
    "TeamRuntimeLifecycle",
    "ServiceKey",
    "install_agent_team",
]

ServiceKey = "agentTeams"


def install_agent_team(
    ctx: Any,
    manager: Any,
    config: Config | None = None,
) -> TeamService:
    """装配 Agent Teams 服务：提供 ctx.agentTeams、接线生命周期事件、对已在位
    与新增成员安装 scope 工具与恢复。

    @param manager - 以 Team Lead 为 parent 的 SubagentContinuationManager
        （teammate 冷恢复/续跑/steering 的载体）；注入代理层在装配处选择。
    @param config - Team 部署上限（缺省取 DEFAULT_CONFIG）。
    @returns 已装配的 TeamService。
    """
    existing = ctx.get(ServiceKey)
    if existing is not None:
        return existing

    agents = ctx.get("agents")
    sessions = ctx.get("sessions")
    persistence = ctx.get("sessionPersistence")
    if persistence is None:
        persistence = getattr(manager, "persistence", None)
    if agents is None or sessions is None or persistence is None:
        raise RuntimeError(
            "install_agent_team requires ctx.agents, ctx.sessions, and a "
            "sessionPersistence service (or a continuation manager with .persistence)"
        )

    activity = TeamActivity()
    lifecycle = TeamRuntimeLifecycle(
        (config.disposalTimeoutMs if config and config.disposalTimeoutMs else DEFAULT_CONFIG["disposalTimeoutMs"])
    )
    journal = TeamJournal(sessions, lambda root: activity.wake(root.id))

    service = TeamService(
        agents, sessions, persistence, manager, activity, lifecycle, journal, config,
    )
    ctx.provide(ServiceKey, service)

    def on_session_event(payload):
        service.mailbox.observe_session_event(payload.get("session"), payload.get("event"))

    def on_session_start(payload):
        agent = payload.get("agent")
        if agent is None:
            return
        _recover_for(service, agent)

    def on_status(payload):
        agent = payload.get("agent")
        if agent is None:
            return
        membership = service.try_membership(agent)
        if membership is not None:
            activity.wake(membership.id)

    def on_agent_created(payload):
        agent = payload.get("agent")
        if agent is None:
            return
        _maybe_install_tools(ctx, service, agent)

    ctx.on("session/event", on_session_event)
    ctx.on("agent/session-start", on_session_start)
    ctx.on("agent/status", on_status)
    ctx.on("agent/created", on_agent_created)

    for agent in agents.list():
        _recover_for(service, agent)
        _maybe_install_tools(ctx, service, agent)

    return service


def _recover_for(service, agent) -> None:
    """结算 provision-only 成员 + 重试其 pending mailbox（上游 recoverFor）。"""
    try:
        if service.try_membership(agent) is None:
            return
        service.roster.recover_for(agent)
        service.mailbox.recover_for(agent)
    except BaseException as error:  # noqa: BLE001 - 恢复失败保暖告警
        _warn(service, f"Agent Teams recovery for \"{agent.id}\" failed: {error_message(error)}")


def _maybe_install_tools(ctx, service, agent) -> None:
    """给一个 Team 成员 scope 安装模型侧工具 + team:policy 提示节。

    工具/api 都注册进 agent 自身的 scope（agent.reg / agent.system_prompt），
    随 agent scope 拆解自动注销（对齐上游 scoped tool effect 归属）；无需
    显式 agent/disposed 拆除。
    """
    if service.try_membership(agent) is None:
        return
    reg = getattr(agent, "tools", None) or getattr(agent, "reg", None)
    if reg is None:
        return
    from .tools import install_agent_team_tools
    try:
        install_agent_team_tools(agent.ctx, reg, service)
    except BaseException as error:  # noqa: BLE001 - scope 装配失败保暖
        _warn(service, f'Team tools install for "{agent.id}" failed: {error_message(error)}')


def _warn(service, message: str) -> None:
    logger = getattr(service, "logger", None)
    if getattr(logger, "warn", None) is not None:
        logger.warn(message)