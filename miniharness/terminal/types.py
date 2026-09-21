"""terminal 域公共类型、常量与错误（对齐 upstream terminal/src/types.ts + index.ts 错误码闭集）。

上游：
  * types.ts:29  TerminalWaitReason、types.ts:36 TerminalSignal（5 信号闭集）、
    types.ts:39 TerminalSessionStatus（running / exited 判别联合）、
    types.ts:55-71 请求接口、types.ts:73-91 增量/结算结果、types.ts:103-131 翻页/信号结果、
    types.ts:133-177 backend 契约与快照。
  * index.ts:55-63 TerminalErrorCode 8 码闭集、index.ts:78-80 TerminalSessionId 品牌。

实现载体差异（已在 design-terminal-domain.md 立项 + verified-diffs 登记）：
  * 上游 AbortSignal → 本包的 Cancellation（threading.Event 载体，附带 reason）；
    上游 Promise/async backend → 同步 backend 契约（P1 单线程确定性面）。
  * 结果形状沿用上游 JSON 字段名的 camelCase 键、判别字段 kind —— 直接可序列化。
"""

from __future__ import annotations

import threading
from typing import Protocol

#: 一次交互式 send 返回控制权的四种原因（types.ts:29）。
TERMINAL_WAIT_REASONS = ("stdin_read", "inferred_idle", "timeout", "session_exit")

#: 模型面允许发给前台进程组的信号闭集（types.ts:36，与 subprocess 的
#: SubprocessTerminalSignal 保持成员一致）。
TERMINAL_SIGNALS = ("SIGINT", "SIGTERM", "SIGKILL", "SIGTSTP", "SIGHUP")
TERMINAL_SIGNAL_SET = frozenset(TERMINAL_SIGNALS)

#: TerminalErrorCode 闭集（index.ts:55-63）。
TERMINAL_ERROR_CODES = frozenset({
    "DUPLICATE_BACKEND",
    "DUPLICATE_NAME",
    "FOREIGN_SESSION",
    "NO_BACKEND",
    "NO_SESSION",
    "OWNER_NOT_LIVE",
    "SEND_ACTIVE",
    "SERVICE_DISPOSING",
})

#: controlled bash 发送的私人 prompt marker 的 OSC 前缀（sanitize.ts:6）。
PROMPT_MARKER_PREFIX = "133;D;"

#: marker 之后的可打印 prompt（sanitize.ts:9 / config.ts CONTROLLED_PROMPT）。
CONTROLLED_PROMPT = "dsh> "


class TerminalError(Exception):
    """带稳定 TerminalErrorCode 的 PTY 服务失败（对齐 index.ts:66-71）。"""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


class TerminalBackendCleanupError(Exception):
    """backend 未发布 setup 失败且自身清理也失败（对齐 types.ts:18-26）。

    成立时会把 backend 层销毁活资源失败一并上报，供 owner/service 拆解时追账。
    """

    def __init__(self, spawn_error, cleanup_error):
        super().__init__("PTY backend startup and cleanup both failed")
        self.spawn_error = spawn_error
        self.cleanup_error = cleanup_error


class Cancellation:
    """上游 AbortSignal 的同步微小载体：is_set()=aborted，abort(reason) 置位并留存原因。

    P1 单线程确定性面用；P2 接真 PTY 线程时供线程间中止判读。
    """

    def __init__(self, reason=None):
        self._event = threading.Event()
        self.reason = reason

    def is_set(self) -> bool:
        return self._event.is_set()

    def abort(self, reason) -> None:
        self.reason = reason
        self._event.set()

    def throw_if_aborted(self) -> None:
        if self._event.is_set():
            if isinstance(self.reason, BaseException):
                raise self.reason
            raise RuntimeError(str(self.reason) if self.reason is not None else "cancelled")


class _AnyCancellation:
    """AbortSignal.any 的同步微型载体：任一组成信号置位即置位（index.ts:164）。"""

    def __init__(self, parts):
        self._parts = parts

    def is_set(self) -> bool:
        return any(p.is_set() for p in self._parts)

    def throw_if_aborted(self) -> None:
        for part in self._parts:
            if part.is_set():
                part.throw_if_aborted()


def any_cancellation(parts) -> _AnyCancellation:
    """组合多个 Cancellation 为单一判读信号（对齐 index.ts:164 AbortSignal.any）。"""
    return _AnyCancellation([p for p in parts if p is not None])


def _utf8_bytes(text: str) -> int:
    """Node Buffer.byteLength(text, 'utf8') 的等价面：UTF-8 字节数。

    孤立代理项按 Node 的 3 字节编码计；相邻高+低代理项（一个 UTF-16 pair，
    即非 BMP 码点）按 4 字节计 —— 与 Node 对同一码位序列的记账一致，escaping
    载体喂入的孤立代理与真实码点都成立（bounded_buffer 字节账目用）。
    """
    total = 0
    index = 0
    size = len(text)
    while index < size:
        code = ord(text[index])
        if 0xD800 <= code <= 0xDBFF and index + 1 < size and 0xDC00 <= ord(text[index + 1]) <= 0xDFFF:
            total += 4
            index += 2
        elif code < 0x80:
            total += 1
            index += 1
        elif code < 0x800:
            total += 2
            index += 1
        else:
            total += 3 if code < 0x10000 else 4
            index += 1
    return total


# ---------- backend / operation 契约（types.ts:147-177 的 sync 载体） ----------


class TerminalSendOperation(Protocol):
    """一次 backend 会话的独占交互式 send 的可见面（types.ts:93-101）。"""

    @property
    def settled(self) -> bool:
        ...

    def read_output(self) -> dict:
        ...

    def cancel(self) -> bool:
        ...


class TerminalBackendSession(Protocol):
    """backend 持有的活会话（types.ts:147-163）。"""

    motd: str
    pid: int | None

    def start_send(self, request: dict) -> TerminalSendOperation:
        ...

    def read(self, request: dict) -> dict:
        ...

    def signal(self, signal: str) -> dict:
        ...

    def status(self) -> dict:
        ...

    def close(self, reason: str) -> None:
        ...


class TerminalBackend(Protocol):
    """一个 PTY 会话类型的可替换 provider（types.ts:165-171）。"""

    type: str

    def spawn(self, spec: dict) -> TerminalBackendSession:
        ...


def terminal_session_id(value: str) -> str:
    """品牌化 registry 铸造的会话 id（index.ts:78-80；sync 载体以纯 str 承载）。"""
    return value