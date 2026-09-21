"""本地 PTY 会话（对齐 upstream terminal-bash/src/session.ts，同步载体）。

契约面照抄：独占 send 状态机（active/interrupting/polling）、prompt+marker 就绪、
exact-probe / inferred_idle / timeout / session_exit 五种结算、scrollback 双限
缓冲、终端协议应答器、transport failure 摄取与 close 静态序。

载体差异（已登记 verified-diffs §3.30）：
* 无 Promise/定时器：emulator feed 与 response write 同步排空（emulatorWrites/
  responseWrites/emulatorBuffer 链条坍缩）；deadline 由 poll 首判读模拟独立
  定时器；readiness 由 run_until_settled 驱轮询，poll_readiness 仍可直接调用。
* initialize/startup 在 spawn 内同步阻塞至就绪（上游 await 的同步等价形态）。
* 结算后 session 内嵌 statusValue 在 close 窗口保持 running（closeOnce 序）。
"""

from __future__ import annotations

import codecs
import threading
import time

from .emulator import TerminalProtocolEmulator
from .provider import TerminalHandle  # noqa: F401  (protocol param)

from ..terminal.bounded_buffer import BoundedTextBuffer, read_scrollback, utf8_tail
from ..terminal.operation import LocalSendOperation
from ..terminal.sanitize import CONTROLLED_PROMPT, TerminalSanitizer
from ..terminal.types import TerminalError

__all__ = ["LocalPtySession"]

_SIGINT = "SIGINT"


def _coerce_failure(error) -> Exception:
    return error if isinstance(error, Exception) else RuntimeError(str(error))


class LocalPtySession:
    """一条 provider 持有的 PTY 的 backend 会话。"""

    def __init__(self, handle: TerminalHandle, config: dict, clock=None):
        self.pid = handle.pid
        self.motd = ""
        self._handle = handle
        self._config = config
        self._clock = clock or (lambda: time.monotonic_ns() // 1_000_000)
        self._decoder = codecs.getincrementaldecoder("utf-8")()
        self._emulator = TerminalProtocolEmulator(config["cols"], config["rows"])
        self._sanitizer = TerminalSanitizer(config["maxReadBytes"])
        self._scrollback = BoundedTextBuffer(
            config["scrollbackMaxBytes"], config["scrollbackLines"])
        self._status_value = {"kind": "running"}
        self._active = None
        self._active_signal = None
        self._active_deadline_ms = None
        self._interrupting = None
        self._polling = False
        self._polling_ready = None
        self._prompt_seen = False
        self._prompt_text_seen = False
        self._prompt_tail = ""
        self._shell_pgid = None
        self._initializing = False
        self._last_output_at = self._now_ms()
        self._closing = False
        self._closed = False
        self._transport_failure = None
        self._output_ended = False
        handle.output.subscribe(self._on_terminal_data, self._on_terminal_end, self._on_terminal_error)

    def __repr__(self) -> str:  # pragma: no cover - debug aid
        return f"<LocalPtySession pid={self.pid} status={self.status()}>"

    # ---------- 时钟 / 驱动 ----------

    def _now_ms(self) -> int:
        return self._clock()

    def _sleep_poll(self) -> None:
        time.sleep(self._config["pollIntervalMs"] / 1000)

    # ---------- 公开面 ----------

    def initialize(self, signal=None) -> None:
        """捕获启动输出直到首个就绪证明（session.ts:312-326）。

        退出/超时 → RuntimeError；stdin_read/inferred_idle → motd=viewport。
        """
        self._initializing = True
        try:
            request = {"text": "", "submit": False}
            if signal is not None:
                request["signal"] = signal
            operation = self.start_send(request)
            self.run_until_settled(operation, signal)
            result = operation.result
            if result["waitReason"] == "session_exit":
                raise RuntimeError("PTY shell exited during startup")
            if result["waitReason"] == "timeout":
                raise RuntimeError("PTY shell did not reach readiness before startup timeout")
            self.motd = result["viewport"]
        finally:
            self._initializing = False

    def run_until_settled(self, operation=None, signal=None):
        """驱 readiness 轮询直至结算（同步载体的 await done 等价面）。

        供 backend 启动 / 工具层 / 测试显式驱动；signal 在每个轮询交界判读，
        等价 AbortSignal abort 监听路径。
        """
        operation = operation or self._active
        if operation is None:
            raise RuntimeError("no active send to drive")
        while not operation.settled:
            if signal is not None and signal.is_set():
                operation.cancel()
            self.poll_readiness(operation)
            if operation.settled:
                return operation
            if signal is not None and signal.is_set():
                operation.cancel()
            self._sleep_poll()
        return operation

    def run_until_settled_until(self, operation=None, signal=None, remaining_ms: int | None = None):
        """带绝对 deadline 的驱动面（pwsh 启动整体限时的同步等价形态）。

        deadline 到顶而不结算 → settle timeout（retain_ownership 语义同
        poll_readiness 的 deadline 首判读）。
        """
        operation = operation or self._active
        if operation is None:
            raise RuntimeError("no active send to drive")
        deadline = None if remaining_ms is None else self._now_ms() + remaining_ms
        while not operation.settled:
            if signal is not None and signal.is_set():
                operation.cancel()
            self.poll_readiness(operation)
            if operation.settled:
                return operation
            if signal is not None and signal.is_set():
                operation.cancel()
            if deadline is not None and self._now_ms() >= deadline:
                self._settle_active("timeout", retain_ownership=self._interrupting is operation)
                return operation
            self._sleep_poll()
        return operation

    def start_send(self, request: dict) -> LocalSendOperation:
        """启动一次独占 send；活跃未结 → SEND_ACTIVE（session.ts:328-363）。"""
        if self._closing:
            raise RuntimeError("PTY session is closing")
        if self._status_value["kind"] == "exited":
            raise RuntimeError("PTY session has exited")
        if self._active is not None:
            draining = ""
            if self._interrupting is not None:
                draining = " or draining foreground interrupt"
            raise TerminalError(f"PTY session already has an active send{draining}", "SEND_ACTIVE")
        self._throw_if_aborted(request.get("signal"))

        operation = LocalSendOperation(self._config["maxReadBytes"], self._now_ms())
        operation.set_on_cancel(lambda: self._interrupt(operation))
        self._active = operation
        self._reset_readiness_evidence()
        self._active_signal = request.get("signal")
        self._active_deadline_ms = self._now_ms() + self._config["timeoutMs"]
        self._begin_send(operation, request)
        return operation

    def read(self, request: dict | None = None) -> dict:
        return read_scrollback(self._scrollback.snapshot(), self._config["maxReadBytes"], dict(request or {}))

    def signal(self, signal: str) -> dict:
        if self._closing:
            raise RuntimeError("PTY session is closing")
        target_pgid = self._handle.signal_foreground(signal)
        return {"delivered": True, "targetPgid": target_pgid}

    def status(self) -> dict:
        return self._status_value

    def close(self, reason: str) -> None:
        self._closing = True
        if self._closed:
            return
        self._closed = True
        self._stop_readiness_polling()
        self._emulator.close()
        try:
            self._handle.terminate()
        except Exception as error:
            raise RuntimeError(f"PTY cleanup failed ({reason})") from error
        self._settle_active("session_exit")
        self._handle.wait(self._config["disposeGraceMs"] / 1000)
        if self._transport_failure is not None:
            raise self._transport_failure

    # ---------- 数据 / 退出 ----------

    def _on_terminal_data(self, chunk: bytes) -> None:
        if self._closed:
            return
        try:
            data = self._decoder.decode(bytes(chunk))
        except UnicodeDecodeError as error:
            self._on_transport_failure(error)
            return
        self._queue_emulator_data(data)
        self._on_data(data)

    def _on_terminal_end(self, outcome) -> None:
        self._on_data(self._decoder.decode(b"", final=True))
        self._append_output(self._sanitizer.flush())
        self._emulator.close()
        self._output_ended = True
        self._on_exit(outcome)

    def _on_terminal_error(self, error: BaseException) -> None:
        self._emulator.close()
        self._output_ended = True
        self._on_transport_failure(error)

    def _on_exit(self, outcome) -> None:
        if self._transport_failure is not None:
            return
        self._status_value = {"kind": "exited", "exitCode": outcome.exit_code, "signal": outcome.signal}
        self._settle_active("session_exit")

    def _on_transport_failure(self, error) -> None:
        failure = _coerce_failure(error)
        if self._transport_failure is None:
            self._transport_failure = failure
        self._status_value = {"kind": "exited", "exitCode": None, "signal": None}
        self._emulator.close()
        self._fail_active(failure)
        try:
            self._handle.terminate()
        except Exception:
            pass

    def _on_data(self, data: str) -> None:
        sanitized = self._sanitizer.push(data)
        self._append_output(sanitized["text"])
        if sanitized["prompt"]:
            self._prompt_seen = True
            self._prompt_tail = ""
            self._last_output_at = self._now_ms()
        if self._prompt_seen and "promptTail" in sanitized:
            remaining = max(0, len(CONTROLLED_PROMPT) + 1 - len(self._prompt_tail))
            self._prompt_tail += sanitized["promptTail"][:remaining]
            if len(sanitized["promptTail"]) > remaining:
                self._prompt_tail = f"{CONTROLLED_PROMPT}\0"
            self._prompt_text_seen = self._prompt_tail == CONTROLLED_PROMPT

    def _append_output(self, text: str) -> None:
        if len(text) == 0:
            return
        self._last_output_at = self._now_ms()
        self._scrollback.append(text)
        if self._active is not None:
            self._active.append(text)

    # ---------- send 生命周期 ----------

    def _begin_send(self, operation: LocalSendOperation, request: dict) -> None:
        self._drain_terminal_protocol()
        try:
            foreground = self._inspect_foreground()
        except Exception as error:
            if self._active is operation and not self._closing and self._interrupting is not operation:
                self._fail_active(error)
            return
        if self._active is not operation or self._closing or self._interrupting is operation:
            return
        operation.set_initial_foreground(foreground)
        input_text = f"{request.get('text', '')}{'\r' if request.get('submit') else ''}"
        if len(input_text) > 0 and not operation.cancel_requested:
            self._reset_readiness_evidence()
            try:
                self._handle.write(input_text)
            except Exception as error:
                if self._active is operation and not self._closing:
                    if operation.settled:
                        self._release_settled_active()
                    else:
                        self._fail_active(error)
                return
        if operation.cancel_requested:
            return
        if self._active is operation and operation.settled:
            self._release_settled_active()
            return
        if self._active is operation and not self._closing:
            self._polling_ready = operation

    def poll_readiness(self, operation: LocalSendOperation) -> None:
        """单次 readiness 判读（session.ts:549-600 的同步面，频率由驱动方决定）。"""
        if self._active is not operation or self._polling:
            return
        self._polling = True
        try:
            if self._active_deadline_ms is not None and self._now_ms() >= self._active_deadline_ms:
                self._settle_active("timeout", retain_ownership=self._interrupting is operation)
                return
            if self._status_value["kind"] == "exited":
                self._settle_active("session_exit")
                return
            self._drain_terminal_protocol()
            foreground = self._inspect_foreground()
            if self._active is not operation or self._closing or self._interrupting is operation:
                return
            now = self._now_ms()
            idle_for = now - self._last_output_at
            if self._prompt_seen and foreground is not None and self._shell_pgid is None:
                self._shell_pgid = foreground["processGroupId"]
            if (self._prompt_seen and self._prompt_text_seen
                    and idle_for >= self._config["pollIntervalMs"]
                    and (foreground is None or foreground["processGroupId"] == self._shell_pgid)):
                self._settle_active("stdin_read")
                return
            elapsed = now - operation.started_at
            startup_has_output = (not self._initializing) or (not self._scrollback.is_empty)
            accepts_stdin_wait = (
                startup_has_output
                and foreground is not None
                and operation.accepts_stdin_wait(foreground["processGroupId"], foreground["inputWaiting"]))
            if elapsed >= self._config["exactProbeAfterMs"] and accepts_stdin_wait:
                self._settle_active("stdin_read")
                return
            handoff_grace = self._config["handoffGraceMs"] if self._prompt_seen else 0
            if startup_has_output and idle_for >= self._config["idleSilenceMs"] + handoff_grace:
                self._settle_active("inferred_idle")
                return
        finally:
            self._polling = False

    def _interrupt(self, operation: LocalSendOperation) -> None:
        if self._active is not operation:
            return
        self._interrupting = operation
        self._stop_readiness_polling()
        self._interrupt_once(operation)

    def _interrupt_once(self, operation: LocalSendOperation) -> None:
        try:
            self._handle.signal_foreground(_SIGINT)
        except Exception as error:
            self._interrupting = None
            if self._active is operation and not self._closing:
                self._on_transport_failure(error)
            return
        self._interrupting = None
        if self._active is operation and operation.settled:
            self._release_settled_active()
        elif self._active is operation and not self._closing:
            self._polling_ready = operation

    # ---------- 协议应答 ----------

    def _queue_emulator_data(self, data: str) -> None:
        if self._emulator.closed:
            return
        try:
            replies = self._emulator.feed(data)
            if replies:
                self._handle.write(replies)
        except Exception as error:
            if not self._closing:
                self._on_transport_failure(error)

    def _drain_terminal_protocol(self) -> None:
        # 同步载体：emulator feed 与 response write 在同一调用内排空，
        # 无需上游 emulatorWrites/responseWrites 双链等待。
        return

    def _inspect_foreground(self) -> dict | None:
        foreground = self._handle.inspect_foreground()
        if foreground is None:
            return None
        return {"processGroupId": foreground.process_group_id, "inputWaiting": foreground.input_waiting}

    # ---------- 结算 / 清理 ----------

    def _reset_readiness_evidence(self) -> None:
        self._last_output_at = self._now_ms()
        self._prompt_seen = False
        self._prompt_text_seen = False
        self._prompt_tail = ""
        self._shell_pgid = None

    def _settle_active(self, wait_reason: str, retain_ownership: bool = False) -> None:
        operation = self._active
        if operation is None:
            return
        scrollback_truncated = self._scrollback.truncated
        if retain_ownership:
            self._stop_readiness_polling()
        else:
            self._clear_active()
        operation.settle(wait_reason, self._status_value, scrollback_truncated)

    def _release_settled_active(self) -> None:
        operation = self._active
        if operation is None or not operation.settled:
            return
        if self._interrupting is operation:
            return
        self._clear_active()

    def _clear_active(self) -> None:
        self._stop_readiness_polling()
        self._active_signal = None
        self._interrupting = None
        self._active = None

    def _fail_active(self, error) -> None:
        operation = self._active
        if operation is None:
            return
        self._clear_active()
        operation.fail(error)

    def _stop_readiness_polling(self) -> None:
        self._polling_ready = None

    @staticmethod
    def _throw_if_aborted(signal) -> None:
        if signal is not None and signal.is_set():
            raise RuntimeError("PTY send aborted before write")