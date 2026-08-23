"""本地 bash 执行器（ctx.shell 缺省 provider，上游 bash-local 对应物）。

职责：把 shell 源码交给 `bash -c` 并继承本地进程机制；run() 结算为
{exitCode, stdout, stderr, signal?}。沙箱包装是子类（bash_sandbox.py）
的事——本类不知道沙箱存在（上游 LocalBashExecutor 同构）。

spec 形状（上游 ShellExecSpec 的 mini 子集）：{command, workdir?, signal?,
sandboxPolicy?}。resolve(request) 是显式决议步（上游 request/spec 分离
模板）：mini 无 intercept 层叠需求，缺省原样透传，子类在此盖策略戳。
"""

from __future__ import annotations

import subprocess

from ..core.scope import Context, Service

__all__ = ["LocalBashExecutor"]


class LocalBashExecutor(Service):
    """ctx.shell：`bash -c <command>` 的本地执行。"""

    provide = "shell"

    def __init__(self, ctx: Context, config: dict | None = None):
        config = dict(config or {})
        self.program: list[str] = list(config.get("program") or ["bash", "-c"])
        super().__init__(ctx, "shell")

    def resolve(self, request: dict) -> dict:
        """请求 → 规格：显式决议步（缺省透传；沙箱子类盖策略戳）。"""
        return dict(request)

    def run(self, spec: dict) -> dict:
        return self.spawn_argv(spec, [*self.program, spec["command"]])

    def spawn_argv(self, spec: dict, argv: list[str]) -> dict:
        """spawn 精确 argv 并结算结果（沙箱子类传包裹后的 argv）。"""
        try:
            proc = subprocess.run(
                argv,
                cwd=spec.get("workdir") or None,
                input=spec.get("stdin") or "",
                capture_output=True,
                text=True,
                timeout=spec.get("timeoutMs", None) / 1000 if spec.get("timeoutMs") else None,
            )
        except subprocess.TimeoutExpired as exc:
            result = {
                "exitCode": None,
                "stdout": exc.stdout if isinstance(exc.stdout, str) else (exc.stdout or b"").decode(errors="replace"),
                "stderr": exc.stderr if isinstance(exc.stderr, str) else (exc.stderr or b"").decode(errors="replace"),
            }
            return {**result, "signalled": True}
        return {
            "exitCode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
