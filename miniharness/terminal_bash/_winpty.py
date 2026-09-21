"""Windows ConPTY 载体（provider.py 的平台分支）。

pywinpty PtyProcess 包一个 conhost/ConPTY：reader 线程泵 read() 直到 EOFError，
outcome 取 exitstatus；Windows 无 procfs 前台组 → inspect_foreground=None，
SIGINT 以 ^C（0x03）写入、SIGTSTP 等不可达前台信号 fail loud。

载体差异（已登记，verified-diffs §3.30）：Windows 前台交互信号面向 ConPTY
控制台写字节而非 kill 进程组；terminate() 是创建会话唯一的进程树终止路径。
"""

from __future__ import annotations

import threading

from ..terminal_bash.provider import (
    SubprocessForeground,
    SubprocessOutcome,
    TerminalHandle,
    TerminalOutputChannel,
)

__all__ = ["WinptyTerminalHandle"]


class WinptyTerminalHandle(TerminalHandle):
    """pywinpty ConPTY 载体。"""

    _CONSOLE_SIGNALS = {"SIGINT", "SIGBREAK"}
    _UNREACHABLE_SIGNALS = {"SIGTSTP", "SIGHUP", "SIGQUIT", "SIGCONT", "SIGTERM", "SIGKILL"}

    def __init__(self, spec: dict):
        from winpty import PtyProcess
        self._proc = PtyProcess.spawn(
            list(spec["argv"]),
            cwd=spec.get("cwd") or None,
            env=dict(spec.get("env") or {}),
            dimensions=(spec.get("rows") or 40, spec.get("cols") or 160),
        )
        channel = TerminalOutputChannel()
        super().__init__(self._proc.pid, channel)
        reader = threading.Thread(target=self._read_loop, name="winpty-reader", daemon=True)
        reader.start()

    def _read_loop(self) -> None:
        try:
            while True:
                chunk = self._proc.read()
                if not chunk:
                    if not self._proc.isalive():
                        break
                    continue
                self.output.emit_data(chunk.encode("utf-8"))
        except (EOFError, OSError):
            pass
        finally:
            exit_code = None
            try:
                if not self._proc.isalive():
                    exit_code = int(self._proc.exitstatus)
            except (AttributeError, OSError):
                pass
            self.output.emit_end(SubprocessOutcome(exit_code, None))
            try:
                self._proc.close()
            except Exception:
                pass

    def write(self, text: str) -> None:
        # pywinpty 契约：PtyProcess.write 收 str（内部编码），read 回 str。
        self._proc.write(text)

    def resize(self, cols: int, rows: int) -> None:
        self._proc.setwinsize(rows, cols)

    def inspect_foreground(self) -> SubprocessForeground | None:
        return None

    def signal_foreground(self, signal: str) -> int | None:
        if signal in self._UNREACHABLE_SIGNALS:
            raise RuntimeError(f"signal {signal} is not deliverable to a ConPTY foreground; use close() instead")
        if signal == "SIGINT":
            self._proc.sendintr()
            return None
        self._proc.sendcontrol("c")
        return None

    def terminate(self) -> None:
        try:
            self._proc.terminate()
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        try:
            self._proc.close()
        except Exception:
            pass