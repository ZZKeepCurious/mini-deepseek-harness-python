"""一个 PTY、一个有界终端仿真器与其可分离的浏览器跟随者（对齐 terminal-controller/src/terminal.ts）。

上游 @xterm/headless + SerializeAddon 的 Python 等价载体是 pyte（VT 仿真 + 可序列化
屏幕，见 design-terminal-domain.md §3）。进程生命周期独立于 follower 与组件生命周期：
分离不杀进程，显式 close 才终止。

载体差异（已登记）：上游以 async 队列有序化 write/resize/output；mini 单进程同步模型
以可重入锁保序，输出到达发生在 provider reader 线程（回调）。输入消费（上行命令的
序列检查、ack/refusal）由 `TerminalAttachment.submit` 同步承担，async 泵留在 web 层。
"""
from __future__ import annotations

import codecs
import threading
from typing import Any

import pyte

from ..terminal.types import _utf8_bytes
from .stream import TerminalFollower
from .types import TerminalControlUnavailable

__all__ = ["BrowserTerminal", "TerminalAttachment"]


def _row_text(row: Any) -> str:
    """把 pyte 的行（{col: Char} 或字符串）投影为文本。"""
    if isinstance(row, str):
        return row.rstrip()
    if not row:
        return ""
    return "".join(row[col].data for col in sorted(row)).rstrip()


class TerminalAttachment:
    """一次附加：独占输入控制 + 一张完整有界屏幕后跟有序输出（types.ts:71-79）。

    对齐上游 `BrowserTerminal.attach`（terminal.ts:82-111）：snapshot 为首帧，
    `writable` 指示本附加当前是否持有输入控制；上行的 `TerminalInput` 命令经
    `submit` 同步处理并按序产出 `ack` 帧（被拒时带 `refusal`）。较晚附加接管输入，
    旧附加保持只读。

    @param terminal - 所属 BrowserTerminal。
    @param limits - 输入字节与 PTY 尺寸上限（`TerminalEnvironment` 的
        `maxInputBytes` / `maxCols` / `maxRows`）。
    @param signal - 附加取消（不终止进程）；已中止的附加不获得输入控制权。
    @param input_window - 载体上行窗口（`RemoteUplinkWindow`）；载体无编码帧 inbox
        限额时缺省，snapshot 不带 `inputWindow`。
    """

    def __init__(self, terminal: "BrowserTerminal", limits: dict, signal=None,
                 input_window: dict | None = None):
        self._terminal = terminal
        self._limits = limits
        self._sequence = 0
        self.baseline, self.follower = terminal._attach(signal, input_window)

    @property
    def writable(self) -> bool:
        """本附加当前是否持有输入控制。"""
        return self._terminal.writable(self.follower)

    def submit(self, command: dict) -> None:
        """处理一条已解码的 `TerminalInput` 命令并产出其 `ack`（terminal.ts:113-152）。

        对齐上游 `consumeInput`：序列从 1 连续编号，缺口即失败整条附加（抛错，由
        web 层折进 follower）；`terminal/control-unavailable` 与已退出进程折成带
        `refusal` 的 `ack`（输出附加保持有效），其它失败照原样抛出。
        """
        self._sequence += 1
        sequence = command.get("sequence")
        if not _is_safe_int(sequence) or sequence != self._sequence:
            raise RuntimeError("Terminal input sequence has a gap")
        try:
            self._terminal._apply_command(command, self.follower, self._limits)
        except TerminalControlUnavailable as error:
            self.follower.push({"type": "ack", "sequence": sequence,
                                "refusal": error.details["reason"]})
            return
        except Exception:  # noqa: BLE001 - provider 写/调整失败：已退出即不运行，否则重抛
            if self._terminal.exited:
                self.follower.push({"type": "ack", "sequence": sequence,
                                    "refusal": "not-running"})
                return
            raise
        self.follower.push({"type": "ack", "sequence": sequence})

    def detach(self) -> None:
        self._terminal._detach(self.follower)


def _is_safe_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


class BrowserTerminal:
    """一个 PTY 进程、其恢复屏幕与输入控制（terminal.ts:14-170）。

    @param handle - 已分配的终端进程（provider TerminalHandle 契约）。
    @param info - 初始元数据（WebTerminalInfo）。
    @param scrollback - 保留的屏幕历史行数。
    @param max_buffered_bytes - 单个 follower 的排队字节上限。
    """

    def __init__(self, handle: Any, info: dict, scrollback: int, max_buffered_bytes: int):
        self._handle = handle
        self.info = info
        self._max_buffered_bytes = max_buffered_bytes
        self._screen = pyte.HistoryScreen(info["cols"], info["rows"], history=scrollback)
        self._stream = pyte.Stream(self._screen)
        self._decoder = codecs.getincrementaldecoder("utf-8")("replace")
        self._followers: set[TerminalFollower] = set()
        self._sequence = 0
        self._controller: TerminalFollower | None = None
        self._closing = False
        self._lock = threading.RLock()
        self._drained = threading.Event()
        self._consumed = False
        handle.output.subscribe(self._on_data, self._on_end, self._on_error)

    # ---------- 输出消费（provider reader 线程回调） ----------

    def _on_data(self, data: bytes) -> None:
        text = self._decoder.decode(bytes(data))
        if text:
            self._output(text)

    def _on_end(self, outcome: Any) -> None:
        remainder = self._decoder.decode(b"", True)
        if remainder:
            self._output(remainder)
        with self._lock:
            self._settle("exited", exit_code=getattr(outcome, "exit_code", None))

    def _on_error(self, error: BaseException) -> None:
        remainder = self._decoder.decode(b"", True)
        if remainder:
            self._output(remainder)
        with self._lock:
            self._settle("failed", error=str(error))

    def _settle(self, state: str, error: str | None = None,
                exit_code: int | None = None) -> None:
        if self._consumed:
            return
        self._consumed = True
        info = dict(self.info)
        info["state"] = state
        if state == "exited":
            info["exitCode"] = exit_code
        else:
            info["error"] = error
        self.info = info
        self._broadcast_state()
        self._drained.set()

    def _output(self, data: str) -> None:
        if not data:
            return
        with self._lock:
            if self._consumed:
                return
            try:
                self._stream.feed(data)
            except Exception as error:  # noqa: BLE001 - 仿真失败即终态
                self._settle("failed", error=str(error))
                return
            self._sequence += 1
            self._broadcast({"type": "output", "sequence": self._sequence, "data": data})

    # ---------- 附加 / 控制 ----------

    @property
    def closed(self) -> bool:
        """该身份是否已进入关闭（`close()` 起为真；清理失败可回退待重试）。

        `terminal/retain` 的持窗代次以此为终态判据（上游 retention.ts:45-61 的
        `lifetime` 闩：身份关闭即结束本代次，进程退出本身不结束）。
        """
        return self._closing

    @property
    def exited(self) -> bool:
        """provider 是否已结算（退出或失败，terminal.ts:24 的 `exited` 闩）。"""
        return self._consumed

    def _attach(self, signal, input_window: dict | None = None) -> tuple[dict, TerminalFollower]:
        if signal is not None:
            signal.throw_if_aborted()
        with self._lock:
            if signal is not None:
                signal.throw_if_aborted()
            follower = TerminalFollower(self._max_buffered_bytes)
            self._controller = follower
            self._broadcast_state()
            snapshot = {"type": "snapshot", "sequence": self._sequence,
                        "screen": self._serialize(), "info": self.info,
                        "writable": self.writable(follower)}
            if input_window is not None:
                snapshot["inputWindow"] = input_window
            self._followers.add(follower)
            return snapshot, follower

    def _detach(self, follower: TerminalFollower) -> None:
        with self._lock:
            self._followers.discard(follower)
            follower.close()
            if self._controller is follower:
                self._controller = None
                self._broadcast_state()

    def attach(self, limits: dict, signal=None,
               input_window: dict | None = None) -> TerminalAttachment:
        """附加输入/输出（较晚附加接管输入，旧的降为只读）。"""
        return TerminalAttachment(self, limits, signal, input_window)

    def _apply_command(self, command: dict, follower: TerminalFollower,
                       limits: dict) -> None:
        """在输入控制下执行一条已校验的 `TerminalInput`（terminal.ts:121-141）。"""
        with self._lock:
            self._require_controller(follower)
            if command["type"] == "write":
                if _utf8_bytes(command["data"]) > limits["maxInputBytes"]:
                    raise RuntimeError("Terminal input exceeds the configured limit")
                self._handle.write(command["data"])
                return
            if command["type"] == "resize":
                cols, rows = command["cols"], command["rows"]
                if (not _is_safe_int(cols) or cols < 2 or cols > limits["maxCols"]
                        or not _is_safe_int(rows) or rows < 1 or rows > limits["maxRows"]):
                    raise RuntimeError("Terminal dimensions exceed the configured limits")
                self._handle.resize(cols, rows)
                self._screen.resize(rows, cols)
                self.info = {**self.info, "cols": cols, "rows": rows}
                self._broadcast_state()
                return
            raise RuntimeError(f"unknown terminal input type: {command.get('type')!r}")

    def writable(self, follower: TerminalFollower) -> bool:
        """本 follower 当前是否持有输入控制（terminal.ts:154-156）。"""
        return (not self._consumed and not self._closing
                and self.info["state"] == "running" and self._controller is follower)

    def rename(self, title: str) -> None:
        """向每个附加视图发布显示名。"""
        with self._lock:
            self.info = {**self.info, "title": title}
            self._broadcast_state()

    def close(self) -> None:
        """先终止 provider 拥有的完整进程范围，再排空并释放屏幕；失败可重试。"""
        with self._lock:
            if self._closing:
                return
            self._closing = True
        try:
            self._handle.terminate()
            self._drained.wait()
        except BaseException:
            with self._lock:
                self._closing = False
            raise
        with self._lock:
            for follower in list(self._followers):
                follower.finish()
            self._followers.clear()

    # ---------- 内部 ----------

    def _require_controller(self, follower: TerminalFollower) -> None:
        if self._closing or self.info["state"] != "running":
            raise TerminalControlUnavailable("not-running")
        if self._controller is not follower:
            raise TerminalControlUnavailable("read-only")

    def _broadcast(self, frame: dict) -> None:
        for follower in list(self._followers):
            follower.push(frame)

    def _broadcast_state(self) -> None:
        """向每个 follower 独立发布当前元数据与其 `writable`（terminal.ts:158-160）。"""
        for follower in list(self._followers):
            follower.push({"type": "state", "info": self.info,
                           "writable": self.writable(follower)})

    def _serialize(self) -> str:
        """完整有界屏幕文本（含滚出历史），行尾空白裁剪、尾随空行去除。"""
        lines: list[str] = []
        history = getattr(self._screen, "history", None)
        if history is not None:
            lines.extend(_row_text(row) for row in history.top)
        lines.extend(_row_text(row) for row in self._screen.display)
        while lines and lines[-1] == "":
            lines.pop()
        return "\n".join(lines)
