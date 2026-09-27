"""沙箱消费 pwsh 执行器（ctx.shell 的 pwsh 受限 provider，上游 pwsh-sandbox 对应物）。

对齐 `packages/shell/pwsh-sandbox/src/index.ts`：在 PwshLocalExecutor 之上把
精确 pwsh argv 经 `ctx.sandbox.confine` 包裹后 spawn，报告所选 mode、
enforcement 与 denial 事实；danger-full-access 直通。三路归因与 bash
helpers 同款（runner 失败优先于 denial 并抛 SandboxUnavailableError）。
"""
from __future__ import annotations

from ..core.scope import Context
from ..seams.sandbox_local import LocalSandboxProvider, SandboxUnavailableError
from .helpers import (
    classify_denial,
    classify_runner_failure,
    is_runner_spawn_failure,
)
from .pwsh_local import PwshLocalExecutor
from .types import ShellExecution

__all__ = ["SandboxPwshExecutor"]


class SandboxPwshExecutor(PwshLocalExecutor):
    """以受限形态注册 ctx.shell（pwsh 方言）。"""

    def __init__(self, ctx: Context, config: dict | None = None,
                 sandbox: LocalSandboxProvider | None = None):
        super().__init__(ctx, config)
        self._sandbox = sandbox or ctx.get("sandbox")
        self._policy_service = ctx.get("sandboxPolicy")
        if self._sandbox is None or self._policy_service is None:
            raise ValueError(
                "SandboxPwshExecutor requires 'sandbox' and 'sandboxPolicy' services")
        self.mode: str = self._policy_service.default_mode

    @property
    def sandbox_mode(self) -> str:
        """配置缺省模式——工具层读的能力事实。"""
        return self.mode

    def resolve(self, request: dict) -> dict:
        spec = super().resolve(request)
        if spec.get("sandboxPolicy") is None:
            spec["sandboxPolicy"] = self._policy_service.resolve()
        return spec

    def execute(self, spec: dict) -> ShellExecution:
        spec = self.resolve(spec)
        policy = spec["sandboxPolicy"]
        mode = policy["mode"]
        if mode == "danger-full-access":
            return self._decorate(
                super().execute(spec),
                lambda result: {**result, "sandbox": {"mode": mode, "denied": False}})
        confined = self._sandbox.confine(self.pwsh_argv(spec["command"]),
                                         {**policy, "mode": mode})
        execution = self.execute_argv(
            spec, confined["argv"],
            on_settled=lambda e: self._stamp(e, confined, mode, spec["workdir"]))
        return self._decorate(
            execution,
            lambda result: self._sandbox_result(result, confined, mode),
            lambda error: self._classify_spawn_error(error, confined, mode,
                                                     spec["workdir"]))

    def _sandbox_result(self, result: dict, confined: dict, mode: str) -> dict:
        """给结算投影盖沙箱事实；runner 失败优先于 denial 并抛基础设施错误。"""
        stderr_text = result["stderr"]["text"]
        runner_failure = classify_runner_failure(
            result.get("exitCode"), stderr_text, confined["runnerFailureRules"])
        if runner_failure is not None:
            raise SandboxUnavailableError(mode, runner_failure["detail"])
        return {
            **result,
            "sandbox": {
                "mode": mode,
                "denied": classify_denial(
                    {"exitCode": result.get("exitCode"), "stderr": stderr_text},
                    confined["denialSignatures"]),
                "enforcement": confined["enforcement"],
            },
        }

    def _stamp(self, execution: ShellExecution, confined: dict, mode: str,
               workdir: str) -> None:
        """进程结算即把沙箱事实挂上句柄（后台读路径也看得到 runnerFailed）。"""
        stderr_text = execution.observed["stderr"].read_from(0)["text"]
        if execution._spawn_error is not None:
            runner_failed = is_runner_spawn_failure(
                execution._spawn_error, confined["argv"][0], workdir)
        else:
            runner_failed = classify_runner_failure(
                execution.exitCode, stderr_text, confined["runnerFailureRules"]) is not None
        sandbox = {
            "mode": mode,
            "denied": (not runner_failed) and classify_denial(
                {"exitCode": execution.exitCode, "stderr": stderr_text},
                confined["denialSignatures"]),
            "enforcement": confined["enforcement"],
        }
        if runner_failed:
            sandbox["runnerFailed"] = True
        execution.sandbox = sandbox

    @staticmethod
    def _classify_spawn_error(error: BaseException, confined: dict, mode: str,
                              workdir: str):
        """上游中止仍是取消；否则 runner 可执行证据 → SandboxUnavailableError。"""
        if isinstance(error, OSError) and is_runner_spawn_failure(
                error, confined["argv"][0], workdir):
            raise SandboxUnavailableError(mode, str(error)) from error
        raise error

    @staticmethod
    def _decorate(execution: ShellExecution, map_result, map_error=None) -> ShellExecution:
        """就地装饰前台投影（memoized 一次）——句柄身份不变。"""
        base = execution.result
        state = {"value": None}

        def result():
            if state["value"] is None:
                try:
                    raw = base()
                except BaseException as error:  # noqa: BLE001 - 装饰错误映射后重抛
                    if map_error is not None:
                        map_error(error)
                    raise
                state["value"] = map_result(raw)
            return state["value"]

        execution.result = result
        return execution