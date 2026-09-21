"""POSIX pty 载体（仅在 POSIX 平台导入，期依赖 pty/termios/fcntl）。

pty.fork 会话 + master fd 读线程泵输出；foreground 用 TIOCGPGRP，信号经
killpg 送前台组。`inputWaiting` 无 procfs 近似 → 乐观 True（已登记差异）。
"""

from __future__ import annotations

import fcntl
import os
import select
import signal
import struct
import termios
import threading

from .provider import SubprocessForeground, SubprocessOutcome, TerminalHandle, TerminalOutputChannel

__all__ = ["PtyTerminalHandle"]

#: 前台 SIGKILL 保护线（subprocess-local terminal.ts：不禁送 shell pgid 自杀）。
_SHELL_SIGNAL_GUARD = ("SIGKILL",)

_SIGNAL_MAP = {
    "SIGINT": signal.SIGINT,
    "SIGTERM": signal.SIGTERM,
    "SIGKILL": signal.SIGKILL,
    "SIGHUP": signal.SIGHUP,
    "SIGTSTP": signal.SIGTSTP,
    "SIGCONT": signal.SIGCONT,
    "SIGQUIT": signal.SIGQUIT,
}


class PtyTerminalHandle(TerminalHandle):
    def __init__(self, spec: dict):
        self._rows = spec.get("rows") or 40
        self._cols = spec.get("cols") or 160
        pid, master_fd = pty.fork()
        if pid == 0:
            self._child_entry(spec)
        self._master_fd = master_fd
        self._fg_cache = None
        try:
            self._apply_winsize()
        except OSError:
            pass
        super().__init__(pid, TerminalOutputChannel())
        threading.Thread(target=self._read_loop, name="pty-reader", daemon=True).start()

    def _child_entry(self, spec: dict) -> None:
        try:
            if spec.get("cwd"):
                os.chdir(spec["cwd"])
            if spec.get("env"):
                os.environ.update(spec["env"])
            os.execvp(spec["argv"][0], spec["argv"])
        except BaseException:
            os._exit(127)

    def _read_loop(self) -> None:
        try:
            while True:
                ready, _, _ = select.select([self._master_fd], [], [], 0.5)
                if not ready:
                    continue
                chunk = os.read(self._master_fd, 4096)
                if not chunk:
                    break
                self.output.emit_data(chunk)
        except (OSError, ValueError):
            pass
        finally:
            self._close_fd()
            _, status = os.waitpid(self.pid, 0)
            exit_code = None
            sig = None
            if os.WIFEXITED(status):
                exit_code = os.WEXITSTATUS(status)
            elif os.WIFSIGNALED(status):
                sig = signal.Signals(os.WTERMSIG(status)).name
            self.output.emit_end(SubprocessOutcome(exit_code, sig))

    def _close_fd(self) -> None:
        try:
            os.close(self._master_fd)
        except OSError:
            pass

    def _apply_winsize(self) -> None:
        fcntl.ioctl(self._master_fd, termios.TIOCSWINSZ, struct.pack("HHHH", self._rows, self._cols, 0, 0))

    def write(self, text: str) -> None:
        os.write(self._master_fd, text.encode("utf-8"))

    def resize(self, cols: int, rows: int) -> None:
        self._rows, self._cols = rows, cols
        self._apply_winsize()

    def inspect_foreground(self) -> SubprocessForeground | None:
        try:
            pgid = termios.tcgetpgrp(self._master_fd)
            self._fg_cache = SubprocessForeground(pgid, True)
            return self._fg_cache
        except OSError:
            return self._fg_cache

    def signal_foreground(self, signal: str) -> int | None:
        if signal in _SHELL_SIGNAL_GUARD:
            raise RuntimeError(f"refusing to send {signal} to the shell process group")
        foreground = self.inspect_foreground()
        target = foreground.process_group_id if foreground is not None else self.pid
        os.killpg(target, _SIGNAL_MAP[signal])
        return target

    def terminate(self) -> None:
        try:
            os.kill(self.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass