"""Schedule 对共享会话持久化屏障的使用（对齐 packages/schedule/schedule/src/persistence.ts）。

mini 的 ``ctx.sessions.flush(session)`` 已是同步并行检查点且返回是否至少一个
监听器参与（session_store.py flush）；本模块把它包装为「无人参与或抛错 →
SchedulePersistenceError」，与上游「一次成功 shared checkpoint」的语义同构。
flushing 名义上同步，但返回稳定的 bool（无 await 面）——调用方按需 await。
"""
from __future__ import annotations

from typing import Any

__all__ = ["SchedulePersistenceError", "flush_schedule_persistence"]


class SchedulePersistenceError(RuntimeError):
    """未能证明当前 live 前缀到达过持久化监听器（上游 persistence.ts:7-16）。"""

    def __init__(self, cause: BaseException | None = None):
        message = "Schedule persistence did not complete."
        if cause is not None:
            super().__init__(message, cause)
            self.__cause__ = cause
        else:
            super().__init__(message)
        self.name = "SchedulePersistenceError"


def flush_schedule_persistence(ctx: Any, session: Any) -> None:
    """要求一次成功的共享持久化检查点（未何参与即失败）。

    @param ctx - 携带 live session store 的全局上下文。
    @param session - 要检查点的精确 live 会话。
    @raises SchedulePersistenceError - 无人参与（false）或监听器抛错。
    """
    sessions = getattr(ctx, "sessions", None) or ctx.get("sessions")
    if sessions is None:
        raise SchedulePersistenceError()
    try:
        if not sessions.flush(session):
            raise SchedulePersistenceError()
    except SchedulePersistenceError:
        raise
    except BaseException as error:
        raise SchedulePersistenceError(error) from error