"""Durable team shared task board（对齐上游 task-board.ts）。

经 Team Lead 日志 `team/task` event 提交，append 全量 task 快照（version 2）；
Compare-and-set：每个 mutation 携带 expectedRevision，CAS 失败抛
TEAM_TASK_STALE_REVISION；owner/Lead 授权门；目标星图受共享校验器约束。
"""

from __future__ import annotations

from typing import Any

from ...core.agent_loop.agent import AgentLoop
from .error import TeamError
from .journal import TeamJournal
from .roster import resolve_active_member
from .task_graph import assert_task_graph_candidate, TeamTaskGraphError
from .types import TeamTaskSnapshot, TeamTaskView, UpdateTeamTaskRequest
from .validation import required_text, write_scope

__all__ = ["TeamTaskBoard"]

MAX_TASK_SUBJECT_LENGTH = 200
MAX_TASK_DESCRIPTION_LENGTH = 16_384

TASK_GRAPH_ERROR_CODES = {
    "missing": "TEAM_TASK_NOT_FOUND",
    "duplicate": "TEAM_INVALID_ARGUMENT",
    "cycle": "TEAM_TASK_DEPENDENCY_CYCLE",
}


def scopes_overlap(left: str, right: str) -> bool:
    """两条规范化文件/目录前缀在路径分量上是否重叠（对齐上游 scopesOverlap）。"""
    return left == right or left.startswith(f"{right}/") or right.startswith(f"{left}/")


class TeamTaskBoard:
    def __init__(
        self,
        agents: Any,
        journal: TeamJournal,
        roster: TeamRoster,
        max_tasks: int,
    ):
        self._agents = agents
        self._journal = journal
        self._roster = roster
        self._max_tasks = max_tasks

    # ---------- 读 ----------

    def list(self, membership) -> list[TeamTaskView]:
        state = self._journal.state(membership.root)
        return [
            self._task_view(membership, state, task)
            for task in state.tasks if task.status != "deleted"
        ]

    def get(self, membership, task_id: str) -> TeamTaskView:
        state = self._journal.state(membership.root)
        task = next((t for t in state.tasks if t.id == task_id), None)
        if task is None:
            raise TeamError(f'team task "{task_id}" not found', "TEAM_TASK_NOT_FOUND")
        return self._task_view(membership, state, task)

    # ---------- 写 ----------

    def create(self, membership, subject: str, description: str,
               blocked_by: tuple[str, ...], write_scopes: tuple[str, ...]) -> TeamTaskView:
        root = membership.root
        return self._transact(root, lambda: self._create_locked(membership, subject, description, blocked_by, write_scopes))

    def _create_locked(self, membership, subject: str, description: str,
                       blocked_by: tuple[str, ...], write_scopes: tuple[str, ...]) -> TeamTaskView:
        root = membership.root
        state = self._journal.state(root)
        active = [t for t in state.tasks if t.status != "deleted"]
        if len(active) >= self._max_tasks:
            raise TeamError(f"Team task limit {self._max_tasks} reached", "TEAM_TASK_LIMIT")
        task_id = f"task-{state.next_task_number}"
        if any(t.id == task_id for t in state.tasks):
            raise TeamError("Team task id space exhausted", "TEAM_TASK_LIMIT")
        task = TeamTaskSnapshot(
            id=task_id,
            revision=1,
            subject=required_text(subject, "subject", MAX_TASK_SUBJECT_LENGTH),
            description=required_text(description, "description", MAX_TASK_DESCRIPTION_LENGTH),
            status="pending",
            blockedBy=tuple(self._dependencies(blocked_by, state)),
            writeScopes=tuple(self._write_scopes(write_scopes)),
        )
        self._assert_task_graph(state, task)
        self._journal.append_and_flush(root, "team/task", {
            "version": 2, "teamId": root.id, "task": task.to_dict(),
        })
        return self._task_view(membership, state, task)

    def update(self, caller: AgentLoop, membership, request: UpdateTeamTaskRequest) -> TeamTaskView:
        root = membership.root
        return self._transact(root, lambda: self._update_locked(caller, membership, request))

    def _update_locked(self, caller: AgentLoop, membership, request: UpdateTeamTaskRequest) -> TeamTaskView:
        root = membership.root
        state = self._journal.state(root)
        current = next((t for t in state.tasks if t.id == request.taskId), None)
        if current is None:
            raise TeamError(f'team task "{request.taskId}" not found', "TEAM_TASK_NOT_FOUND")
        if current.revision != request.expectedRevision:
            raise TeamError(
                f'stale team task "{current.id}" revision {request.expectedRevision}; '
                f"current revision is {current.revision}",
                "TEAM_TASK_STALE_REVISION",
            )
        if current.status == "deleted":
            raise TeamError(f'team task "{current.id}" is deleted', "TEAM_TASK_DELETED")
        lead = membership.role == "lead"
        owner = current.ownerId == caller.id

        def authorize_owner() -> None:
            if not lead and not owner:
                raise TeamError(
                    "task mutation requires its owner or Team Lead",
                    "TEAM_TASK_UNAUTHORIZED",
                )

        action = request.action
        if action == "claim":
            if current.ownerId is not None and current.ownerId != caller.id:
                raise TeamError(
                    f'team task "{current.id}" is owned by another member',
                    "TEAM_TASK_ALREADY_CLAIMED",
                )
            if current.status != "pending" or not self._task_ready(state, current):
                raise TeamError(
                    f'team task "{current.id}" is not ready to claim',
                    "TEAM_TASK_BLOCKED",
                )
            next_ = TeamTaskSnapshot(**{**current.to_dict(), "status": "in_progress", "ownerId": caller.id})
        elif action == "release":
            authorize_owner()
            if current.status != "in_progress":
                raise TeamError(
                    "only an in-progress task can be released",
                    "TEAM_TASK_INVALID_TRANSITION",
                )
            next_ = _without_owner(TeamTaskSnapshot(**{**current.to_dict(), "status": "pending"}))
        elif action == "edit":
            authorize_owner()
            if (request.subject is None and request.description is None
                    and request.writeScopes is None):
                raise TeamError(
                    "task edit requires subject, description, or write_scopes",
                    "TEAM_INVALID_ARGUMENT",
                )
            data = current.to_dict()
            if request.subject is not None:
                data["subject"] = required_text(request.subject, "subject", MAX_TASK_SUBJECT_LENGTH)
            if request.description is not None:
                data["description"] = required_text(request.description, "description", MAX_TASK_DESCRIPTION_LENGTH)
            if request.writeScopes is not None:
                data["writeScopes"] = tuple(self._write_scopes(request.writeScopes))
            next_ = TeamTaskSnapshot(**data)
        elif action == "set_dependencies":
            authorize_owner()
            if request.blockedBy is None:
                raise TeamError("set_dependencies requires blocked_by", "TEAM_INVALID_ARGUMENT")
            next_ = TeamTaskSnapshot(**{
                **current.to_dict(),
                "blockedBy": tuple(self._dependencies(request.blockedBy, state, current.id)),
            })
        elif action == "complete":
            authorize_owner()
            if current.status != "in_progress":
                raise TeamError(
                    "only an in-progress task can complete",
                    "TEAM_TASK_INVALID_TRANSITION",
                )
            next_ = TeamTaskSnapshot(**{**current.to_dict(), "status": "completed"})
        elif action == "reopen":
            authorize_owner()
            if current.status != "completed":
                raise TeamError(
                    "only a completed task can reopen",
                    "TEAM_TASK_INVALID_TRANSITION",
                )
            next_ = _without_owner(TeamTaskSnapshot(**{**current.to_dict(), "status": "pending"}))
        elif action == "reassign":
            if not lead:
                raise TeamError("only the Team Lead can reassign tasks", "TEAM_LEAD_REQUIRED")
            if current.status not in ("pending", "in_progress"):
                raise TeamError(
                    "only a pending or in-progress task can be reassigned",
                    "TEAM_TASK_INVALID_TRANSITION",
                )
            if request.owner is None or request.owner.strip() == "":
                next_ = _without_owner(TeamTaskSnapshot(**{**current.to_dict(), "status": "pending"}))
            else:
                if not self._task_ready(state, current):
                    raise TeamError(f'team task "{current.id}" is blocked', "TEAM_TASK_BLOCKED")
                assignee = resolve_active_member(root, state, request.owner)
                next_ = TeamTaskSnapshot(**{
                    **current.to_dict(), "status": "in_progress", "ownerId": assignee["id"],
                })
        elif action == "delete":
            authorize_owner()
            dependent = next(
                (t for t in state.tasks
                 if t.status != "deleted" and t.id != current.id and current.id in t.blockedBy),
                None,
            )
            if dependent is not None:
                raise TeamError(
                    f'team task "{current.id}" still blocks "{dependent.id}"',
                    "TEAM_TASK_HAS_DEPENDENTS",
                )
            next_ = TeamTaskSnapshot(**{**current.to_dict(), "status": "deleted"})
        else:  # pragma: no cover - 闭集已穷举
            raise TeamError(f"unsupported task action {action}", "TEAM_INVALID_ARGUMENT")

        task = TeamTaskSnapshot(**{**next_.to_dict(), "revision": current.revision + 1})
        self._assert_task_graph(state, task)
        self._journal.append_and_flush(root, "team/task", {
            "version": 2, "teamId": root.id, "task": task.to_dict(),
        })
        return self._task_view(membership, state, task)

    # ---------- 内部 ----------

    def _transact(self, root: AgentLoop, operation):
        return self._journal.transact(root.id, operation)

    def _dependencies(self, values, state, self_id: str | None = None):
        seen = set()
        result = []
        for id_ in values:
            if id_ == self_id:
                raise TeamError("a team task cannot block itself", "TEAM_TASK_DEPENDENCY_CYCLE")
            if id_ in seen:
                raise TeamError(f'duplicate blocker "{id_}"', "TEAM_INVALID_ARGUMENT")
            task = next((t for t in state.tasks if t.id == id_), None)
            if task is None or task.status == "deleted":
                raise TeamError(f'blocker task "{id_}" not found', "TEAM_TASK_NOT_FOUND")
            seen.add(id_)
            result.append(id_)
        return result

    def _write_scopes(self, values):
        return list(dict.fromkeys(write_scope(v) for v in values))

    def _assert_task_graph(self, state, candidate: TeamTaskSnapshot) -> None:
        try:
            assert_task_graph_candidate(
                [t for t in state.tasks if t.status != "deleted"],
                candidate,
            )
        except TeamTaskGraphError as error:
            raise TeamError(str(error), TASK_GRAPH_ERROR_CODES[error.violation]) from error

    def _task_ready(self, state, task: TeamTaskSnapshot) -> bool:
        by_id = {t.id: t for t in state.tasks}
        return all(
            by_id.get(id_) is not None and by_id[id_].status == "completed"
            for id_ in task.blockedBy
        )

    def _task_view(self, membership, state, task: TeamTaskSnapshot) -> TeamTaskView:
        root = membership.root
        if task.ownerId is None:
            owner_name = None
        elif task.ownerId == root.id:
            owner_name = "lead"
        else:
            owner_name = next(
                (m.name for m in state.members if m.id == task.ownerId), None
            )
        warnings = set()
        for other in state.tasks:
            if other.id == task.id or other.status != "in_progress":
                continue
            if any(
                scopes_overlap(left, right)
                for left in task.writeScopes for right in other.writeScopes
            ):
                warnings.add(f"write scopes overlap with {other.id}")
        return TeamTaskView(
            id=task.id,
            revision=task.revision,
            subject=task.subject,
            description=task.description,
            status=task.status,
            blockedBy=task.blockedBy,
            writeScopes=task.writeScopes,
            ownerName=owner_name,
            ready=task.status == "pending" and self._task_ready(state, task),
            writeScopeWarnings=tuple(warnings),
        )


def _without_owner(task: TeamTaskSnapshot) -> TeamTaskSnapshot:
    return TeamTaskSnapshot(**{**task.to_dict(), "ownerId": None})