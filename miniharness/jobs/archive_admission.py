"""job 家族并入 Workspace 归档准入（对齐 jobs/src/archive-admission.ts）。

回答 `workspace/session-activity`：该 Session 拥有的、尚未结算的作业，按 kind
`job` 前插；`workspace/session-stop`：逐一 kill 这些作业。两个监听器与 `ctx` 的
fiber 同寿——即注册表自身的 fiber。每个注册表实现经 seam 构造函数安装，故仅凭
abstract 的 `list`/`kill` 即对所有实现生效。

载体说明（登录 verified-diffs）：mini 的 jobs 层（L2）不得 import workspace（L2，
见 test_dependencies §5 规则 1），故活动条目以本地 duck-typed `_JobActivity`
（`.kind`/`.items`）承载；workspace 侧消费面只看属性，语义等价。
"""
from __future__ import annotations

__all__ = ["install_job_archive_admission"]


class _ActivityItem:
    """活动项（`.id`/`.label`），对齐上游 SessionActivityItem 的消费面。"""

    __slots__ = ("id", "label")

    def __init__(self, id_: str, label) -> None:
        self.id = id_
        self.label = label


class _JobActivity:
    """`job` 活动家族（`.kind`/`.items`），对齐上游 SessionActivity 的消费面。"""

    __slots__ = ("kind", "items")

    def __init__(self, items: list) -> None:
        self.kind = "job"
        self.items = items


def _running_jobs(registry, owner: str) -> list:
    """该 Session 拥有的未结算作业；同列表中的 unowned 作业不属于任何人。"""
    return [
        job for job in registry.list(owner)
        if job.get("owner") == owner and job["status"] in ("running", "stopping")
    ]


def install_job_archive_admission(ctx, registry) -> None:
    """安装 job 归档准入：activity 询问 + session-stop kill。

    @param ctx - 注册表的注册上下文。
    @param registry - 以 `list`/`kill` 作答的注册表。
    """
    def on_session_activity(payload: dict, next_) -> list:
        session_id = payload.get("sessionId") if isinstance(payload, dict) else None
        jobs = _running_jobs(registry, session_id) if session_id is not None else []
        rest = next_()
        if not jobs:
            return rest
        own = _JobActivity([
            _ActivityItem(job["id"], job["label"]) for job in jobs
        ])
        return [own, *(rest or [])]

    def on_session_stop(payload: dict) -> None:
        session_id = payload.get("sessionId") if isinstance(payload, dict) else None
        if session_id is None:
            return
        for job in _running_jobs(registry, session_id):
            try:
                registry.kill(job["id"], session_id, "session archived")
            except Exception as error:  # noqa: BLE001 - 一个 producer 抛错不能扣住其余作业
                logger = getattr(ctx, "logger", None)
                if logger is not None:
                    logger.warn(
                        f'jobs: killing "{job["id"]}" for an archived Session failed: {error}')

    ctx.on("workspace/session-activity", on_session_activity)
    ctx.on("workspace/session-stop", on_session_stop)
