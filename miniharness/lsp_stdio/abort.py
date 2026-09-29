"""本地 LSP provider 的取消助手（host-I/O、队列、协议阶段的共享信号处理）。

对齐 packages/lsp/lsp-stdio/src/abort.ts 的 `abortError`/`throwIfAborted`/`abortable`。

载体差异（登记）：上游用 Web `AbortSignal`（可 `.aborted`、`addEventListener('abort')`、
带 `reason` 与 dsh-timeout 分类）；mini 的调用方信号是 `threading.Event`/`FusedSignal`
（读 `.is_set()`、`.set()`）。本模块用鸭子类型读 `.aborted` 或 `.is_set()`，并在
`abortable` 里以短轮询观察取消（无事件监听面）。
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, TypeVar

__all__ = [
    "AbortCancelled",
    "abort_error",
    "abortable",
    "signal_aborted",
    "throw_if_aborted",
]

T = TypeVar("T")

#: 轮询取消信号的间隔（秒）。
_POLL_INTERVAL = 0.02


class AbortCancelled(Exception):
    """查询取消（上游 `abortError` 的 mini 载体：稳定类，非超时分类）。"""

    def __init__(self, message: str = "LSP query aborted"):
        super().__init__(message)
        self.code = "LSP_ABORTED"


def signal_aborted(signal: Any) -> bool:
    """signal 中止判定：AbortSignal 形状读 `.aborted`；事件/熔合信号读 `is_set()`。"""
    if signal is None:
        return False
    aborted = getattr(signal, "aborted", None)
    if aborted is not None:
        return bool(aborted)
    is_set = getattr(signal, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def abort_error(signal: Any) -> Exception:
    """构造取消错误（mini 无 timeoutOf 分类；signal.reason 为异常时透传）。"""
    reason = getattr(signal, "reason", None)
    if isinstance(reason, BaseException):
        return reason
    return AbortCancelled()


def throw_if_aborted(signal: Any) -> None:
    """信号已触发 → 抛出其取消错误。"""
    if signal_aborted(signal):
        raise abort_error(signal)


async def abortable(work: Awaitable[T], signal: Any = None) -> T:
    """等一个 awaitable，允许查询信号放弃等待（底层工作仍按其自身边界继续）。

    对齐上游 `abortable`（`Promise.race([work, canceled])`）：**绝不取消底层 work**——
    work 保留自己的处理器并跑到其属主定义的静止边界。mini 以短轮询观察
    `threading.Event`，取消时抛弃等待并抛取消错误（若 work 是新建的协程任务，其
    最终结果被静默消费，避免未取回警告）。
    """
    if signal is None:
        return await work
    if signal_aborted(signal):
        raise abort_error(signal)
    task = asyncio.ensure_future(work)
    while True:
        done, _pending = await asyncio.wait({task}, timeout=_POLL_INTERVAL)
        if task in done:
            return task.result()
        if signal_aborted(signal):
            # 抛弃等待但不取消 work；若它是新建任务，静默其最终异常。
            if task is not work:
                task.add_done_callback(_consume)
            raise abort_error(signal)


def _consume(task: asyncio.Future) -> None:
    """静默一个被抛弃任务的最终结果/异常（避免未取回警告）。"""
    if not task.cancelled():
        task.exception()

