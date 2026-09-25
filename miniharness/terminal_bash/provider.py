"""PTY 载体 seam（对齐 upstream subprocess/subprocess-local/src/terminal.ts 的等价面）。

TerminalHandle / TerminalOutputChannel / SubprocessForeground / SubprocessOutcome
契约给 LocalPtySession 消费；平台载体分叉：Windows → pywinpty ConPTY，POSIX →
stdlib pty + termios。POSIX 载体是独立模块（_posix.py），仅 POSIX 平台导入
（期 imports pty/termios 在 Windows 不存在）；Windows 载体在 _winpty.py。

载体差异（已登记）：上游 provider 在子进程内做「scrubbed ambient base + 终端
覆盖」两层环境（subprocess 的 ProcessEnvironmentSpec），本实现由调用方传完整
env、provider 直接透传 spawn；`inputWaiting` 上游来自 linux procfs 的
`%pwaitable` 启发式，Windows 无前台信息（inspect_foreground=None），POSIX
按乐观 True 近似。
"""

from __future__ import annotations

import sys
import threading

from ..seams.subprocess_env import scrubbed_parent_env

__all__ = [
    "SubprocessOutcome",
    "SubprocessForeground",
    "SubprocessTerminalActivity",
    "TerminalOutputChannel",
    "TerminalHandle",
    "spawn_terminal",
]


class SubprocessOutcome:
    __slots__ = ("exit_code", "signal")

    def __init__(self, exit_code: int | None, signal: str | None):
        self.exit_code = exit_code
        self.signal = signal


class SubprocessForeground:
    __slots__ = ("process_group_id", "input_waiting")

    def __init__(self, process_group_id: int | None, input_waiting: bool | None):
        self.process_group_id = process_group_id
        self.input_waiting = input_waiting


class SubprocessTerminalActivity:
    """provider 对 shell 生命周期与存活作业的观测（types.ts:244）。

    idle 需要正向 prompt 证据且没有观测到前台/后台/停止作业；revision 随输入、
    shell 迁移与进程观测变化，作用域限于本 handle。
    """

    __slots__ = ("state", "revision")

    def __init__(self, state: str, revision: int):
        self.state = state
        self.revision = revision


class TerminalOutputChannel:
    """可订阅的输出通道：data / end / error 同步回调 + 结束事件。

    真实载体的 reader 线程按序调用 emit_data → emit_error/emit_end；Fake 载体
    测试直接同步调用同一组 emit 方法。end 事件携带 outcome，保证在最后一块
    data 送达之后触发。
    """

    def __init__(self):
        self._data_listeners = []
        self._end_listener = None
        self._error_listener = None
        self._ended_event = threading.Event()
        self._outcome = None
        self._error = None

    def subscribe(self, on_data, on_end=None, on_error=None):
        self._data_listeners.append(on_data)
        if on_end is not None:
            self._end_listener = on_end
        if on_error is not None:
            self._error_listener = on_error

    def emit_data(self, data: bytes) -> None:
        if self._ended_event.is_set():
            return
        for listener in self._data_listeners:
            listener(data)

    def emit_error(self, error: BaseException) -> None:
        if self._ended_event.is_set():
            return
        self._error = error
        self._ended_event.set()
        if self._error_listener is not None:
            self._error_listener(error)

    def emit_end(self, outcome: SubprocessOutcome) -> None:
        if self._ended_event.is_set():
            return
        self._outcome = outcome
        self._ended_event.set()
        if self._end_listener is not None:
            self._end_listener(outcome)

    @property
    def outcome(self) -> SubprocessOutcome | None:
        return self._outcome

    @property
    def error(self) -> BaseException | None:
        return self._error

    def wait_end(self, timeout: float | None = None) -> bool:
        return self._ended_event.wait(timeout)


class TerminalHandle:
    """provider 持有的活 PTY；会话只消费本契约。

    `write`/`signal_foreground` 前作废 shell-activity 证据（上游 terminal.ts:143,197），
    `terminate` 后置静默并释放 activity（terminal.ts:222-233）。平台子类实现
    `_write`/`_signal_foreground`/`_terminate` 即可获得同一生命周期。
    """

    def __init__(self, pid: int, output: TerminalOutputChannel, shell_activity=None):
        self.pid = pid
        self.output = output
        self._shell_activity = shell_activity
        self._quiescent = False
        self._activity_key = ""
        self._activity_revision = 0

    def write(self, text: str) -> None:
        self._invalidate_activity()
        self._write(text)

    def _write(self, text: str) -> None:
        raise NotImplementedError

    def resize(self, cols: int, rows: int) -> None:
        raise NotImplementedError

    def inspect_foreground(self) -> SubprocessForeground | None:
        raise NotImplementedError

    def inspect_activity(self) -> SubprocessTerminalActivity:
        """观察命令活动，不解读输出、不把静默当完成（terminal.ts:166-194）。

        mini 载体按功能可及面收窄（verified-diffs §3.43）：无进程表 inspect
        （descendants/完整快照/会话存活）与 managed task count，故只凭
        shell-activity 证据 + 前台进程组判读；观测定不下来即 unknown。
        """
        state = "idle" if self._quiescent else "unknown"
        revision = 0
        if not self._quiescent:
            shell = (self._shell_activity.inspect(self.pid) if self._shell_activity is not None
                     else SubprocessTerminalActivity("unknown", 0))
            revision = shell.revision
            foreground = self.inspect_foreground()
            if foreground is None:
                state = "unknown"
            elif foreground.process_group_id != self.pid:
                state = "busy"
            else:
                state = shell.state
        key = f"{revision}:{state}"
        if key != self._activity_key:
            self._activity_key = key
            self._activity_revision += 1
        return SubprocessTerminalActivity(state, self._activity_revision)

    def signal_foreground(self, signal: str) -> int | None:
        self._invalidate_activity()
        return self._signal_foreground(signal)

    def _signal_foreground(self, signal: str) -> int | None:
        raise NotImplementedError

    def terminate(self) -> None:
        self._terminate()
        self._quiescent = True
        if self._shell_activity is not None:
            self._shell_activity.dispose()

    def _terminate(self) -> None:
        raise NotImplementedError

    def _invalidate_activity(self) -> None:
        if self._shell_activity is not None:
            self._shell_activity.invalidate()

    def wait(self, timeout: float | None = None) -> bool:
        return self.output.wait_end(timeout)


def spawn_terminal(spec: dict, platform: str | None = None) -> TerminalHandle:
    """按平台分叉创建真实 PTY。spec 需含 argv/cwd/env/rows/cols（optional）。

    环境语义对齐 subprocess 的 `targetEnvironment`：payload = 净身父环境
    （`seams/subprocess_env.scrubbed_parent_env`）叠加 spec 显式 env，再由
    provider 覆盖 `TERM=terminalType`（上游 spawnTerminal 同序）。POSIX 载体
    自身也继承父环境，此合并对 Windows 的 ConPTY（env 替换语义）是必要面。

    `spec.shellActivity is True` 时对纯 `bash|zsh -i` 注入私有启动集成
    （shell-activity.ts），改 argv/env 并把 activity 交给 handle；其余启动原样。

    抛错时 fail loud，不 fallback（黄金法则 #14）。
    """
    platform = platform or sys.platform
    resolved = dict(spec)
    env = {**scrubbed_parent_env(), **(spec.get("env") or {})}
    # 延迟导入：shell_activity 反向依赖本模块的 SubprocessTerminalActivity 契约。
    from .shell_activity import prepare_shell_activity
    activity = prepare_shell_activity(spec, env, platform)
    if activity is not None:
        resolved["argv"] = activity.argv
        env = dict(activity.env)
    if spec.get("terminalType"):
        env["TERM"] = spec["terminalType"]
    resolved["env"] = env
    if platform.startswith("win"):
        from ._winpty import WinptyTerminalHandle
        return WinptyTerminalHandle(resolved, activity)
    from ._posix import PtyTerminalHandle
    return PtyTerminalHandle(resolved, activity)