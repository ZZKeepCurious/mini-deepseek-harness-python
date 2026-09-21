"""一条 Remote 流代次的有限输出队列（对齐 terminal-controller/src/stream.ts）。

排队预算按 `JSON.stringify(frame)` 的 UTF-8 字节；超额即显式失败并关闭该 follower
（较晚的附加可从屏幕恢复）。正常关闭先排空已排队帧再终止（含最终退出状态）。
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any, Iterator

from .types import frame_bytes

__all__ = ["TerminalFollower"]

#: 同步 read() 的等待切片（毫秒级）；真实用法经 pop() 由异步包装轮询，
#: read() 供确定性单测/同步消费。
_READ_WAIT_SECONDS = 0.05


class TerminalFollower:
    """一个输出跟随者的有界队列（stream.ts:6-75）。

    @param max_bytes - 本 follower 允许排队的最大 UTF-8 字节数。
    """

    def __init__(self, max_bytes: int):
        self._max_bytes = max_bytes
        self._queue: deque[tuple[dict, int]] = deque()
        self._bytes = 0
        self._closed = False
        self._finished = False
        self._failure: Exception | None = None
        self._lock = threading.Condition()

    # ---------- 生产面 ----------

    def push(self, frame: dict) -> None:
        """入队一帧；超出字节预算即显式失败并关闭本 follower。"""
        with self._lock:
            if self._closed or self._finished:
                return
            size = frame_bytes(frame)
            if self._bytes + size > self._max_bytes:
                self._failure = RuntimeError(
                    "Terminal output consumer exceeded its buffer; reconnect to "
                    "recover the current screen")
                self._close_locked()
                return
            self._queue.append((frame, size))
            self._bytes += size
            self._lock.notify_all()

    def finish(self) -> None:
        """排空全部已排队帧后结束（含最终退出状态）。"""
        with self._lock:
            self._finished = True
            self._lock.notify_all()

    def close(self) -> None:
        """停止本 follower 而不停止其终端；丢弃已排队数据。"""
        with self._lock:
            self._close_locked()

    def _close_locked(self) -> None:
        self._closed = True
        self._queue.clear()
        self._bytes = 0
        self._lock.notify_all()

    # ---------- 读面 ----------

    def pop(self) -> dict | None:
        """非阻塞取下一帧；空返回 None（供异步包装轮询）。"""
        with self._lock:
            if not self._queue:
                return None
            frame, size = self._queue.popleft()
            self._bytes -= size
            return frame

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def finished(self) -> bool:
        return self._finished

    @property
    def failure(self) -> Exception | None:
        return self._failure

    def read(self) -> Iterator[dict]:
        """同步排空直至被分离或失败（stream.ts:53-74 的同步等价）。

        调用方在生成器返回/关闭时负责 detach；此处 finally 关闭 follower。
        """
        try:
            while True:
                frame: dict | None = None
                done = False
                with self._lock:
                    if self._closed:
                        done = True
                    elif self._queue:
                        frame, size = self._queue.popleft()
                        self._bytes -= size
                    elif self._finished:
                        done = True
                    else:
                        self._lock.wait(_READ_WAIT_SECONDS)
                if frame is not None:
                    yield frame
                    continue
                if done:
                    break
            if self._failure is not None:
                raise self._failure
        finally:
            self.close()
