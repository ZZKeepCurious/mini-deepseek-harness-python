"""沙箱消费 bash 执行器（ctx.shell 的受限 provider，上游 bash-sandbox 对应物）。

把精确的本地 bash argv 经 `ctx.sandbox`（seams/sandbox_local.py）包裹后
spawn，继承本地进程机制，并报告所选 mode、enforcement 与 denial 事实。
正面 runner 失败证据意味着命令从未运行：前台调用抛 `SandboxUnavailableError`
（SANDBOX_UNAVAILABLE）。审批归工具层所有；每次调用携带完整的已决议策略。

三路归因（helpers.py）：runner 启动失败 / denial / 命令自身失败——runner
失败优先于 denial（诊断里可能含 denial 词汇但命令根本没跑）。
danger-full-access 直通：不调 confine，结果附 `sandbox: {mode, denied: false}`。
"""

from __future__ import annotations

from ..core.scope import Context
from ..seams.sandbox_local import LocalSandboxProvider, SandboxUnavailableError
from .bash_local import LocalBashExecutor
from .helpers import classify_denial, classify_runner_failure, is_runner_spawn_failure

__all__ = ["SandboxBashExecutor"]


class SandboxBashExecutor(LocalBashExecutor):
    """以受限形态注册 ctx.shell；要求 ctx.sandbox provider + ctx.sandboxPolicy。

    工具调用传调用方会话的已决议策略（spec.sandboxPolicy）；直接调用回退
    部署策略。result.sandbox 报告实际使用的 mode 与 enforcement。
    """

    def __init__(self, ctx: Context, config: dict | None = None,
                 sandbox: LocalSandboxProvider | None = None):
        super().__init__(ctx, config)
        self._sandbox = sandbox or ctx.get("sandbox")
        self._policy_service = ctx.get("sandboxPolicy")
        if self._sandbox is None or self._policy_service is None:
            # 上游经 inject 声明依赖、装载期保证；mini 构造期 fail loud 等价
            raise ValueError(
                "SandboxBashExecutor requires 'sandbox' and 'sandboxPolicy' services")
        # 缺省模式是 schema 广告用的能力事实；实际执行携带逐调用决议策略
        self.mode: str = self._policy_service.default_mode

    @property
    def sandbox_mode(self) -> str:
        """配置缺省模式——工具层读的能力事实。"""
        return self.mode

    def resolve(self, request: dict) -> dict:
        """给规格盖完整逐调用策略戳：显式策略 > 部署决议。"""
        spec = super().resolve(request)
        if spec.get("sandboxPolicy") is None:
            spec["sandboxPolicy"] = self._policy_service.resolve()
        return spec

    def run(self, spec: dict) -> dict:
        spec = self.resolve(spec)
        policy = spec["sandboxPolicy"]
        mode = policy["mode"]
        if mode == "danger-full-access":
            result = super().run(spec)
            return {**result, "sandbox": {"mode": mode, "denied": False}}
        confined = self.confine(spec["command"], {**policy, "mode": mode})
        try:
            result = self.spawn_argv(spec, confined["argv"])
        except OSError as error:
            # 阻止了 spawn 的上游中止仍是取消（上游 signal.throwIfAborted 同位；
            # mini 前台路径无 AbortSignal 语义，OSError 直接归因）
            if is_runner_spawn_failure(error, confined["argv"][0], spec.get("workdir") or "."):
                raise SandboxUnavailableError(mode, str(error)) from error
            raise
        runner_failure = classify_runner_failure(
            result.get("exitCode"), result.get("stderr", ""),
            confined["runnerFailureRules"])
        if runner_failure is not None:
            raise SandboxUnavailableError(mode, runner_failure["detail"])
        return {
            **result,
            "sandbox": {
                "mode": mode,
                "denied": classify_denial(result, confined["denialSignatures"]),
                "enforcement": confined["enforcement"],
            },
        }

    def confine(self, command: str, policy: dict) -> dict:
        """经 ctx.sandbox 包裹一条 shell 命令（内层 `bash -c`）。

        provider 错误原样传播；返回的 argv 直接交给本地执行器的 spawn 路径。
        """
        return self._sandbox.confine(["bash", "-c", command], policy)
