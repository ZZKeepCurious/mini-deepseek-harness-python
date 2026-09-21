"""terminal-bash 后端（对齐 upstream terminal-bash/src/index.ts，同步载体）。

BashTerminalBackend.spawn：policy resolve → spawnArgv（danger-full-access 免包，
否则走 ctx.sandbox.confine）→ spawnTerminal → createSession → startupReadiness，
任一步失败经 rejectAfterStartupCleanup 撤未发布资源（TerminalBackendCleanupError
聚合双重失败）。`install_terminal_bash` 幂等注册 backend 类型。

载体差异（已登记 verified-diffs §3.30）：
* 沙箱模式 fence（ensureSandboxModeFence + internal/dispatch 钩）延至 P4 接线
  ——当前 mini 无进程内 sandbox/mode 变更路径；
* spawn/startup 同步阻塞（上游 async/await 的同步等价形态），abort 以
  Cancellation 判读贯穿。
"""

from __future__ import annotations

from .config import resolve_config, validate_config
from .environment import ENCODING_PREAMBLE, PWSH_PROMPT_SETUP, child_environment
from .provider import spawn_terminal
from .session import LocalPtySession

from ..terminal.service import install_terminals
from ..terminal.types import TerminalBackendCleanupError

__all__ = ["BashTerminalBackend", "apply", "install_terminal_bash"]

DEFAULT_SHELL_DIALECTS = ("bash", "pwsh")

#: 硬依赖服务清单（index.ts:27 inject 的 sync 映射）。
REQUIRED_SERVICES = ("terminals", "sandboxPolicy", "sessionProjections", "subprocess")


def _throw_if_aborted(signal) -> None:
    if signal is not None:
        signal.throw_if_aborted()


def spawn_argv(config: dict, policy: dict, sandbox) -> list:
    """构造待 spawn 参数；danger-full-access 免包，其余经 confine（index.ts:100-109）。"""
    argv = [config["shellPath"], *config["shellArgs"]]
    if policy["mode"] == "danger-full-access":
        return argv
    if sandbox is None:
        raise RuntimeError(
            f'terminal-bash: sandbox mode "{policy["mode"]}" requires a ctx.sandbox provider in the execution world')
    return sandbox.confine(argv, dict(policy, mode=policy["mode"]))["argv"]


def _reject_after_startup_cleanup(error, cleanup) -> None:
    try:
        cleanup()
    except Exception as cleanup_error:
        raise TerminalBackendCleanupError(error, cleanup_error) from None
    raise error


def startup_session(session: LocalPtySession, dialect: str, timeout_ms: int, signal=None) -> None:
    """启动就绪（index.ts:114-171）。

    bash：initialize 直接等首 prompt（PROMPT_COMMAND 注入 marker）；pwsh：反复
    send 安装 prompt 函数 + 锁 UTF-8 输出，直到 stdin_read 证据明确 —— 提示串
    的可打印尾巴不算就绪。整体受一个绝对 deadline 约束（同步载体以会话时
    钟判读）。
    """
    if dialect == "bash":
        session.initialize(signal)
        return
    clock = getattr(session, "_now_ms")
    deadline = clock() + timeout_ms
    viewport = ""
    while True:
        first = len(viewport) == 0
        request = {
            "text": ENCODING_PREAMBLE + PWSH_PROMPT_SETUP if first else "",
            "submit": first,
        }
        if signal is not None:
            request["signal"] = signal
        operation = session.start_send(request)
        if clock() >= deadline:
            session.run_until_settled(operation, signal)
        else:
            remaining = deadline - clock()
            session.run_until_settled_until(operation, signal, remaining)
        result = operation.result
        if result["waitReason"] == "session_exit":
            raise RuntimeError("PTY shell exited during startup")
        if result["waitReason"] == "timeout":
            raise RuntimeError("PTY shell did not reach readiness before startup timeout")
        viewport = result["viewport"]
        if result["waitReason"] == "stdin_read":
            break
    session.motd = viewport


class BashTerminalBackend:
    """本地 shell backend（index.ts:184-231）。"""

    def __init__(self, ctx, config: dict, spawn_terminal_service=None, create_session=None):
        self.type = config["backendType"]
        self.ctx = ctx
        self._config = config
        self._spawn_terminal_service = spawn_terminal_service or spawn_terminal
        self._create_session = create_session or (lambda handle, cfg: LocalPtySession(handle, cfg))

    def spawn(self, spec: dict) -> LocalPtySession:
        _throw_if_aborted(spec.get("signal"))
        policy_service = self.ctx.get("sandboxPolicy")
        if policy_service is None:
            raise RuntimeError("terminal-bash: the sandboxPolicy service is required to spawn a shell backend")
        if callable(policy_service):
            policy = policy_service({"session": spec["owner"].session})
        else:
            policy = policy_service.resolve({"session": spec["owner"].session})
        argv = spawn_argv(self._config, policy, self.ctx.get("sandbox"))
        _throw_if_aborted(spec.get("signal"))
        if not argv or not argv[0]:
            raise RuntimeError("terminal-bash: sandbox returned empty argv")
        handle = self._spawn_terminal_service({
            "argv": argv,
            "cwd": spec.get("cwd") or policy.get("workspaceRoot"),
            "env": child_environment(spec, self._config["shellDialect"]),
            "rows": self._config["rows"],
            "cols": self._config["cols"],
            "terminalType": "dumb",
            "graceMs": self._config["disposeGraceMs"],
        })
        try:
            session = self._create_session(handle, self._config)
        except Exception as error:
            return _reject_after_startup_cleanup(error, handle.terminate)
        try:
            startup_session(session, self._config["shellDialect"], self._config["timeoutMs"], spec.get("signal"))
            return session
        except Exception as error:
            return _reject_after_startup_cleanup(error, lambda: session.close("PTY startup failed"))


def apply(ctx, config: dict) -> None:
    resolved = resolve_config(config)
    validate_config(resolved)
    ctx.get("terminals").register_backend(BashTerminalBackend(ctx, resolved))


def install_terminal_bash(ctx, config: dict | None = None, backend: BashTerminalBackend | None = None):
    """幂等装配：解析/校验配置并注册 bash/pwsh PTY backend（index.ts:234-238）。

    已注册同类型 backend（重复 install）→ 返回现有 terminals 服务，不重复注册。
    """
    resolved = resolve_config(dict(config or {}))
    validate_config(resolved)
    terminals = install_terminals(ctx)
    if backend is None and resolved["backendType"] in terminals.list_backends():
        return terminals
    created = backend or BashTerminalBackend(ctx, resolved)
    terminals.register_backend(created)
    return created