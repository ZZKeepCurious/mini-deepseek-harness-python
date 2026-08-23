"""shell 能力（ctx.shell）：本地 bash 执行 + 沙箱消费执行器。

对应 dsh 真实源码：packages/shell/{shell, bash-local, bash-sandbox}。
mini 子集：前台 `bash -c` 执行（ShellExecSpec 的 {command, workdir?,
signal?, sandboxPolicy?}）；后台进程机制未复现（mini 的后台面是 jobs
registry，见 AGENTS.md 差异清单 §3.5）。

装配（上游由 bundle 补丁层选择 provider；mini 经 install_bash_executor
显式安装，sandboxed 缺省自动探测）：

    install_bash_executor(ctx)                    # 本地直跑
    install_sandbox_stack(ctx, {"mode": ...})     # sandbox + sandboxPolicy
                                                  # + 受限 bash 执行器

工具层（cli/default_tools.py）经 ctx.get("shell") 收编真实 bash 工具；
无 shell 服务时保持教学 stub，行为不变。
"""

from __future__ import annotations

from ..core.scope import Context
from .bash_local import LocalBashExecutor
from .bash_sandbox import SandboxBashExecutor

__all__ = [
    "LocalBashExecutor",
    "SandboxBashExecutor",
    "install_bash_executor",
]


def install_bash_executor(ctx: Context, config: dict | None = None,
                          sandboxed: bool | None = None) -> LocalBashExecutor:
    """提供 ctx.shell。sandboxed=None 时按服务可用性自动选择：
    ctx.sandbox + ctx.sandboxPolicy 齐备 → 受限执行器，否则本地直跑。"""
    if sandboxed is None:
        sandboxed = (ctx.get("sandbox") is not None
                     and ctx.get("sandboxPolicy") is not None)
    cls = SandboxBashExecutor if sandboxed else LocalBashExecutor
    return cls(ctx, config)
