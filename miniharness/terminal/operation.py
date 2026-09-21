"""本地 send 操作（对齐 upstream terminal-bash/src/session.ts:158-234 LocalSendOperation）。

一次独占交互式 send：携带独立的 maxReadBytes 输出缓冲、结算（settle）、取消
（cancel→前台 SIGINT 由 on_cancel 回调驱动）。readiness 轮询所需的前台证据
（set_initial_foreground / accepts_stdin_wait）一并在此维护，供 P2 会话轮询消费。

载体差异（已登记）：上游 `done` 为 Promise，await 回落；本实现为同步契约——
backend 驱动 settle/fail 后读 `done` 取结果或重抛失败，未 settle 即读取 fail loud。
"""

from __future__ import annotations

from .bounded_buffer import BoundedTextBuffer

__all__ = ["LocalSendOperation"]


class LocalSendOperation:
    """backend 会话内的一次独占交互式 send。"""

    def __init__(self, max_bytes: int, started_at: int, on_cancel=None):
        self._output = BoundedTextBuffer(max_bytes)
        self.started_at = started_at
        self._on_cancel = on_cancel
        self._finished = False
        self._cancellation_requested = False
        self._failed = None
        self._result = None
        self._initial_foreground_pgid: int | None = None
        self._initial_foreground_left_wait = True

    @property
    def settled(self) -> bool:
        return self._finished

    @property
    def cancel_requested(self) -> bool:
        return self._cancellation_requested

    @property
    def done(self):
        """结算结果；failed 时重抛，未结算时 fail loud（同步载体契约）。"""
        if not self._finished:
            raise RuntimeError("PTY send has not settled")
        if self._failed is not None:
            raise self._failed
        return self._result

    @property
    def result(self):
        """结算结果（done 的免重抛读取面，结算后恒可用）。"""
        if not self._finished:
            raise RuntimeError("PTY send has not settled")
        return self._result

    def set_on_cancel(self, callback) -> None:
        """补绑取消回调（构造期无法引用自身的场景：先建 op 再绑定）。"""
        self._on_cancel = callback

    def append(self, text: str) -> None:
        if not self._finished:
            self._output.append(text)

    def settle(self, wait_reason: str, session_status: dict, inherited_truncation: bool) -> None:
        if self._finished:
            return
        self._finished = True
        read = self._output.snapshot()
        self._result = {
            "viewport": read["text"],
            "waitReason": wait_reason,
            "sessionStatus": session_status,
            "truncated": read["truncated"] or inherited_truncation,
        }

    def fail(self, error: BaseException) -> None:
        if self._finished:
            return
        self._finished = True
        self._failed = error

    def read_output(self) -> dict:
        """自上次读取以来的增量输出（delta / truncated）。"""
        return self._output.consume()

    def set_initial_foreground(self, foreground: dict | None) -> None:
        self._initial_foreground_pgid = foreground.get("processGroupId") if foreground else None
        self._initial_foreground_left_wait = foreground is None or foreground.get("inputWaiting") is not True

    def accepts_stdin_wait(self, pgid: int, waiting: bool) -> bool:
        """同一前台组可能仍暴露 write 前就已存在的 wait（session.ts:219-226）。"""
        if pgid != self._initial_foreground_pgid:
            return waiting
        if not waiting:
            self._initial_foreground_left_wait = True
        return waiting and self._initial_foreground_left_wait

    def cancel(self) -> bool:
        if self._finished:
            return False
        self._cancellation_requested = True
        if self._on_cancel is not None:
            self._on_cancel()
        return True