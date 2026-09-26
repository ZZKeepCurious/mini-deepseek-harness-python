"""一个 PTY、一个有界终端仿真器与其可分离的浏览器跟随者（对齐 terminal-controller/src/terminal.ts）。

上游 @xterm/headless + SerializeAddon 的 Python 等价载体是 pyte（VT 仿真 + 可序列化
屏幕，见 design-terminal-domain.md §3）。进程生命周期独立于 follower 与组件生命周期：
分离不杀进程，显式 close 才终止。

载体差异（已登记）：上游以 async 队列有序化 write/resize/output；mini 单进程同步模型
以可重入锁保序，输出到达发生在 provider reader 线程（回调）。
"""
from __future__ import annotations

import codecs
import threading
from typing import Any, Iterator

import pyte

from .stream import TerminalFollower
from .types import TerminalControlUnavailable

__all__ = ["BrowserTerminal", "TerminalFollow"]


def _row_text(row: Any) -> str:
    """把 pyte 的行（{col: Char} 或字符串）投影为文本。"""
    if isinstance(row, str):
        return row.rstrip()
    if not row:
        return ""
    return "".join(row[col].data for col in sorted(row)).rstrip()


class TerminalFollow:
    """一次附加：一张完整有界屏幕（baseline）后跟有序输出（types.ts:59-63）。

    @param terminal - 所属 BrowserTerminal。
    @param attachment_id - 新独占输入附加身份。
    @param signal - 附加取消（不终止进程）；已中止的附加不获得输入控制权。
    """

    def __init__(self, terminal: "BrowserTerminal", attachment_id: str, signal=None):
        self._terminal = terminal
        self.baseline, self.follower = terminal._attach(attachment_id, signal)

    def detach(self) -> None:
        self._terminal._detach(self.follower)

    def read(self) -> Iterator[dict]:
        """同步帧源：先 baseline，再跟随队列（生成器结束时自动分离）。"""
        try:
            yield self.baseline
            yield from self.follower.read()
        finally:
            self.detach()


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
        self._controller: tuple[str, TerminalFollower] | None = None
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
        self._broadcast({"type": "state", "info": self.info})
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

    def _attach(self, attachment_id: str, signal) -> tuple[dict, TerminalFollower]:
        if signal is not None:
            signal.throw_if_aborted()
        with self._lock:
            if signal is not None:
                signal.throw_if_aborted()
            follower = TerminalFollower(self._max_buffered_bytes)
            self._controller = (attachment_id, follower)
            self.info = {**self.info, "controllerId": attachment_id}
            self._broadcast({"type": "state", "info": self.info})
            snapshot = {"type": "snapshot", "sequence": self._sequence,
                        "screen": self._serialize(), "info": self.info}
            self._followers.add(follower)
            return snapshot, follower

    def _detach(self, follower: TerminalFollower) -> None:
        with self._lock:
            self._followers.discard(follower)
            follower.close()
            if self._controller is not None and self._controller[1] is follower:
                self._controller = None
                info = {k: v for k, v in self.info.items() if k != "controllerId"}
                self.info = info
                self._broadcast({"type": "state", "info": info})

    def follow(self, attachment_id: str, signal=None) -> TerminalFollow:
        """附加并取得 baseline + 有序输出（较晚附加接管输入，旧的降为只读）。"""
        return TerminalFollow(self, attachment_id, signal)

    def write(self, attachment_id: str, data: str) -> None:
        """投递原始输入（不做命令解释）。"""
        with self._lock:
            self._require_controller(attachment_id)
            self._handle.write(data)

    def resize(self, attachment_id: str, cols: int, rows: int) -> None:
        """按与输出相同的操作顺序调整 PTY 与恢复屏幕。"""
        with self._lock:
            self._require_controller(attachment_id)
            self._handle.resize(cols, rows)
            self._screen.resize(rows, cols)
            self.info = {**self.info, "cols": cols, "rows": rows}
            self._broadcast({"type": "state", "info": self.info})

    def rename(self, title: str) -> None:
        """向每个附加视图发布显示名。"""
        with self._lock:
            self.info = {**self.info, "title": title}
            self._broadcast({"type": "state", "info": self.info})

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

    def _require_controller(self, attachment_id: str) -> None:
        if self._closing or self.info["state"] != "running":
            raise TerminalControlUnavailable("not-running")
        if self._controller is None or self._controller[0] != attachment_id:
            raise TerminalControlUnavailable("read-only")

    def _broadcast(self, frame: dict) -> None:
        for follower in list(self._followers):
            follower.push(frame)

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
