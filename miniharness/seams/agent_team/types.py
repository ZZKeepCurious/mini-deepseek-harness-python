"""Agent Teams 公共类型：身份、durable 快照、视图、请求/结果（对齐上游 types.ts）。"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "DEFAULT_MAX_MEMBERS",
    "DEFAULT_MAX_TASKS",
    "DEFAULT_MAX_PENDING_MESSAGES",
    "DEFAULT_MAX_MESSAGE_BYTES",
    "DEFAULT_DISPOSAL_TIMEOUT_MS",
    "team_id",
    "team_task_id",
    "team_message_id",
    "generate_child_session_id",
    "generate_message_id",
    "TeamMemberPhase",
    "TeamTaskStatus",
    "TeamTaskAction",
    "TeamMemberSnapshot",
    "TeamMemberView",
    "TeamTaskSnapshot",
    "TeamTaskView",
    "TeamMessageSnapshot",
    "TeamMessageSource",
    "TeamMembership",
    "TeamView",
    "Config",
    "SpawnTeammateRequest",
    "SpawnTeammateResult",
    "SendTeamMessageRequest",
    "SendTeamMessageResult",
    "CreateTeamTaskRequest",
    "UpdateTeamTaskRequest",
]

DEFAULT_MAX_MEMBERS = 8
DEFAULT_MAX_TASKS = 256
DEFAULT_MAX_PENDING_MESSAGES = 64
DEFAULT_MAX_MESSAGE_BYTES = 65_536
DEFAULT_DISPOSAL_TIMEOUT_MS = 5_000

_TASK_NUMBER = re.compile(r"^task-(\d+)$")

TEAM_MESSAGE_PREFIX = "team-message-"


def team_id(id_: str) -> str:
    """brand 根 Session 身份为其 implicit Team 身份（同一字符串）。"""
    return id_


def team_task_id(id_: str) -> str:
    """brand 校验过的 task id。"""
    return id_


def team_message_id(id_: str) -> str:
    """brand 生成的 peer message id。"""
    return id_


def generate_child_session_id() -> str:
    """生成一个新 durable teammate 的 SessionId（上游 childId = randomUUID()）。"""
    return str(uuid.uuid4())


def generate_message_id() -> str:
    """生成一个新 durable peer 消息 id（上游 `team-message-${randomUUID()}`）。"""
    return f"{TEAM_MESSAGE_PREFIX}{uuid.uuid4()}"


def is_numeric_task_id(id_: str) -> bool:
    """`task-<n>` 且 n 为安全整数时返回 True（配合投影 nextTaskNumber 推进）。"""
    match = _TASK_NUMBER.match(id_)
    if match is None:
        return True
    return (1 << 53) - 1 >= int(match.group(1)) >= 0


TeamMemberPhase = Literal["provisioning", "active", "failed"]
TeamTaskStatus = Literal["pending", "in_progress", "completed", "deleted"]
TeamTaskAction = Literal[
    "claim", "release", "edit", "set_dependencies", "complete", "reopen", "reassign", "delete"
]


@dataclass(frozen=True)
class TeamMemberSnapshot:
    """durable teammate lifecycle 全量投标。"""

    id: str
    name: str
    description: str
    provider: str
    context: Literal["fresh", "fork"]
    phase: TeamMemberPhase
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "provider": self.provider,
            "context": self.context,
            "phase": self.phase,
        }
        if self.error is not None:
            out["error"] = self.error
        return out

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "TeamMemberSnapshot":
        return TeamMemberSnapshot(
            id=data["id"],
            name=data["name"],
            description=data["description"],
            provider=data["provider"],
            context=data["context"],
            phase=data["phase"],
            error=data.get("error"),
        )


@dataclass(frozen=True)
class TeamTaskSnapshot:
    """durable task 全量投标；每次突变 revision +1。"""

    id: str
    revision: int
    subject: str
    description: str
    status: TeamTaskStatus
    blockedBy: tuple[str, ...] = ()
    writeScopes: tuple[str, ...] = ()
    ownerId: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "revision": self.revision,
            "subject": self.subject,
            "description": self.description,
            "status": self.status,
            "blockedBy": list(self.blockedBy),
            "writeScopes": list(self.writeScopes),
        }
        if self.ownerId is not None:
            out["ownerId"] = self.ownerId
        return out

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "TeamTaskSnapshot":
        return TeamTaskSnapshot(
            id=data["id"],
            revision=data["revision"],
            subject=data["subject"],
            description=data["description"],
            status=data["status"],
            blockedBy=tuple(data.get("blockedBy") or ()),
            writeScopes=tuple(data.get("writeScopes") or ()),
            ownerId=data.get("ownerId"),
        )


@dataclass(frozen=True)
class TeamMessageSnapshot:
    """one peer message retained until its target Session records it。"""

    id: str
    senderId: str
    senderName: str
    targetId: str
    content: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "senderId": self.senderId,
            "senderName": self.senderName,
            "targetId": self.targetId,
            "content": [dict(x) for x in self.content],
        }

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "TeamMessageSnapshot":
        return TeamMessageSnapshot(
            id=data["id"],
            senderId=data["senderId"],
            senderName=data["senderName"],
            targetId=data["targetId"],
            content=tuple(dict(x) for x in data.get("content") or ()),
        )


@dataclass(frozen=True)
class TeamMessageSource:
    """目标 Session 保留的 source，用于 durable mailbox 去重。"""

    kind: Literal["team-message"] = "team-message"
    teamId: str | None = None
    messageId: str | None = None
    senderId: str | None = None
    senderName: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "teamId": self.teamId,
            "messageId": self.messageId,
            "senderId": self.senderId,
            "senderName": self.senderName,
        }


@dataclass(frozen=True)
class TeamMembership:
    """调用者在 Team 内身份（root + role + name），由准确 live Agent 解析。

    root 为 exact live Team Lead AgentLoop（权威身份）。
    """

    root: Any
    root_id: str
    role: Literal["lead", "teammate"]
    name: str

    @property
    def id(self) -> str:
        """Team 身份（= root SessionId，对齐上游 TeamMembership.id）。"""
        return self.root_id


@dataclass(frozen=True)
class TeamMemberView:
    """运行时富化 roster 行。"""

    id: str
    name: str
    role: Literal["lead", "teammate"]
    status: Literal["running", "idle", "inactive", "provisioning", "failed"]
    diagnostics: tuple[str, ...] = ()
    description: str | None = None
    provider: str | None = None
    context: Literal["fresh", "fork"] | None = None
    model: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "name": self.name,
            "role": self.role,
            "status": self.status,
            "diagnostics": list(self.diagnostics),
        }
        if self.description is not None:
            out["description"] = self.description
        if self.provider is not None:
            out["provider"] = self.provider
        if self.context is not None:
            out["context"] = self.context
        if self.model is not None:
            out["model"] = self.model
        return out


@dataclass(frozen=True)
class TeamTaskView:
    """运行时富化 task 行（ownerName + ready + writeScopeWarnings）。"""

    id: str
    revision: int
    subject: str
    description: str
    status: TeamTaskStatus
    blockedBy: tuple[str, ...] = ()
    writeScopes: tuple[str, ...] = ()
    ready: bool = False
    writeScopeWarnings: tuple[str, ...] = ()
    ownerName: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out = {
            "id": self.id,
            "revision": self.revision,
            "subject": self.subject,
            "description": self.description,
            "status": self.status,
            "blockedBy": list(self.blockedBy),
            "writeScopes": list(self.writeScopes),
            "ready": self.ready,
            "writeScopeWarnings": list(self.writeScopeWarnings),
        }
        if self.ownerName is not None:
            out["ownerName"] = self.ownerName
        return out


@dataclass
class TeamView:
    """当前 roster + 非删除 task 快照（Remote view 返回值）。"""

    members: list[TeamMemberView]
    tasks: list[TeamTaskView]


@dataclass(frozen=True)
class Config:
    """Team 服务部署上限；None 取默认。"""

    maxMembers: int | None = None
    maxTasks: int | None = None
    maxPendingMessagesPerMember: int | None = None
    maxMessageBytes: int | None = None
    disposalTimeoutMs: int | None = None


@dataclass(frozen=True)
class SpawnTeammateRequest:
    """创建一位 durable teammate 的输入。"""

    name: str
    description: str
    prompt: tuple[dict[str, Any], ...]
    context: Literal["fresh", "fork"]
    provider: str


@dataclass(frozen=True)
class SpawnTeammateResult:
    """一位 teammate 到达 durable active 或 failed 边缘后返回。"""

    member: TeamMemberView


@dataclass(frozen=True)
class SendTeamMessageRequest:
    """一条 durable peer 消息的输入。"""

    target: str
    content: tuple[dict[str, Any], ...]


@dataclass(frozen=True)
class SendTeamMessageResult:
    """peer 消息进入 durable mailbox 后返回。"""

    messageId: str
    status: Literal["accepted", "queued"]


@dataclass(frozen=True)
class CreateTeamTaskRequest:
    """创建一条共享任务的输入。"""

    subject: str
    description: str
    blockedBy: tuple[str, ...] = ()
    writeScopes: tuple[str, ...] = ()


@dataclass(frozen=True)
class UpdateTeamTaskRequest:
    """Compare-and-set 突变一条共享任务。"""

    taskId: str
    expectedRevision: int
    action: TeamTaskAction
    subject: str | None = None
    description: str | None = None
    blockedBy: tuple[str, ...] | None = None
    writeScopes: tuple[str, ...] | None = None
    owner: str | None = None