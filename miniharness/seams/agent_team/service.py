"""Agent Teams 服务门面：roster / mailbox / task / activity / lifecycle 归并
（对齐上游 index.ts TeamService，去掉 Remote/Typert wire 层）。

mini 载体差异（verified-diffs §2.29）：
  * 上游 TeamService extends TypertRemoteService：Remote view/createTask/updateTask
    端点与 taskMutationResult 结果包装属 wire 层，mini 同步服务门面不承载（及其
    `team-task-conflict` / `team-rejected` 码）。模型侧结果由 tools.py 直接返回
    canonical 值；错误语义仍由 TeamError.code 闭集表达。
  * 上游 ctx.on/ctx.effect 生命周期接线在 install_agent_team 装配；本类只做
    同步编排与事务。乐观 in-flight 簿记（pendingCreations/pendingDispatches）
    由 teamwork 各 owner 自我管理，dispose 时经 settle 收口。
"""

from __future__ import annotations

from typing import Any

from ...core.agent_loop.agent import AgentLoop
from .activity import TeamActivity, TeamWaitResult
from .error import TeamError
from .lifecycle import TeamRuntimeLifecycle
from .mailbox import TeamMailbox
from .roster import TeamRoster
from .task_board import TeamTaskBoard
from .types import (
    Config,
    CreateTeamTaskRequest,
    SendTeamMessageRequest,
    SendTeamMessageResult,
    SpawnTeammateRequest,
    SpawnTeammateResult,
    TeamMemberView,
    TeamTaskView,
    TeamView,
    UpdateTeamTaskRequest,
)

__all__ = ["TeamService", "DEFAULT_CONFIG", "positive_limit"]

DEFAULT_MAX_MEMBERS = 8
DEFAULT_MAX_TASKS = 256
DEFAULT_MAX_PENDING_MESSAGES = 64
DEFAULT_MAX_MESSAGE_BYTES = 65_536
DEFAULT_DISPOSAL_TIMEOUT_MS = 5_000

DEFAULT_CONFIG = {
    "maxMembers": DEFAULT_MAX_MEMBERS,
    "maxTasks": DEFAULT_MAX_TASKS,
    "maxPendingMessagesPerMember": DEFAULT_MAX_PENDING_MESSAGES,
    "maxMessageBytes": DEFAULT_MAX_MESSAGE_BYTES,
    "disposalTimeoutMs": DEFAULT_DISPOSAL_TIMEOUT_MS,
}


def positive_limit(name: str, value: int) -> int:
    """校验一个正的安全整数部署上限（对齐上游 positiveLimit）。"""
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise TeamError(
            f"{name} must be a positive safe integer", "TEAM_INVALID_CONFIG")
    return value


class TeamService:
    """以 Team Lead 日志为权威的 Agent Teams 服务门面。"""

    def __init__(
        self,
        agents: Any,
        sessions: Any,
        persistence: Any,
        manager: Any,
        activity: TeamActivity,
        lifecycle: TeamRuntimeLifecycle,
        journal: Any,
        config: Config | None = None,
    ):
        cfg = config or Config()
        self.config: dict[str, int] = {
            "maxMembers": positive_limit("maxMembers",
                                         cfg.maxMembers or DEFAULT_MAX_MEMBERS),
            "maxTasks": positive_limit("maxTasks", cfg.maxTasks or DEFAULT_MAX_TASKS),
            "maxPendingMessagesPerMember": positive_limit(
                "maxPendingMessagesPerMember",
                cfg.maxPendingMessagesPerMember or DEFAULT_MAX_PENDING_MESSAGES),
            "maxMessageBytes": positive_limit(
                "maxMessageBytes", cfg.maxMessageBytes or DEFAULT_MAX_MESSAGE_BYTES),
            "disposalTimeoutMs": positive_limit(
                "disposalTimeoutMs",
                cfg.disposalTimeoutMs or DEFAULT_DISPOSAL_TIMEOUT_MS),
        }
        self.activity = activity
        self.lifecycle = lifecycle
        self.journal = journal
        self.roster = TeamRoster(
            agents, sessions, persistence, manager, journal, lifecycle,
            self.config["maxMembers"],
        )
        self.mailbox = TeamMailbox(
            agents, sessions, persistence, manager, journal, self.roster, lifecycle,
            self.config["maxPendingMessagesPerMember"], self.config["maxMessageBytes"],
        )
        self.tasks = TeamTaskBoard(
            agents, journal, self.roster, self.config["maxTasks"],
        )

    # ---------- 身份 ----------

    def membership(self, agent: AgentLoop):
        return self.roster.membership(agent)

    def try_membership(self, agent: AgentLoop):
        return self.roster.try_membership(agent)

    # ---------- roster ----------

    def list_members(self, agent: AgentLoop) -> list[TeamMemberView]:
        membership = self.roster.membership(agent)
        return self.roster.list(membership)

    def spawn_teammate(
        self, caller: AgentLoop, request: SpawnTeammateRequest,
    ) -> SpawnTeammateResult:
        return self.roster.spawn(caller, request)

    async def spawn_teammate_async(
        self, caller: AgentLoop, request: SpawnTeammateRequest,
    ) -> SpawnTeammateResult:
        """事件循环内创建 teammate（异步工具/驱动载体）：投递与 checkpoint
        都让出控制，避免同步 time.sleep 卡死循环线程。"""
        return await self.roster.spawn_async(caller, request)

    def interrupt(self, caller: AgentLoop, target_name: str) -> dict:
        return self.roster.interrupt(caller, target_name)

    # ---------- mailbox ----------

    def send_message(
        self, caller: AgentLoop, request: SendTeamMessageRequest,
    ) -> SendTeamMessageResult:
        return self.mailbox.send(caller, request)

    # ---------- task board ----------

    def create_task(self, caller: AgentLoop, request: CreateTeamTaskRequest) -> TeamTaskView:
        membership = self.roster.membership(caller)
        return self.tasks.create(
            membership, request.subject, request.description,
            request.blockedBy, request.writeScopes,
        )

    def get_task(self, caller: AgentLoop, task_id: str) -> TeamTaskView:
        membership = self.roster.membership(caller)
        return self.tasks.get(membership, task_id)

    def list_tasks(self, caller: AgentLoop) -> list[TeamTaskView]:
        membership = self.roster.membership(caller)
        return self.tasks.list(membership)

    def update_task(
        self, caller: AgentLoop, request: UpdateTeamTaskRequest,
    ) -> TeamTaskView:
        membership = self.roster.membership(caller)
        return self.tasks.update(caller, membership, request)

    # ---------- activity ----------

    def wait_for_change(
        self, caller: AgentLoop, timeout_ms: int,
    ) -> TeamWaitResult:
        membership = self.roster.membership(caller)
        return self.activity.wait_for(membership.id, timeout_ms)

    # ---------- view ----------

    def view(self, agent: AgentLoop) -> TeamView:
        return TeamView(
            members=self.list_members(agent),
            tasks=self.list_tasks(agent),
        )