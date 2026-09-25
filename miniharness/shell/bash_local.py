"""本地 bash 执行器（ctx.shell 缺省 provider，上游 bash-local 对应物）。

职责：把 shell 源码交给 `bash -c` 并继承本地进程机制；`execute(spec)` 返回
`ShellExecution` 句柄，前台调用读 `result()`，后台调用保留句柄。沙箱包装是
子类（bash_sandbox.py）的事——本类不知道沙箱存在（上游 LocalBashExecutor 同构）。

spec 形状（上游 ShellExecSpec 的 mini 子集）：{command, workdir, timeoutMs,
onExpiry, stdoutMaxBytes, signal?, stdin?, env?, dshEnv?, sandboxPolicy?}。
resolve(request) 是显式决议步（上游 request/spec 分离模板）：补齐 workdir /
timeoutMs / onExpiry / stdoutMaxBytes，子类在此盖策略戳。

载体说明（登录 verified-diffs）：上游经 `ctx.subprocess` 托管范围并落盘 spill；
mini 以 `subprocess.Popen` + stdout/stderr 读线程 + 监视线程承载进程句柄，
内存增长缓冲（UTF-8 安全裁头）替代 spill 收集器，SIGTERM→SIGKILL 宽限期近似。
"""
from __future__ import annotations

import os
import signal as _signal
import subprocess
import threading
import time

from ..core.scope import Context, Service
from ..seams.subprocess_env import scrubbed_parent_env
from .types import (
    OutputBuffer,
    ShellExecution,
    SubprocessOutputReader,
    is_aborted,
)

__all__ = ["ENV_OVERRIDES", "LocalBashExecutor"]

#: 模型友好环境覆盖（对齐 bash-local ENV_OVERRIDES）：禁颜色/分页器/交互特性。
ENV_OVERRIDES = {
    "NO_COLOR": "1",
    "TERM": "dumb",
    "PAGER": "cat",
    "GIT_PAGER": "cat",
}

#: 配置缺省（对齐 bash-local Config 表）。
DEFAULT_TIMEOUT_MS = 120_000
DEFAULT_MAX_TIMEOUT_MS = 600_000
DEFAULT_MAX_OUTPUT_BYTES = 64_000
#: SIGTERM→SIGKILL 宽限期（上游 graceMs 缺省 3s）。
DEFAULT_GRACE_MS = 3_000

#: 读线程分块大小。
_READ_CHUNK = 65_536


def _positive_finite(name: str, value) -> None:
    if not isinstance(value, (int, float)) or isinstance(value, bool) \
            or value <= 0 or value != value or value in (float("inf"), float("-inf")):
        raise ValueError(f"bash-local: {name} must be a positive finite number")


def _signal_name(code: int | None) -> str | None:
    """Python 负返回码 → 信号名（无信号返回 None）。"""
    if code is None or code >= 0:
        return None
    try:
        return _signal.Signals(-code).name
    except ValueError:
        return f"signal {(-code)}"


def _terminate(proc: subprocess.Popen, grace_ms: int) -> None:
    """SIGTERM，宽限期后仍在则 SIGKILL（上游 graceMs 升级近似）。"""
    try:
        proc.terminate()
    except (ProcessLookupError, OSError):
        return

    def escalate() -> None:
        try:
            if proc.poll() is None:
                proc.kill()
        except (ProcessLookupError, OSError):
            pass

    timer = threading.Timer(grace_ms / 1000, escalate)
    timer.daemon = True
    timer.start()


class LocalBashExecutor(Service):
    """ctx.shell：`bash -c <command>` 的本地执行，返回 `ShellExecution` 句柄。"""

    provide = "shell"

    def __init__(self, ctx: Context, config: dict | None = None):
        config = dict(config or {})
        self.program: list[str] = list(config.get("program") or ["bash", "-c"])
        self.cwd: str | None = config.get("cwd")
        self.timeout_ms: int = config.get("timeoutMs", DEFAULT_TIMEOUT_MS)
        self.max_timeout_ms: int = config.get("maxTimeoutMs", DEFAULT_MAX_TIMEOUT_MS)
        self.max_output_bytes: int = config.get("maxOutputBytes", DEFAULT_MAX_OUTPUT_BYTES)
        self.grace_ms: int = config.get("graceMs", DEFAULT_GRACE_MS)
        super().__init__(ctx, "shell")

    def _assert_serviceable(self) -> None:
        _positive_finite("timeoutMs", self.timeout_ms)
        _positive_finite("maxTimeoutMs", self.max_timeout_ms)
        _positive_finite("maxOutputBytes", self.max_output_bytes)
        _positive_finite("graceMs", self.grace_ms)

    def resolve(self, request: dict) -> dict:
        """请求 → 规格：补齐缺省并封顶 timeoutMs；子类在此盖策略戳。"""
        self._assert_serviceable()
        timeout = request.get("timeoutMs")
        if timeout is None:
            timeout = self.timeout_ms
        _positive_finite("request.timeoutMs", timeout)
        timeout = min(timeout, self.max_timeout_ms)
        stdout_max = request.get("stdoutMaxBytes", self.max_output_bytes)
        _positive_finite("request.stdoutMaxBytes", stdout_max)
        spec = dict(request)
        spec["command"] = request["command"]
        spec["workdir"] = request.get("workdir") or self.cwd or os.getcwd()
        spec["timeoutMs"] = timeout
        spec["onExpiry"] = request.get("onExpiry") or "kill"
        spec["stdoutMaxBytes"] = stdout_max
        return spec

    def execute(self, spec: dict) -> ShellExecution:
        """spawn `bash -c <command>` 并返回执行句柄（未结算的 spec 先 resolve）。"""
        if "onExpiry" not in spec or "timeoutMs" not in spec:
            spec = self.resolve(spec)
        return self.execute_argv(spec, [*self.program, spec["command"]])

    def execute_argv(self, spec: dict, argv: list[str],
                     on_settled=None) -> ShellExecution:
        """spawn 精确 argv 并返回句柄（沙箱子类传包裹后的 argv）。"""
        return self.spawn_argv(spec, argv, on_settled)

    def spawn_argv(self, spec: dict, argv: list[str],
                   on_settled=None) -> ShellExecution:
        """spawn 精确 argv 并织好读取/监视线程，返回执行句柄。"""
        timeout_ms = spec["timeoutMs"]
        stdout_buf = OutputBuffer(spec.get("stdoutMaxBytes"))
        stderr_buf = OutputBuffer(self.max_output_bytes)
        execution = ShellExecution(SubprocessOutputReader(stdout_buf),
                                   SubprocessOutputReader(stderr_buf))
        job_signal = spec.get("signal")
        cause = {"timedOut": False, "aborted": False}

        def build_result() -> dict:
            return {
                "exitCode": execution.exitCode,
                "signal": execution.signal,
                "timedOut": cause["timedOut"],
                "aborted": cause["aborted"],
                "timeoutMs": timeout_ms,
                "stdout": stdout_buf.final(),
                "stderr": stderr_buf.final(),
            }

        execution._result_builder = build_result

        def finish(code: int | None) -> None:
            execution.exitCode = code
            execution.signal = _signal_name(code)
            if cause["timedOut"] or cause["aborted"] or (code is not None and code < 0):
                execution.status = "killed"
            elif execution.status == "running":
                execution.status = "completed"
            execution._result_builder = build_result
            execution.mark_settled()
            if on_settled is not None:
                on_settled(execution)

        # 已 aborted 的 signal 视为已触发（types.ts:76：不 spawn，直接结算 killed）。
        if is_aborted(job_signal):
            cause["aborted"] = True
            execution.status = "killed"
            finish(None)
            return execution

        try:
            proc = subprocess.Popen(
                argv,
                cwd=spec.get("workdir") or None,
                stdin=subprocess.PIPE if spec.get("stdin") is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=self._spawn_env(spec),
            )
        except OSError as error:
            note = f"subprocess failed before reporting an outcome: {error}"
            stderr_buf.append(note.encode("utf-8"))
            execution.status = "killed"
            execution._spawn_error = error
            finish(None)
            return execution

        execution._kill_fn = lambda: _terminate(proc, self.grace_ms)

        def pump(stream, buffer: OutputBuffer) -> None:
            try:
                while True:
                    chunk = stream.read(_READ_CHUNK)
                    if not chunk:
                        break
                    buffer.append(chunk)
            finally:
                try:
                    stream.close()
                except OSError:
                    pass

        readers = [
            threading.Thread(target=pump, args=(proc.stdout, stdout_buf), daemon=True),
            threading.Thread(target=pump, args=(proc.stderr, stderr_buf), daemon=True),
        ]
        for reader in readers:
            reader.start()
        if spec.get("stdin") is not None:
            threading.Thread(target=self._feed_stdin, args=(proc, spec["stdin"]),
                             daemon=True).start()

        def monitor() -> None:
            deadline = (time.monotonic() + timeout_ms / 1000) if spec["onExpiry"] == "kill" else None
            while True:
                try:
                    code = proc.wait(timeout=0.02)
                    break
                except subprocess.TimeoutExpired:
                    if is_aborted(job_signal):
                        cause["aborted"] = True
                        _terminate(proc, self.grace_ms)
                    elif deadline is not None and time.monotonic() >= deadline:
                        cause["timedOut"] = True
                        _terminate(proc, self.grace_ms)
            for reader in readers:
                reader.join(timeout=5)
            finish(code)

        threading.Thread(target=monitor, daemon=True).start()
        return execution

    @staticmethod
    def _feed_stdin(proc: subprocess.Popen, data: str) -> None:
        try:
            proc.stdin.write(data.encode("utf-8"))
            proc.stdin.close()
        except (BrokenPipeError, ValueError, OSError):
            pass

    @staticmethod
    def _spawn_env(spec: dict) -> dict:
        """清洗父环境 + 模型友好覆盖 + spec.env + spec.dshEnv（后者最后合并）。"""
        env = scrubbed_parent_env()
        env.update(ENV_OVERRIDES)
        if spec.get("env"):
            env.update({str(k): str(v) for k, v in spec["env"].items()})
        if spec.get("dshEnv"):
            env.update({str(k): str(v) for k, v in spec["dshEnv"].items()})
        return env
