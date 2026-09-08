"""Team task 依赖图完整校验（对齐上游 task-graph.ts）。"""

from __future__ import annotations

from typing import Literal

from .types import TeamTaskSnapshot

__all__ = ["TeamTaskGraphError", "TeamTaskGraphViolation", "assert_task_graph_candidate"]

TeamTaskGraphViolation = Literal["missing", "duplicate", "cycle"]


class TeamTaskGraphError(Exception):
    """依赖关系不合法的包内失败，供命令层映射为稳定错误码。"""

    def __init__(self, message: str, violation: TeamTaskGraphViolation):
        super().__init__(message)
        self.violation = violation


def assert_task_graph_candidate(
    current: tuple[TeamTaskSnapshot, ...], candidate: TeamTaskSnapshot
) -> None:
    """替换一条候选快照后校验完整 active task 图。

    任一 active 依赖缺失 / 重复 / 自引用 / 成环即抛 TeamTaskGraphError。
    """
    tasks = {task.id: task for task in current}
    tasks[candidate.id] = candidate

    for task in tasks.values():
        if task.status == "deleted":
            continue
        seen = set()
        for blocker_id in task.blockedBy:
            if blocker_id == task.id:
                raise TeamTaskGraphError(
                    f'team task "{task.id}" cannot block itself', "cycle"
                )
            if blocker_id in seen:
                raise TeamTaskGraphError(
                    f'team task "{task.id}" repeats blocker "{blocker_id}"', "duplicate"
                )
            blocker = tasks.get(blocker_id)
            if blocker is None or blocker.status == "deleted":
                raise TeamTaskGraphError(
                    f'blocker task "{blocker_id}" for "{task.id}" is missing or deleted',
                    "missing",
                )
            seen.add(blocker_id)

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(id_: str) -> None:
        if id_ in visiting:
            raise TeamTaskGraphError(f'task dependency cycle includes "{id_}"', "cycle")
        if id_ in visited:
            return
        task = tasks.get(id_)
        if task is None or task.status == "deleted":
            return
        visiting.add(id_)
        for blocker_id in task.blockedBy:
            visit(blocker_id)
        visiting.discard(id_)
        visited.add(id_)

    for task in tasks.values():
        visit(task.id)