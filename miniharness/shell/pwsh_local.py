"""本地 pwsh 执行器（ctx.shell 的 pwsh provider，上游 pwsh-local 对应物）。

对齐 `packages/shell/pwsh-local/src/index.ts`：以
`pwsh -NoLogo -NoProfile -NonInteractive -Command <ENCODING_PREAMBLE><cmd>`
运行（整条命令作为 -Command 后的单个 argv 元素，无中间 shell/引号层）。
UTF-8 编码前导（`[Console]::OutputEncoding` + `$OutputEncoding`）保证
Windows PowerShell 5.1 非 ASCII 输出不乱码；pwsh 7 不受影响。

与 LocalBashExecutor 同构：`execute(spec)` 返回 `ShellExecution` 句柄，
resolve 补齐 workdir/timeoutMs/onExpiry/stdoutMaxBytes。argv 差异在
`pwsh_argv`（可测的静态方法）；spawn/读取/监控/终止复用 bash 的
Popen + 读线程承载。

载体说明（登录 verified-diffs）：同 bash——无 ctx.subprocess 托管范围与
spill，进程经 subprocess.Popen + 读/监视线程承载，SIGTERM→SIGKILL 宽限。
"""
from __future__ import annotations

import os

from ..core.scope import Context, Service
from .bash_local import ENV_OVERRIDES, LocalBashExecutor

__all__ = [
    "ENCODING_PREAMBLE",
    "PwshLocalExecutor",
    "candidate_pwsh_paths",
    "resolve_pwsh_path",
]

#: 逐字对齐 pwsh-local ENCODING_PREAMBLE（index.ts:48）——UTF-8 输出前导。
ENCODING_PREAMBLE = (
    "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
    "$OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
)

#: pwsh 探活命令（`$true` 返回 status 0）。
_PROBE = "$true"


def candidate_pwsh_paths(env: dict | None = None) -> list[str]:
    """候选 pwsh 路径（resolve.ts:5-33）：ProgramFiles PowerShell 7 pwsh.exe
    + PATH 逐项 pwsh.exe（去引号）+ Windows PowerShell 5.1 兜底。"""
    env = env if env is not None else os.environ
    candidates: list[str] = []
    program_files = env.get("ProgramFiles") or r"C:\Program Files"
    candidates.append(os.path.join(program_files, "PowerShell", "7", "pwsh.exe"))
    path = env.get("PATH") or ""
    for entry in path.split(";"):
        entry = entry.strip().strip('"')
        if entry:
            candidates.append(os.path.join(entry, "pwsh.exe"))
    system_root = env.get("SystemRoot") or r"C:\Windows"
    candidates.append(os.path.join(
        system_root, "System32", "WindowsPowerShell", "v1.0", "powershell.exe"))
    return candidates


def resolve_pwsh_path(configured: str | None = None, env: dict | None = None,
                      platform: str | None = None) -> str:
    """解析 pwsh 可执行（resolve.ts:35-68）：配置非空 → 原样；win32 → 首个
    lstat 存在的候选；其它平台 → 'pwsh'。"""
    if configured:
        return configured
    platform = platform if platform is not None else os.name
    if platform == "nt":
        import os.path as _osp
        for candidate in candidate_pwsh_paths(env):
            try:
                if _osp.islink(candidate) or _osp.isfile(candidate):
                    return candidate
            except OSError:
                continue
        return candidate_pwsh_paths(env)[0]
    return "pwsh"


class PwshLocalExecutor(LocalBashExecutor):
    """ctx.shell：`pwsh -Command <cmd>` 的本地执行（bash 同构 argv 差异）。"""

    def __init__(self, ctx: Context, config: dict | None = None):
        config = dict(config or {})
        self.pwsh_path: str = config.get("pwshPath") or resolve_pwsh_path()
        super().__init__(ctx, config)
        self.program = [self.pwsh_path, "-NoLogo", "-NoProfile", "-NonInteractive"]

    def pwsh_argv(self, command: str) -> list[str]:
        """精确 argv（pwsh-local index.ts:193-195）：编码前导 + 命令单元素。"""
        return [*self.program, "-Command", f"{ENCODING_PREAMBLE}{command}"]

    def execute(self, spec: dict) -> object:
        """spawn `pwsh -Command <cmd>` 并返回执行句柄（未结算的 spec 先 resolve）。"""
        if "onExpiry" not in spec or "timeoutMs" not in spec:
            spec = self.resolve(spec)
        return self.execute_argv(spec, self.pwsh_argv(spec["command"]))