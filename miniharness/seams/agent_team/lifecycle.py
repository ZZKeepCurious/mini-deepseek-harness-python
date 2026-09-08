"""Team 运行时准入截止 + 结算（对齐上游 lifecycle.ts）。

上游 AbortController：dispose() 后新准入抛 TEAM_DISPOSED、取消在飞操作、有界
等待不中断；mini 以同步布尔标记 + 同构错误码闭合准入，`pending_operations` 收集
结算前未完结的操作（同步载体多为空，保留簿记接口对齐上游 settle 语义）。
"""

from __future__ import annotations

from typing import Any

from .error import TeamError

__all__ = ["TeamRuntimeLifecycle"]


class TeamRuntimeLifecycle:
    def __init__(self, disposal_timeout_ms: int):
        self.disposal_timeout_ms = disposal_timeout_ms
        self._disposed = False
        self._pending: list[Any] = []

    @property
    def disposed(self) -> bool:
        return self._disposed

    @property
    def signal_aborted(self) -> bool:
        return self._disposed

    def assert_admitting(self) -> None:
        if self._disposed:
            raise TeamError("Agent Teams service is disposing", "TEAM_DISPOSED")

    def track(self, operation: Any) -> Any:
        """登记一个 admitted 操作供结算快照（同步载体多为**进行中**任务引用）。"""
        if operation is not None:
            self._pending.append(operation)

            def forget() -> None:
                try:
                    self._pending.remove(operation)
                except ValueError:
                    pass

            if hasattr(operation, "add_done_callback"):
                operation.add_done_callback(lambda _: forget())
            elif callable(operation):
                pass
        return operation

    def pending_operations(self) -> tuple[Any, ...]:
        return tuple(self._pending)

    def settle(self) -> None:
        """关闭准入并结算在飞操作摘要（载体差异：同步完成语义，无 await）。"""
        self._disposed = True