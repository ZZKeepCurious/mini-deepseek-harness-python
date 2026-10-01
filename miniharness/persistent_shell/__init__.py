"""持久 shell 工具族共享引擎（对齐 packages/shell/tool-{bash,pwsh}-persistent）。

一个 owner（agent）作用域下池化一个持久 shell 会话，把模型的每条命令包上
start/end 标记 + 退出码，经 `ctx.terminals` 发送并驱动至结算，再从回滚缓冲里
抽取标记之间的输出与退出码，封顶渲染返回。shell 状态（当前目录、导出环境变量）
跨调用保持。

载体差异（登记，verified-diffs §3.5x）：
  * 上游 async poll（`await pause()` 循环 + `operation.done` Promise）；mini 同步
    backend 经 `ctx.get("terminals").await_send`（`run_until_settled`）驱动，单次 send
    通常一次结算即完成（就绪即提示符），循环仍保留以覆盖 stdin_read/超时/退出分支。
  * 上游 `Agent`/`AbortSignal`/`WeakMap` 缓存；mini 以 agent 对象为键的 dict +
    鸭子类型信号（`.is_set()`）。
"""
from __future__ import annotations

import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

from ..core.output_retention import truncate_without_splitting_surrogate_pair, utf16_length
from ..core.tools import Tool, ToolRegistry

__all__ = [
    "BASH_DIALECT",
    "PWSH_DIALECT",
    "DEFAULT_MAX_OUTPUT_CHARS",
    "DEFAULT_TIMEOUT_MS",
    "PersistentShells",
    "ShellDialect",
    "captured_command_output",
    "captured_partial_output",
    "create_persistent_tool",
    "install_persistent_tool",
    "maybe_truncate",
    "render_captured",
    "resolve_persistent_config",
    "retained_scrollback",
    "run_persistent_command",
]

#: 单条命令的墙钟上限（ms；对齐上游 Config @default 300000）。
DEFAULT_TIMEOUT_MS = 300_000
#: 返回命令输出的字符上限（对齐上游 Config @default 16000）。
DEFAULT_MAX_OUTPUT_CHARS = 16_000

#: 结果封顶时的固定前缀（逐字对齐上游 TRUNCATED_MESSAGE）。
TRUNCATED_MESSAGE = (
    "<response clipped><NOTE>To save on context only part of this file has been "
    "shown to you. You should retry this tool after you have searched inside the "
    "file with `grep -n` in order to find the line numbers of what you are looking "
    "for.</NOTE>"
)
#: 回滚缓冲丢头时的固定前缀（逐字对齐上游 LOST_PREFIX_MESSAGE）。
LOST_PREFIX_MESSAGE = (
    "<response clipped><NOTE>The beginning of this command output was dropped by "
    "the terminal scrollback limit. The following text is the earliest retained "
    "output.</NOTE>\n"
)
#: 命令超时但从未报退出码时的状态尾标。
TIMEOUT_STATUS_MARKER = "[Command timed out or OOM]"

#: 每页回滚行数（上游 SCROLLBACK_PAGE_LINES）。
SCROLLBACK_PAGE_LINES = 1_000
#: 轮询间隔（秒；上游 POLL_INTERVAL_MS=25）。
POLL_INTERVAL_SECONDS = 0.025

_STATUS_RE = re.compile(r"^(\d+)\r?\n")


def _quote_for_bash(value: str) -> str:
    """把命令包成单引号安全的 `$'...'`（对齐上游 quoteForBash）。"""
    escaped = (value.replace("\\", "\\\\").replace("'", "\\'")
               .replace("\r", "\\r").replace("\n", "\\n"))
    return f"$'{escaped}'"


def _wrap_bash(command: str, markers: dict) -> str:
    # 保持包命令在一条物理行上：交互式 bash 对内嵌换行会先打印 PS2，把提示符与
    # 标记源码泄进模型面结果。
    return (f"printf '%s\\n' {_quote_for_bash(markers['start'])}; "
            f"eval -- {_quote_for_bash(command)}; "
            f"__dsh_persistent_bash_status=$?; "
            f"printf '%s%s\\n' {_quote_for_bash(markers['end'])} "
            f"\"$__dsh_persistent_bash_status\"")


def _quote_for_pwsh(value: str) -> str:
    """把命令包成 PowerShell 双引号字符串体（对齐上游 quoteForPwsh）。"""
    return (value.replace("`", "``").replace('"', '`"').replace("$", "`$")
            .replace("\r", "").replace("\n", "`n").replace("\x1b", "`e"))


def _wrap_pwsh(command: str, markers: dict) -> str:
    body = _quote_for_pwsh(command)
    return (
        f"Write-Output '{markers['start']}'; $LASTEXITCODE = $null; $__s = 1; "
        f"try {{ Invoke-Expression \"{body}\"; $__ok = $? }} catch {{ $__ok = $false }}; "
        f"if ($null -ne $LASTEXITCODE) {{ $__s = [int]$LASTEXITCODE }} "
        f"else {{ $__s = if ($__ok) {{ 0 }} else {{ 1 }} }}; "
        f"Write-Output ('{markers['end']}' + $__s)")


@dataclass(frozen=True)
class ShellDialect:
    """一个持久 shell 变体的方言面（bash / pwsh）。"""

    name: str
    reset_message: str
    timeout_code: str
    default_description: str
    marker_prefix: str
    quote: Callable[[str], str]
    wrap: Callable[[str, dict], str]
    #: PSReadLine 会回显输入：pwsh 需从抽取结果里剥掉包命令源码。
    strip_wrapper: bool
    #: spawn 后要发的初始化命令（bash 关回显；pwsh 无）。
    setup_command: str | None


BASH_DIALECT = ShellDialect(
    name="bash",
    reset_message=(
        "The persistent bash shell was reset; the next bash call starts from the "
        "workspace with a fresh current directory and environment."),
    timeout_code="PERSISTENT_BASH_TIMEOUT",
    default_description=(
        "Run commands in a persistent bash shell. State, including the current "
        "directory and exported environment variables, persists across calls for "
        "this agent."),
    marker_prefix="__DSH_PERSISTENT_BASH",
    quote=_quote_for_bash,
    wrap=_wrap_bash,
    strip_wrapper=False,
    setup_command="stty -echo",
)

PWSH_DIALECT = ShellDialect(
    name="pwsh",
    reset_message=(
        "The persistent pwsh shell was reset; the next pwsh call starts from the "
        "workspace with a fresh current directory and environment."),
    timeout_code="PERSISTENT_PWSH_TIMEOUT",
    default_description=(
        "Run commands in a persistent PowerShell shell. State, including the "
        "current directory and exported environment variables, persists across "
        "calls for this agent."),
    marker_prefix="__DSH_PERSISTENT_PWSH",
    quote=_quote_for_pwsh,
    wrap=_wrap_pwsh,
    strip_wrapper=True,
    setup_command=None,
)


def make_markers(dialect: ShellDialect) -> dict:
    nonce = str(uuid.uuid4())
    return {"start": f"{dialect.marker_prefix}_START_{nonce}__",
            "end": f"{dialect.marker_prefix}_END_{nonce}:"}


def maybe_truncate(content: str, max_output_chars: int, incomplete: bool = False) -> str:
    """按字符上限裁尾；`incomplete` 时即便未超限也追加截断标记。

    超限时经 `truncate_without_splitting_surrogate_pair`（代理对安全截断，
    对齐 tool-bash-persistent/index.ts:58-63）。
    """
    if utf16_length(content) <= max_output_chars and not incomplete:
        return content
    if utf16_length(content) <= max_output_chars:
        return content + TRUNCATED_MESSAGE
    return (truncate_without_splitting_surrogate_pair(content, max_output_chars)
            + TRUNCATED_MESSAGE)


def _trim_trailing_newline(text: str) -> str:
    return re.sub(r"(?:\r?\n)+$", "", text)


def captured_command_output(snapshot: dict, markers: dict,
                            dialect: ShellDialect, wrapper: str) -> dict | None:
    """从滚动快照抽取一次已完结命令的输出与退出码；未完结 → None。"""
    text = snapshot["text"]
    end = text.rfind(markers["end"])
    if end < 0:
        return None
    status = _STATUS_RE.match(text[end + len(markers["end"]):])
    if status is None:
        return None
    start_marker = text.rfind(markers["start"], 0, end)
    start = 0 if start_marker < 0 else start_marker + len(markers["start"])
    captured = text[start:end]
    if dialect.strip_wrapper:
        captured = captured.replace(wrapper, "")
    captured = re.sub(r"^\r?\n", "", captured)
    return {"text": _trim_trailing_newline(captured),
            "incomplete": start_marker < 0,
            "exitCode": int(status.group(1))}


def captured_partial_output(snapshot: dict, markers: dict, dialect: ShellDialect,
                           wrapper: str, fallback: str,
                           fallback_truncated: bool = False) -> dict:
    """从滚动快照（不足时退回增量拼接）抽取未完结命令的部分输出。"""
    text = snapshot["text"]
    start_marker = text.rfind(markers["start"])
    if start_marker >= 0:
        captured = text[start_marker + len(markers["start"]):]
        captured = re.sub(r"^\r?\n", "", captured)
        return {"text": _trim_trailing_newline(captured), "incomplete": False}
    fallback_start = fallback.rfind(markers["start"])
    if fallback_start < 0:
        after_start = fallback
    else:
        after_start = re.sub(r"^\r?\n", "",
                             fallback[fallback_start + len(markers["start"]):])
    fallback_end = after_start.rfind(markers["end"])
    before_end = after_start if fallback_end < 0 else after_start[:fallback_end]
    if dialect.strip_wrapper:
        before_end = before_end.replace(wrapper, "")
    return {"text": _trim_trailing_newline(before_end),
            "incomplete": fallback_truncated or fallback_start < 0}


def _append_status_marker(content: str, marker: str | None) -> str:
    if marker is None:
        return content
    return marker if len(content) == 0 else f"{content}\n{marker}"


def render_captured(output: dict, max_output_chars: int) -> str:
    rendered = maybe_truncate(output["text"], max_output_chars, output["incomplete"])
    with_prefix = (LOST_PREFIX_MESSAGE + rendered
                   if output["incomplete"] and len(output["text"]) > 0 else rendered)
    marker = (f"[Command finished with exit code {output['exitCode']}]"
              if output.get("exitCode") is not None else None)
    return _append_status_marker(with_prefix, marker)


def _render_shell_exit_status(content: str, exit_code: int | None,
                              signal: str | None) -> str:
    if signal is not None:
        marker = f"[shell killed by signal: {signal}]"
    elif exit_code is not None:
        marker = f"[shell exited: code {exit_code}]"
    else:
        marker = "[shell exited]"
    return _append_status_marker(content, marker)


def _read_page(ctx: Any, owner: Any, session_id: str, offset: int, count: int) -> dict:
    return ctx.get("terminals").read(owner, session_id, {"offset": offset, "count": count})


def retained_scrollback(ctx: Any, owner: Any, session_id: str,
                        latest: dict | None = None) -> dict:
    """合并自最早可用起的多页回滚（对齐上游 retainedScrollback）。"""
    latest = latest if latest is not None else _read_page(
        ctx, owner, session_id, 0, SCROLLBACK_PAGE_LINES)
    pages = [latest["text"]] if latest["text"] else []
    offset = latest["lineEnd"]
    truncated = latest["truncated"]
    while True:
        if offset >= latest["totalLines"]:
            break
        page = _read_page(ctx, owner, session_id, offset, SCROLLBACK_PAGE_LINES)
        truncated = truncated or page["truncated"]
        if page["text"]:
            pages.insert(0, page["text"])
        if page["text"] == "" or page["lineEnd"] <= offset:
            break
        if page["lineEnd"] >= page["totalLines"]:
            break
        offset = page["lineEnd"]
    return {"text": "\n".join(pages), "truncated": truncated}


def _session_status(ctx: Any, owner: Any, session_id: str) -> dict | None:
    for snapshot in ctx.get("terminals").list(owner):
        if snapshot["sessionId"] == session_id:
            return snapshot["status"]
    return None


class PersistentShells:
    """一个 owner（agent）作用域下池化一个持久 shell 会话。"""

    def __init__(self, ctx: Any, dialect: ShellDialect, config: dict):
        self._ctx = ctx
        self._dialect = dialect
        self._config = config
        self._pending: dict = {}
        self._live: dict = {}
        self._lifetime = threading.Event()

    def get(self, owner: Any, signal: Any = None):
        existing = self._pending.get(owner)
        if existing is not None:
            return existing
        session_id = None
        try:
            cwd = _session_cwd(owner)
            request = {"type": self._config["backendType"]}
            if cwd is not None:
                request["cwd"] = cwd
            spawned = self._ctx.get("terminals").spawn(owner, request, _combine(signal, self._lifetime))
            session_id = spawned["sessionId"]
            self._live[owner] = session_id
            self._install_owner_cleanup(owner)
            if self._dialect.setup_command is not None:
                operation = self._ctx.get("terminals").start_send(owner, session_id, {
                    "text": self._dialect.setup_command,
                    "submit": True,
                    "signal": _combine(signal, self._lifetime),
                })
                self._ctx.get("terminals").await_send(
                    owner, session_id, operation, _combine(signal, self._lifetime))
                result = operation.result
                if (result["sessionStatus"]["kind"] == "exited"
                        or result["waitReason"] == "timeout"):
                    raise RuntimeError(
                        f"persistent {self._dialect.name} shell did not accept initialization")
            self._pending[owner] = session_id
            return session_id
        except Exception:
            self.reset(owner, f"persistent {self._dialect.name} initialization failed")
            raise

    def _install_owner_cleanup(self, owner: Any) -> None:
        ctx_attr = getattr(owner, "ctx", None)
        if ctx_attr is None or getattr(owner, "_persistent_shell_cleanup", False):
            return
        try:
            owner._persistent_shell_cleanup = True
        except Exception:  # noqa: BLE001 - 冻结对象跳过标记
            pass

        def cleanup() -> None:
            self._pending.pop(owner, None)
            self._live.pop(owner, None)

        ctx_attr.effect(lambda: cleanup, f"persistent-{self._dialect.name}-owner-cleanup")

    def reset(self, owner: Any, reason: str) -> None:
        self._pending.pop(owner, None)
        session_id = self._live.pop(owner, None)
        if session_id is not None:
            self._close(owner, session_id, reason)

    def _close(self, owner: Any, session_id: str, reason: str) -> None:
        try:
            for snapshot in self._ctx.get("terminals").list(owner):
                if snapshot["sessionId"] == session_id:
                    self._ctx.get("terminals").kill(owner, session_id, reason)
                    return
        except Exception:  # noqa: BLE001 - 会话可能已随 owner 清理消失
            pass

    def close_all(self) -> None:
        self._lifetime.set()
        for owner, session_id in list(self._live.items()):
            self._close(owner, session_id, f"persistent {self._dialect.name} disposed")
        self._live.clear()
        self._pending.clear()


def _session_cwd(owner: Any) -> str | None:
    session = getattr(owner, "session", None)
    meta = getattr(session, "meta", None) if session is not None else None
    if isinstance(meta, dict):
        cwd = meta.get("cwd")
        return cwd if isinstance(cwd, str) and cwd else None
    return None


class _SignalAdapter:
    """把鸭子类型信号（`.is_set()`/`.aborted`）适配为 terminal 载体信号（+`throw_if_aborted`）。"""

    def __init__(self, signal: Any):
        self._signal = signal

    def is_set(self) -> bool:
        if self._signal is None:
            return False
        aborted = getattr(self._signal, "aborted", None)
        if aborted is not None:
            return bool(aborted)
        is_set = getattr(self._signal, "is_set", None)
        return bool(is_set()) if callable(is_set) else False

    def throw_if_aborted(self) -> None:
        if self.is_set():
            reason = getattr(self._signal, "reason", None)
            if isinstance(reason, BaseException):
                raise reason
            raise RuntimeError("persistent shell operation aborted")


class _CombinedSignal:
    def __init__(self, parts):
        self._parts = [p for p in parts if p is not None]

    def is_set(self) -> bool:
        return any(p.is_set() for p in self._parts)

    def throw_if_aborted(self) -> None:
        for part in self._parts:
            thrower = getattr(part, "throw_if_aborted", None)
            if callable(thrower):
                thrower()
            elif getattr(part, "is_set", lambda: False)():
                raise RuntimeError("persistent shell operation aborted")


def _combine(signal: Any, lifetime: threading.Event) -> Any:
    parts = []
    if signal is not None:
        parts.append(_SignalAdapter(signal))
    parts.append(lifetime)
    return _CombinedSignal(parts)


def run_persistent_command(ctx: Any, shells: PersistentShells, dialect: ShellDialect,
                           config: dict, owner: Any, command: str,
                           signal: Any = None) -> str:
    """在 owner 的持久 shell 里执行一条命令并返回模型可见渲染结果。"""
    session_id = shells.get(owner, signal)
    markers = make_markers(dialect)
    wrapped = dialect.wrap(command, markers)
    first = True
    fallback = ""
    fallback_truncated = False
    deadline = (time.monotonic() + config["timeoutMs"] / 1000
                if config["timeoutMs"] else None)
    while True:
        status = _session_status(ctx, owner, session_id)
        if status is not None and status.get("kind") == "exited":
            return _respond_to_session_exit(
                ctx, shells, dialect, config, owner, session_id, status,
                markers, wrapped, fallback, fallback_truncated)
        if deadline is not None and time.monotonic() >= deadline:
            return _respond_to_timeout(
                ctx, shells, dialect, config, owner, session_id, markers, wrapped,
                fallback, fallback_truncated, config["timeoutMs"])
        try:
            operation = ctx.get("terminals").start_send(owner, session_id, {
                "text": wrapped if first else "",
                "submit": first,
                "signal": _combine(signal, _null_lifetime()),
            })
        except Exception:  # noqa: BLE001
            shells.reset(owner, f"persistent {dialect.name} send failed")
            raise
        first = False
        ctx.get("terminals").await_send(owner, session_id, operation, _combine(signal, _null_lifetime()))
        result = operation.result
        incremental = operation.read_output()
        if incremental["delta"]:
            fallback = fallback + incremental["delta"]
        else:
            fallback = result["viewport"]
        fallback_truncated = (fallback_truncated or incremental["truncated"]
                              or result["truncated"])
        latest = _read_page(ctx, owner, session_id, 0, SCROLLBACK_PAGE_LINES)
        if deadline is not None and time.monotonic() >= deadline:
            return _respond_to_timeout(
                ctx, shells, dialect, config, owner, session_id, markers, wrapped,
                fallback, fallback_truncated, config["timeoutMs"])
        snapshot = retained_scrollback(ctx, owner, session_id, latest)
        if markers["end"] in latest["text"]:
            complete = captured_command_output(snapshot, markers, dialect, wrapped)
            if complete is not None:
                return render_captured(complete, config["maxOutputChars"])
        if result["sessionStatus"].get("kind") == "exited":
            return _respond_to_session_exit(
                ctx, shells, dialect, config, owner, session_id,
                result["sessionStatus"], markers, wrapped, fallback, fallback_truncated)
        if result["waitReason"] == "stdin_read":
            partial = captured_partial_output(
                snapshot, markers, dialect, wrapped, fallback, fallback_truncated)
            return render_captured(partial, config["maxOutputChars"])
        time.sleep(POLL_INTERVAL_SECONDS)


def _null_lifetime() -> threading.Event:
    return _NEVER


_NEVER = threading.Event()


def _respond_to_session_exit(ctx: Any, shells: PersistentShells, dialect: ShellDialect,
                             config: dict, owner: Any, session_id: str, status: dict,
                             markers: dict, wrapper: str, fallback: str,
                             fallback_truncated: bool) -> str:
    snapshot = retained_scrollback(ctx, owner, session_id)
    shells.reset(owner, f"persistent {dialect.name} shell exited")
    partial = captured_partial_output(
        snapshot, markers, dialect, wrapper, fallback, fallback_truncated)
    body = render_captured(partial, config["maxOutputChars"])
    return _render_shell_exit_status(body, status.get("exitCode"), status.get("signal"))


def _respond_to_timeout(ctx: Any, shells: PersistentShells, dialect: ShellDialect,
                        config: dict, owner: Any, session_id: str, markers: dict,
                        wrapper: str, fallback: str, fallback_truncated: bool,
                        timeout_ms: int) -> str:
    snapshot = retained_scrollback(ctx, owner, session_id)
    shells.reset(owner, f"persistent {dialect.name} command timed out")
    partial = render_captured(
        captured_partial_output(snapshot, markers, dialect, wrapper, fallback,
                                fallback_truncated),
        config["maxOutputChars"])
    header = (f"Your command timed out after {round(timeout_ms / 1000)} seconds or "
              "experienced an OOM error. Below is partial output:")
    return "\n".join([header, _append_status_marker(partial, TIMEOUT_STATUS_MARKER),
                      dialect.reset_message])


def resolve_persistent_config(dialect: ShellDialect, config: dict | None) -> dict:
    """解析并校验一个持久 shell 工具的配置（对齐上游 Config 默认 + 装配期校验）。"""
    cfg = dict(config or {})
    resolved = {
        "backendType": cfg.get("backendType", "shell"),
        "timeoutMs": cfg.get("timeoutMs", DEFAULT_TIMEOUT_MS),
        "maxOutputChars": cfg.get("maxOutputChars", DEFAULT_MAX_OUTPUT_CHARS),
        "description": cfg.get("description", dialect.default_description),
    }
    if not isinstance(resolved["backendType"], str) or resolved["backendType"].strip() == "":
        raise ValueError(f"tool-{dialect.name}-persistent: backendType must be non-empty")
    for name in ("timeoutMs", "maxOutputChars"):
        value = resolved[name]
        if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
            raise ValueError(
                f"tool-{dialect.name}-persistent: {name} must be a positive integer")
    if not isinstance(resolved["description"], str) or resolved["description"].strip() == "":
        raise ValueError(f"tool-{dialect.name}-persistent: description must be non-empty")
    return resolved


def create_persistent_tool(ctx: Any, dialect: ShellDialect,
                           config: dict | None = None) -> Tool:
    """构造一个持久 shell 模型工具（不注册）。"""
    resolved = resolve_persistent_config(dialect, config)
    shells = PersistentShells(ctx, dialect, resolved)
    ctx.effect(lambda: shells.close_all, f"tool-{dialect.name}-persistent shell cleanup")
    locks: dict = {}

    def _lock_for(owner: Any) -> threading.Lock:
        lock = locks.get(owner)
        if lock is None:
            lock = threading.Lock()
            locks[owner] = lock
        return lock

    def execute(args: dict, exec_: Any) -> str:
        command = args.get("command")
        if not isinstance(command, str) or command.strip() == "":
            raise ValueError("command must be a non-empty string")
        owner = getattr(exec_, "agent", None)
        if owner is None:
            raise RuntimeError(f"{dialect.name} requires an owning agent session")
        with _lock_for(owner):
            return run_persistent_command(
                ctx, shells, dialect, resolved, owner, command,
                getattr(exec_, "signal", None))

    return Tool(
        name=dialect.name,
        description=resolved["description"],
        parameters={
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": f"The {dialect.name} command to run. "
                                   "Relative path is preferred in the command.",
                },
            },
            "required": ["command"],
        },
        output={"schema": {"type": "string"}},
        execute=execute,
        render=lambda _args, value: [{"type": "text", "text": value}],
        present_call=lambda args: {"card": "terminal", "title": args.get("command", "")},
    )


def install_persistent_tool(ctx: Any, registry: ToolRegistry, dialect: ShellDialect,
                            config: dict | None = None) -> Tool | None:
    """把持久 shell 工具注册进 `registry`（幂等）。"""
    existing = registry.resolve(dialect.name)
    if existing is not None:
        return existing
    tool = create_persistent_tool(ctx, dialect, config)
    registry.register(tool)
    return tool
