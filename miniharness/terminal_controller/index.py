"""Session 作用域的浏览器终端控制器（对齐 packages/api/terminal-controller/src/index.ts）。

承载 `ctx.terminalController`：`environment` / `shells` / `list` / `create` / `follow`
/ `write` / `resize` / `rename` / `close`。终端进程独立于浏览器与 follower 生命周期；
保留一个终端即保留其进程与一屏有界缓冲。输出不进模型转录。

依赖的执行 provider（上游 `ctx.subprocess`）：mini 的 PTY seam 落在 `terminal_bash.provider`
（P2 载体），本模块默认消费其 `spawn_terminal`，可经构造参数注入 Fake。
沙箱策略读 `agent.ctx.get('sandboxPolicy')`（对齐上游 `execution(agent)` 的
`agent.ctx.get('sandboxPolicy')`）。

载体差异（已在 verified-diffs / design-terminal-domain.md 登记）：
  * 同步单线程模型：create/spawn 无 `pending` 跨调度帧，close-during-allocation 窗口
    在同步链上不可重入；`allocations` 仍保留以承接「失败分配需显式 close」语义。
  * 沙箱模式 fence：上游经 `internal/dispatch` 拦截 `sandbox/mode` 事件；mini 无该
    dispatch 面，改由 `sandboxPolicy.add_mode_fence` 在唯一写路径前拒绝（功能等价）。
"""
from __future__ import annotations

from typing import Any

from ..core.scope import Context, Service
from ..terminal.types import Cancellation, _utf8_bytes
from .shells import discover_shells, resolve_shell
from .terminal import BrowserTerminal
from .types import (
    TerminalLimitReached,
    is_identity,
    resolve_config,
)

__all__ = ["TerminalController", "install_terminal_controller"]

#: 上游 index.ts:332 spawnTerminal 的终端类型。
TERMINAL_TYPE = "xterm-256color"


def _default_spawn_terminal(spec: dict) -> Any:
    from ..terminal_bash.provider import spawn_terminal
    return spawn_terminal(spec)


class _OwnedSession:
    """一个会话身份拥有的终端与已分配资源（index.ts:56-63）。"""

    def __init__(self, agent: Any):
        self.agent = agent
        self.lifetime = Cancellation()
        self.closed_ids: set[str] = set()
        self.terminals: dict[str, BrowserTerminal] = {}
        self.allocations: dict[str, dict] = {}
        self.cleaned = False

    def reserved(self) -> int:
        return len(self.terminals) + len(self.allocations)


class TerminalController(Service):
    """临时、会话拥有的终端进程的类型化远程控制（index.ts:66-357）。"""

    provide = "terminalController"

    def __init__(self, ctx: Context, config: dict | None = None,
                 spawn_terminal=None, resolve_shell_fn=None, discover_shells_fn=None):
        self.config = resolve_config(config)
        self._spawn_terminal = spawn_terminal or _default_spawn_terminal
        self._resolve_shell = resolve_shell_fn or resolve_shell
        self._discover_shells = discover_shells_fn or discover_shells
        self._owners: dict[str, _OwnedSession] = {}
        self._lifetime = Cancellation()
        super().__init__(ctx, "terminalController")
        self._mode_fence_dispose = self._install_mode_fence()
        ctx.effect(lambda: lambda: self._dispose_all(), "terminal-controller.processes")

    # ---------- 装配 ----------

    def _install_mode_fence(self):
        policy = self.ctx.get("sandboxPolicy")
        if policy is None or not hasattr(policy, "add_mode_fence"):
            return None
        return policy.add_mode_fence(self._fence_mode_change)

    def _fence_mode_change(self, session: Any, mode: str) -> None:
        owner = self._owners.get(session.session_id)
        if owner is None or owner.reserved() == 0:
            return
        policy = self.ctx.get("sandboxPolicy")
        current = policy.override_of(session) if policy is not None else None
        if current is None and policy is not None:
            current = policy.default_mode
        if mode != current:
            raise RuntimeError(
                "Close browser terminals before changing the Session sandbox mode")

    # ---------- 远程方法 ----------

    def environment(self, agent: Any, signal=None) -> dict:
        """读会话工作目录与终端限额（不解析 shell）。"""
        self._throw_if_aborted(signal)
        policy = self._policy()
        resolved = policy.resolve({"session": agent.session})
        return {"cwd": resolved["workspaceRoot"],
                "maxInputBytes": self.config["maxInputBytes"],
                "maxCols": self.config["maxCols"],
                "maxRows": self.config["maxRows"],
                "scrollback": self.config["scrollback"]}

    def shells(self, agent: Any, signal=None) -> list[dict]:
        """发现执行环境中已安装的 shell（配置/环境缺省在最前）。"""
        self._throw_if_aborted(signal)
        return self._discover_shells(self.config["shell"],
                                     self.config["shellCandidates"], signal)

    def list(self, session_id: str) -> list[dict]:
        """列出本 Host 生命期内保留的终端（不激活 Agent）。"""
        owner = self._owners.get(session_id)
        if owner is None:
            return []
        return ([t.info for t in owner.terminals.values()]
                + [a["info"] for a in owner.allocations.values()])

    def create(self, agent: Any, request: dict, signal=None) -> dict:
        """为调用方生成的身份分配一个交互式 shell（开身份幂等）。"""
        self._lifetime.throw_if_aborted()
        if not is_identity(request.get("id")):
            raise ValueError("Invalid terminal identity")
        self._dimensions(request.get("cols"), request.get("rows"))
        owner = self._owner(agent)
        owner.lifetime.throw_if_aborted()
        self._require_open(owner, request["id"])
        existing = owner.terminals.get(request["id"])
        if existing is not None:
            return existing.info
        if request["id"] in owner.allocations:
            raise RuntimeError(
                "Close the failed terminal allocation before creating it again")
        if owner.reserved() >= self.config["maxTerminals"]:
            raise TerminalLimitReached(self.config["maxTerminals"])
        terminal = self._spawn(agent, owner, request, signal)
        owner.allocations.pop(request["id"], None)
        owner.terminals[request["id"]] = terminal
        return terminal.info

    def follow(self, agent: Any, id: str, attachment_id: str, signal=None):
        """附加到一个终端而不把其进程生命周期绑定到传输。"""
        if not is_identity(attachment_id):
            raise ValueError("Invalid terminal attachment identity")
        return self._terminal(agent, id).follow(attachment_id, signal)

    def write(self, agent: Any, id: str, attachment_id: str, data: str) -> None:
        """投递原始输入，含 Tab 补全与控制字符。"""
        if _utf8_bytes(data) > self.config["maxInputBytes"]:
            raise RuntimeError("Terminal input exceeds the configured limit")
        self._terminal(agent, id).write(attachment_id, data)

    def resize(self, agent: Any, id: str, attachment_id: str, cols: int, rows: int) -> None:
        """更新 PTY 与恢复屏幕的尺寸。"""
        self._dimensions(cols, rows)
        self._terminal(agent, id).resize(attachment_id, cols, rows)

    def rename(self, agent: Any, id: str, title: str) -> None:
        """重命名终端而不改变其 shell。"""
        trimmed = title.strip()
        if len(trimmed) == 0 or len(title) > 120:
            raise RuntimeError("Terminal title must contain 1–120 characters")
        self._terminal(agent, id).rename(trimmed)

    def close(self, agent: Any, id: str) -> None:
        """对未来的创建封闭该身份并杀掉其进程范围；重复关闭成功。"""
        owner = self._owner(agent)
        owner.closed_ids.add(id)
        terminal = owner.terminals.get(id)
        if terminal is not None:
            terminal.close()
            owner.terminals.pop(id, None)
            return
        allocation = owner.allocations.get(id)
        if allocation is None:
            return
        allocation["handle"].terminate()
        owner.allocations.pop(id, None)

    # ---------- owner / 拆解 ----------

    def _owner(self, agent: Any) -> _OwnedSession:
        owner = self._owners.get(agent.id)
        if owner is None:
            owner = _OwnedSession(agent)
            self._owners[agent.id] = owner
            self._register_owner_effect(agent, owner)
        return owner

    def _register_owner_effect(self, agent: Any, owner: _OwnedSession) -> None:
        agent_ctx = getattr(agent, "ctx", None)
        if agent_ctx is None or not hasattr(agent_ctx, "effect"):
            return
        agent_ctx.effect(lambda: (lambda: self._dispose_owner(agent.id, owner)),
                         "terminal-controller.owner")

    def _dispose_owner(self, session_id: str, owner: _OwnedSession) -> None:
        if owner.cleaned:
            return
        owner.cleaned = True
        owner.lifetime.abort(RuntimeError("Terminal Session owner disposed"))
        failures = []
        for terminal in list(owner.terminals.values()):
            try:
                terminal.close()
            except Exception as error:  # noqa: BLE001 - 逐个收账
                failures.append(error)
        for allocation in list(owner.allocations.values()):
            try:
                allocation["handle"].terminate()
            except Exception as error:  # noqa: BLE001 - 逐个收账
                failures.append(error)
        owner.terminals.clear()
        owner.allocations.clear()
        self._owners.pop(session_id, None)
        if failures:
            raise _aggregate(failures, "Session terminal cleanup failed")

    def _dispose_all(self) -> None:
        self._lifetime.abort(RuntimeError("Terminal controller disposed"))
        if self._mode_fence_dispose is not None:
            self._mode_fence_dispose()
            self._mode_fence_dispose = None
        failures = []
        for session_id, owner in list(self._owners.items()):
            try:
                self._dispose_owner(session_id, owner)
            except Exception as error:  # noqa: BLE001 - 汇总后 fail loud
                failures.append(error)
        if failures:
            raise _aggregate(failures, "Browser terminal cleanup failed")

    # ---------- 内部 ----------

    def _spawn(self, agent: Any, owner: _OwnedSession, request: dict, signal) -> BrowserTerminal:
        environment = self.environment(agent, signal)
        shell_path = request.get("shellPath")
        if shell_path is None:
            shell = self._resolve_shell(self.config["shell"], signal)
        else:
            shell = next((candidate for candidate in self.shells(agent, signal)
                          if candidate["path"] == shell_path), None)
        if shell is None:
            raise RuntimeError("Selected shell is not available in this execution environment")
        policy = self._policy().resolve({"session": agent.session})
        argv = [shell["path"], *shell["args"]]
        if policy["mode"] != "danger-full-access":
            sandbox = self._sandbox(agent)
            if sandbox is None:
                raise RuntimeError(
                    "The Session sandbox mode requires an execution sandbox provider")
            argv = sandbox.confine(argv, {**policy, "mode": policy["mode"]}, signal)["argv"]
        handle = self._spawn_terminal({
            "argv": argv, "cwd": environment["cwd"], "cols": request["cols"],
            "rows": request["rows"], "terminalType": TERMINAL_TYPE,
            "env": {"DSH_SESSION_ID": agent.id},
            "graceMs": self.config["disposeGraceMs"], "signal": signal,
        })
        info = {"id": request["id"], "title": shell["name"], "shell": shell,
                "cwd": environment["cwd"], "cols": request["cols"], "rows": request["rows"],
                "state": "running", "exitCode": None}
        owner.allocations[request["id"]] = {"handle": handle, "info": info}
        try:
            self._throw_if_aborted(signal)
            return BrowserTerminal(handle, info, self.config["scrollback"],
                                   self.config["maxBufferedBytes"])
        except Exception as error:
            owner.allocations[request["id"]]["info"] = {
                **info, "state": "failed", "error": str(error)}
            try:
                handle.terminate()
            except Exception as cleanup_error:
                raise _aggregate([error, cleanup_error], "Terminal allocation cleanup failed")
            owner.allocations.pop(request["id"], None)
            raise

    def _terminal(self, agent: Any, id: str) -> BrowserTerminal:
        owner = self._owners.get(agent.id)
        terminal = owner.terminals.get(id) if owner is not None else None
        if terminal is None:
            raise RuntimeError("Terminal no longer exists in this Session")
        return terminal

    def _require_open(self, owner: _OwnedSession, id: str) -> None:
        if id in owner.closed_ids:
            raise RuntimeError("Terminal was closed in this Session")

    def _dimensions(self, cols: Any, rows: Any) -> None:
        if (not _is_safe_int(cols) or cols < 2 or cols > self.config["maxCols"]
                or not _is_safe_int(rows) or rows < 1 or rows > self.config["maxRows"]):
            raise RuntimeError("Terminal dimensions exceed the configured limits")

    def _policy(self):
        policy = self.ctx.get("sandboxPolicy")
        if policy is None:
            raise RuntimeError(
                "The Session execution environment requires subprocess and sandbox policy providers")
        return policy

    def _sandbox(self, agent: Any):
        agent_ctx = getattr(agent, "ctx", None)
        if agent_ctx is None:
            return None
        return agent_ctx.get("sandbox")

    @staticmethod
    def _throw_if_aborted(signal) -> None:
        if signal is not None:
            signal.throw_if_aborted()


def _is_safe_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _aggregate(failures: list, message: str) -> Exception:
    errors = list(failures)
    if len(errors) == 1:
        return errors[0]
    try:
        return ExceptionGroup(message, errors)
    except (NameError, TypeError):  # pragma: no cover - Python < 3.11
        return RuntimeError(f"{message}: " + "; ".join(str(e) for e in errors))


def install_terminal_controller(ctx: Context, config: dict | None = None,
                                **inject) -> TerminalController:
    """幂等装配：创建 ctx.terminalController 服务（对齐 install_terminals 惯例）。"""
    existing = ctx.get("terminalController")
    if existing is not None:
        return existing
    return TerminalController(ctx, config or {}, **inject)
