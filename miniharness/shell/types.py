"""shell 能力接缝的共享类型与载体助手（对齐 packages/shell/shell/src/types.ts）。

契约要点（与上游逐条一致）：
  * `ShellExpiryPolicy = 'kill' | 'none'`——超时策略；`'none'` 不布防 deadline，
    resolved `timeoutMs` 只被回显。已 aborted 的 signal 视为已触发。
  * `SubprocessOutputReader.read_from(from_byte)` 是非消耗偏移读（观测面），
    与消费式 `readOutput()` 游标互不打扰。
  * `ShellExecution` 既是 `ShellProcess`（status/exitCode/signal/done/observed/
    readOutput/kill）又带 `result()` 前台投影（首因 timedOut/aborted，memoized）。

载体说明（登录 verified-diffs）：mini 无 `ctx.subprocess` 托管范围与 spill 收集器，
输出以进程内增长缓冲承载，读尾按 UTF-8 安全边界裁头；无 spillPath。
"""
from __future__ import annotations

import concurrent.futures
import threading

__all__ = [
    "SHELL_EXPIRY_POLICIES",
    "CollectedOutput",
    "OutputBuffer",
    "ShellExecution",
    "SubprocessOutputReader",
    "is_aborted",
    "settled_execution",
]

#: 到期策略闭集（types.ts:53）。
SHELL_EXPIRY_POLICIES = ("kill", "none")

#: UTF-8 续字节掩码（裁头对齐多字节边界）。
_UTF8_CONTINUATION = 0x80
_UTF8_CONTINUATION_MASK = 0xC0


class OutputBuffer:
    """进程内增长字节缓冲：绝对偏移 + 头部安全驱逐 + 非消耗偏移读。

    载体替身上游 subprocess 收集器的内存尾（无 spill 文件）。`max_bytes` 为
    None 时无界；超限时从头部丢弃到 UTF-8 边界，已分配偏移不移动（`_total`
    单调），读早于 `_base` 的偏移标记 `lossy`。
    """

    def __init__(self, max_bytes: int | None = None):
        self._lock = threading.Lock()
        self._data = bytearray()
        self._base = 0
        self._total = 0
        self._max_bytes = max_bytes

    def append(self, chunk: bytes) -> None:
        if not chunk:
            return
        with self._lock:
            self._data.extend(chunk)
            self._total += len(chunk)
            if self._max_bytes is not None and len(self._data) > self._max_bytes:
                drop = len(self._data) - self._max_bytes
                # 不劈裂多字节字符：续字节位置的头部边界继续前移
                while drop < len(self._data) and (self._data[drop] & _UTF8_CONTINUATION_MASK) == _UTF8_CONTINUATION:
                    drop += 1
                del self._data[:drop]
                self._base += drop

    def read_from(self, from_byte: int) -> dict:
        """读 `from_byte` 起的未读字节（非消耗）；返回 text/nextOffset/lossy。"""
        with self._lock:
            start = from_byte
            lossy = False
            if start < self._base:
                lossy = True
                start = self._base
            if start > self._total:
                start = self._total
            text = bytes(self._data[start - self._base:]).decode("utf-8", errors="replace")
            return {"text": text, "nextOffset": self._total, "lossy": lossy}

    def final(self) -> dict:
        """结算后的完整收集视图（对齐 CollectedOutput；无 spillPath）。"""
        read = self.read_from(0)
        return {"text": read["text"], "truncated": read["lossy"]}


class SubprocessOutputReader:
    """非消耗偏移读器（对齐 SubprocessOutputReader）：同一缓冲可多读者独立读。"""

    __slots__ = ("_buffer",)

    def __init__(self, buffer: OutputBuffer):
        self._buffer = buffer

    def read_from(self, from_byte: int) -> dict:
        return self._buffer.read_from(from_byte)


def is_aborted(signal) -> bool:
    """统一判读 signal 形状：`aborted` 属性或 `is_set()`（threading.Event/FusedSignal）。"""
    if signal is None:
        return False
    aborted = getattr(signal, "aborted", None)
    if aborted is not None:
        return bool(aborted)
    is_set = getattr(signal, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


class ShellExecution:
    """一次执行的句柄：`ShellProcess` 面 + `result()` 前台投影（对齐 ShellExecution）。

    mini 载体以 `subprocess.Popen` + 读线程 / 监视线程承载：`execute` 返回后进程
    可能仍在运行（后台），`result()` 阻塞至结算。spawn 失败被包含——`done` 正常
    结算、错误只在 `result()` 抛出（对齐上游 provider failure 语义）。
    """

    def __init__(self, stdout_reader: SubprocessOutputReader,
                 stderr_reader: SubprocessOutputReader):
        self.status = "running"
        self.exitCode = None
        self.signal = None
        self.sandbox = None
        self.observed = {"stdout": stdout_reader, "stderr": stderr_reader}
        self._done: "concurrent.futures.Future" = concurrent.futures.Future()
        self._stdout_cursor = 0
        self._stderr_cursor = 0
        self._result = None
        self._result_builder = None
        self._kill_fn = None
        self._spawn_error = None

    @property
    def done(self) -> "concurrent.futures.Future":
        """进程结算 Future（never rejects；spawn 失败也正常结算）。"""
        return self._done

    def mark_settled(self) -> None:
        """结算 `done`（幂等）。"""
        if not self._done.done():
            self._done.set_result(None)

    def read_output(self) -> dict:
        """消费式增量读：stdout 文本 + 一个 `[stderr]` 段；游标只进不退。"""
        out = self.observed["stdout"].read_from(self._stdout_cursor)
        err = self.observed["stderr"].read_from(self._stderr_cursor)
        self._stdout_cursor = out["nextOffset"]
        self._stderr_cursor = err["nextOffset"]
        separator = "\n" if out["text"] and not out["text"].endswith("\n") else ""
        delta = out["text"] + (f"{separator}[stderr]\n{err['text']}" if err["text"] else "")
        return {"delta": delta, "lossy": bool(out["lossy"] or err["lossy"])}

    def kill(self) -> bool:
        """终止托管范围；已结算返回 False（幂等，对齐 ShellProcess.kill）。"""
        if self.status != "running":
            return False
        self.status = "killed"
        if self._kill_fn is not None:
            self._kill_fn()
        return True

    def result(self) -> dict:
        """前台投影（memoized）：结算后按首因分类并交出 split streams。"""
        if self._result is None:
            self._done.result()
            if self._spawn_error is not None:
                raise self._spawn_error
            self._result = self._result_builder()
        return self._result


def settled_execution(exit_code: int | None = 0, stdout: str = "", stderr: str = "",
                      signal: str | None = None, timed_out: bool = False,
                      aborted: bool = False, timeout_ms: int = 0,
                      sandbox: dict | None = None,
                      max_bytes: int | None = None) -> ShellExecution:
    """构造一个已结算 `ShellExecution`（测试/载体断言用的真实对象，非 mock）。"""
    stdout_buf = OutputBuffer(max_bytes)
    stderr_buf = OutputBuffer(max_bytes)
    stdout_buf.append(stdout.encode("utf-8"))
    stderr_buf.append(stderr.encode("utf-8"))
    execution = ShellExecution(SubprocessOutputReader(stdout_buf),
                               SubprocessOutputReader(stderr_buf))
    execution.exitCode = exit_code
    execution.signal = signal
    execution.sandbox = sandbox
    execution.status = "killed" if (signal is not None or timed_out or aborted) else "completed"
    execution._result_builder = lambda: {
        "exitCode": exit_code,
        "signal": signal,
        "timedOut": timed_out,
        "aborted": aborted,
        "timeoutMs": timeout_ms,
        "stdout": stdout_buf.final(),
        "stderr": stderr_buf.final(),
        **({"sandbox": sandbox} if sandbox is not None else {}),
    }
    execution.mark_settled()
    return execution
