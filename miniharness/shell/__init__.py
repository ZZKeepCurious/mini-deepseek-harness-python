"""shell 能力（ctx.shell）：本地 bash/pwsh 执行 + 沙箱消费执行器。

对应 dsh 真实源码：packages/shell/{shell, bash-local, bash-sandbox, pwsh-local,
pwsh-sandbox}。执行器经 `execute(spec) -> ShellExecution` 交出句柄：前台调用读
`result()`，后台调用保留句柄（`observed` 非消耗读 + `readOutput()` 消费读 +
`kill()`）。

装配（上游由 bundle 补丁层选择 provider；mini 经 install_bash_executor /
install_pwsh_executor 显式安装，sandboxed 缺省自动探测）：

    install_bash_executor(ctx)                    # 本地直跑 bash
    install_pwsh_executor(ctx)                    # 本地直跑 pwsh（win32 缺省）
    install_sandbox_stack(ctx, {"mode": ...})     # sandbox + sandboxPolicy
                                                  # + 受限执行器

工具层（tool_bash / tool_pwsh）经 ctx.get("shell") 收编真实 shell 工具；无
shell 服务时保持教学 stub，行为不变。
"""

from __future__ import annotations

from ..core.scope import Context
from .bash_local import LocalBashExecutor
from .bash_sandbox import SandboxBashExecutor
from .env import (
    RESERVED_BASH_ENV_KEYS,
    ShellEnvRegistry,
    install_shell_env,
)
from .pwsh_local import (
    ENCODING_PREAMBLE,
    PwshLocalExecutor,
    candidate_pwsh_paths,
    resolve_pwsh_path,
)
from .pwsh_sandbox import SandboxPwshExecutor
from .types import (
    SHELL_EXPIRY_POLICIES,
    ShellExecution,
    SubprocessOutputReader,
    is_aborted,
    settled_execution,
)

__all__ = [
    "ENCODING_PREAMBLE",
    "LocalBashExecutor",
    "PwshLocalExecutor",
    "RESERVED_BASH_ENV_KEYS",
    "SandboxBashExecutor",
    "SandboxPwshExecutor",
    "SHELL_EXPIRY_POLICIES",
    "ShellEnvRegistry",
    "ShellExecution",
    "SubprocessOutputReader",
    "candidate_pwsh_paths",
    "install_bash_executor",
    "install_pwsh_executor",
    "install_shell_env",
    "is_aborted",
    "resolve_pwsh_path",
    "settled_execution",
]


def install_bash_executor(ctx: Context, config: dict | None = None,
                          sandboxed: bool | None = None) -> LocalBashExecutor:
    """提供 ctx.shell（bash；幂等）。sandboxed=None 时按服务可用性自动选择：
    ctx.sandbox + ctx.sandboxPolicy 齐备 → 受限执行器，否则本地直跑。"""
    existing = ctx.get("shell")
    if existing is not None:
        return existing
    if sandboxed is None:
        sandboxed = (ctx.get("sandbox") is not None
                     and ctx.get("sandboxPolicy") is not None)
    cls = SandboxBashExecutor if sandboxed else LocalBashExecutor
    return cls(ctx, config)


def install_pwsh_executor(ctx: Context, config: dict | None = None,
                          sandboxed: bool | None = None) -> PwshLocalExecutor:
    """提供 ctx.shell（pwsh；幂等）。sandboxed=None 时按服务可用性自动选择。"""
    existing = ctx.get("shell")
    if existing is not None:
        return existing
    if sandboxed is None:
        sandboxed = (ctx.get("sandbox") is not None
                     and ctx.get("sandboxPolicy") is not None)
    cls = SandboxPwshExecutor if sandboxed else PwshLocalExecutor
    return cls(ctx, config)
