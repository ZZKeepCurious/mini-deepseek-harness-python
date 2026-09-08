"""Agent Teams 投影：从已提交 session 事件按状态机折叠 TeamState（对齐上游 projection.ts）。

上游用通用 sessionProjections 注册表按 session/event 增量维护；mini **不建通用投影系统**，
改为按需对 Lead 会话自有团队事件折叠（载体差异，登记 verified-diffs）。折叠语义逐字段对齐
applyProjectionEvent：payload 校验、teamId 过滤、version 检查、phase 转移、revision 连续、
task 图、message 去重——损坏即抛错（fail-closed）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .task_graph import assert_task_graph_candidate
from .types import (
    TeamMemberSnapshot,
    TeamMessageSnapshot,
    TeamTaskSnapshot,
)

__all__ = ["TeamState", "empty_team_state", "is_team_event", "fold_team_state"]

TEAM_EVENT_TYPES = frozenset({
    "team/member",
    "team/task",
    "team/message/queued",
    "team/message/delivered",
})

TEAM_EVENT_VERSION = 2


def is_team_event(event: dict) -> bool:
    """是否属于 Team 域事件。"""
    return event.get("type") in TEAM_EVENT_TYPES


@dataclass
class TeamState:
    """由 durable Team 身份选出的当前团队状态（投影权威快照）。"""

    id: str
    members: list[TeamMemberSnapshot] = field(default_factory=list)
    tasks: list[TeamTaskSnapshot] = field(default_factory=list)
    messages: list[TeamMessageSnapshot] = field(default_factory=list)
    delivered: list[str] = field(default_factory=list)
    next_task_number: int = 1


def empty_team_state(root_id: str) -> TeamState:
    """构造一个 Team 身份的空状态。"""
    return TeamState(id=root_id)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def _validate_payload(type_: str, data: Any) -> dict:
    """逐字段校验一个 Team 事件的 payload（对齐 upstream zod strict 要求）。

    用浅层语义检查把契约钉死：该抛的都抛 ValueError，事件进不了状态。
    """
    if not isinstance(data, dict):
        raise ValueError(f"persisted Agent Teams {type_} payload is invalid")
    version = data.get("version")
    team_id_value = data.get("teamId")
    if not isinstance(version, int) or isinstance(version, bool) or version < 0:
        raise ValueError(f"persisted Agent Teams {type_} payload is invalid")
    if not isinstance(team_id_value, str) or len(team_id_value) == 0:
        raise ValueError(f"persisted Agent Teams {type_} payload is invalid")
    return data


def _member_from(data: Any) -> TeamMemberSnapshot:
    if not isinstance(data, dict):
        raise ValueError("persisted Agent Teams team/member payload is invalid")
    for key in ("id", "name", "description", "provider", "context", "phase"):
        if not isinstance(data.get(key), str):
            raise ValueError("persisted Agent Teams team/member payload is invalid")
    if data["context"] not in ("fresh", "fork"):
        raise ValueError("persisted Agent Teams team/member payload is invalid")
    if data["phase"] not in ("provisioning", "active", "failed"):
        raise ValueError("persisted Agent Teams team/member payload is invalid")
    error = data.get("error")
    if error is not None and not isinstance(error, str):
        raise ValueError("persisted Agent Teams team/member payload is invalid")
    extra = set(data) - {"id", "name", "description", "provider", "context", "phase", "error"}
    if extra:
        raise ValueError("persisted Agent Teams team/member payload is invalid")
    return TeamMemberSnapshot.from_dict(data)


def _message_from(data: Any) -> TeamMessageSnapshot:
    if not isinstance(data, dict):
        raise ValueError("persisted Agent Teams team/message/queued payload is invalid")
    for key in ("id", "senderId", "senderName", "targetId"):
        if not isinstance(data.get(key), str):
            raise ValueError("persisted Agent Teams team/message/queued payload is invalid")
    if not isinstance(data.get("content"), list):
        raise ValueError("persisted Agent Teams team/message/queued payload is invalid")
    extra = set(data) - {"id", "senderId", "senderName", "targetId", "content"}
    if extra:
        raise ValueError("persisted Agent Teams team/message/queued payload is invalid")
    return TeamMessageSnapshot.from_dict(data)


def _task_from(data: Any) -> TeamTaskSnapshot:
    if not isinstance(data, dict):
        raise ValueError("persisted Agent Teams team/task payload is invalid")
    for key in ("id", "subject", "description", "status"):
        if not isinstance(data.get(key), str):
            raise ValueError("persisted Agent Teams team/task payload is invalid")
    revision = data.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise ValueError("persisted Agent Teams team/task payload is invalid")
    if data["status"] not in ("pending", "in_progress", "completed", "deleted"):
        raise ValueError("persisted Agent Teams team/task payload is invalid")
    owner_id = data.get("ownerId")
    if owner_id is not None and not isinstance(owner_id, str):
        raise ValueError("persisted Agent Teams team/task payload is invalid")
    for key in ("blockedBy", "writeScopes"):
        if not isinstance(data.get(key), list) or not all(
            isinstance(item, str) for item in data[key]
        ):
            raise ValueError("persisted Agent Teams team/task payload is invalid")
    extra = set(data) - {
        "id", "revision", "subject", "description", "status",
        "ownerId", "blockedBy", "writeScopes",
    }
    if extra:
        raise ValueError("persisted Agent Teams team/task payload is invalid")
    return TeamTaskSnapshot.from_dict(data)


def _apply_team_member(state: TeamState, member: TeamMemberSnapshot) -> None:
    index = next(
        (i for i, m in enumerate(state.members) if m.id == member.id), -1
    )
    prior = state.members[index] if index >= 0 else None
    named = next((m for m in state.members if m.name == member.name), None)
    if named is not None and named.id != member.id:
        raise ValueError(f'teammate name "{member.name}" is reused by another member')
    if prior is None:
        if member.phase != "provisioning":
            raise ValueError(f'teammate "{member.name}" must begin provisioning')
    else:
        if (
            prior.name != member.name
            or prior.provider != member.provider
            or prior.context != member.context
        ):
            raise ValueError(f'teammate "{member.id}" changed immutable identity fields')
        if prior.phase != "provisioning" or member.phase == "provisioning":
            raise ValueError(
                f'teammate "{member.name}" has an invalid {prior.phase} -> {member.phase} transition'
            )
    if index < 0:
        state.members.append(member)
    else:
        state.members[index] = member


def _apply_team_task(state: TeamState, task: TeamTaskSnapshot) -> None:
    index = next((i for i, t in enumerate(state.tasks) if t.id == task.id), -1)
    prior = state.tasks[index] if index >= 0 else None
    if prior is None and task.revision != 1:
        raise ValueError(f'team task "{task.id}" must begin at revision 1')
    if prior is not None and task.revision != prior.revision + 1:
        raise ValueError(f'team task "{task.id}" revision is not contiguous')
    assert_task_graph_candidate(
        tuple(t for t in state.tasks if t.id != task.id),
        task,
    )
    suffix = task.id.split("-")[-1]
    if task.id.startswith("task-") and suffix.isdigit() and suffix.isascii():
        number = int(suffix)
        if number == (1 << 53) - 1:
            state.next_task_number = max(state.next_task_number, number)
        else:
            state.next_task_number = max(state.next_task_number, number + 1)
    if index < 0:
        state.tasks.append(task)
    else:
        state.tasks[index] = task


def _apply_team_message_queued(state: TeamState, message: TeamMessageSnapshot) -> None:
    if any(m.id == message.id for m in state.messages):
        raise ValueError(f'team message "{message.id}" was queued twice')
    state.messages.append(message)


def _apply_team_message_delivered(state: TeamState, message_id: str, target_id: str) -> None:
    queued = next((m for m in state.messages if m.id == message_id), None)
    if queued is None:
        raise ValueError(f'team message "{message_id}" was delivered before queueing')
    if queued.targetId != target_id:
        raise ValueError(f'team message "{message_id}" target changed')
    if message_id in state.delivered:
        raise ValueError(f'team message "{message_id}" was delivered twice')
    state.delivered.append(message_id)


def _apply_event(state: TeamState, event: dict) -> None:
    """把一个 Team 事件应用到状态机（对齐 applyProjectionEvent）。损坏即抛 ValueError。"""
    if not is_team_event(event):
        return
    data = _validate_payload(event["type"], event.get("data"))
    if data["teamId"] != state.id:
        return
    if data["version"] != TEAM_EVENT_VERSION:
        raise ValueError(
            f"unsupported Agent Teams event version {data['version']}"
        )
    payload = dict(data)
    payload.pop("teamId", None)
    payload.pop("version", None)
    type_ = event["type"]
    if type_ == "team/member":
        member = _member_from(payload.get("member"))
        _apply_team_member(state, member)
    elif type_ == "team/task":
        task = _task_from(payload.get("task"))
        _apply_team_task(state, task)
    elif type_ == "team/message/queued":
        message = _message_from(payload.get("message"))
        _apply_team_message_queued(state, message)
    elif type_ == "team/message/delivered":
        message_id = payload.get("messageId")
        target_id = payload.get("targetId")
        if not isinstance(message_id, str) or not isinstance(target_id, str):
            raise ValueError(
                "persisted Agent Teams team/message/delivered payload is invalid"
            )
        _apply_team_message_delivered(state, message_id, target_id)


def _to_plain(value: Any) -> Any:
    """把 session 回读的 mappingproxy/tuple 深转为纯 dict/list（幂等）。

    事件经 session.append 后再读回是 mappingproxy（非 dict 子类，见 AGENTS.md
    §2.27 注记），这里统一转成投影校验能识别的裸容器。
    """
    if isinstance(value, Mapping):
        return {k: _to_plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_plain(v) for v in value]
    return value


def fold_team_state(team_events: list[dict], root_id: str) -> TeamState:
    """从 Lead 会话自有团队事件折叠权威 TeamState。

    events 必须已经是单进程内已提交 + 已校验的团队事件序列；损坏即抛 ValueError
    （调用方负责把失败呈现为服务错误）。
    """
    state = empty_team_state(root_id)
    for event in team_events:
        plain = dict(event)
        if "data" in plain:
            plain["data"] = _to_plain(plain["data"])
        _apply_event(state, plain)
    return state