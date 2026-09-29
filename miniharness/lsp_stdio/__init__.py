"""`ctx.lsp` 的通用 stdio 语言服务器后端（对齐 packages/lsp/lsp-stdio）。

一个插件实例配置一张「provider id → 本地语言服务器」的命令表，为每条登记一个独立
provider。每个 provider 对每个规范工作区目标**惰性单飞**一个服务端进程，经它服务瞬态
open 查询，并在下一次只读查询前替换一个在查询前/中失败的选定传输。provider 经
`ctx.fs` 读源、经 `ctx.subprocess` 解析可执行文件，使本地与远程实现共享同一 host。

载体差异（登记）：
  * 上游经 `ctx.subprocess.spawn`（受管子进程）；mini 以 `asyncio.create_subprocess_exec`
    直接 spawn（见 connection.py）。
  * 上游 provider teardown 的 AggregateError；mini 以单失败重抛 / 多失败 RuntimeError 承载。
  * 上游 `AbortSignal.any`；mini 以 `_FusedSignal` 熔合调用方信号与 provider 生命周期信号。
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any, Callable

from ..lsp import LspError
from .abort import abort_error, abortable, signal_aborted
from .host import canonicalize_workspace, read_host_source
from .instance import LspInstance

__all__ = [
    "LspStdioRuntime",
    "LocalLspProvider",
    "install_lsp_stdio",
]

_DEFAULT_MAX_MESSAGE_BYTES = 16_000_000
_DEFAULT_MAX_STDERR_BYTES = 1_000_000
_DEFAULT_MAX_DOCUMENT_BYTES = 4_000_000
_DEFAULT_SHUTDOWN_TIMEOUT_MS = 5_000
_DEFAULT_KILL_GRACE_MS = 2_000

#: 每个 server 配置的缺省填充键（对齐上游 schemastery @default）。
_SERVER_DEFAULTS = {
    "args": [],
    "env": {},
    "initializationOptions": None,
    "configuration": None,
    "maxMessageBytes": _DEFAULT_MAX_MESSAGE_BYTES,
    "maxStderrBytes": _DEFAULT_MAX_STDERR_BYTES,
    "maxDocumentBytes": _DEFAULT_MAX_DOCUMENT_BYTES,
    "shutdownTimeoutMs": _DEFAULT_SHUTDOWN_TIMEOUT_MS,
    "killGraceMs": _DEFAULT_KILL_GRACE_MS,
}

_MAX_TIMER_DELAY_MS = 2_147_483_647


class _FusedSignal:
    """熔合调用方信号与 provider 生命周期信号：任一置位即中止。"""

    def __init__(self, *signals: Any):
        self._signals = [s for s in signals if s is not None]

    def is_set(self) -> bool:
        return any(signal_aborted(s) for s in self._signals)


def _resolve_server_config(provider_id: str, raw: dict) -> dict:
    """校验并填充一个 server 配置（对齐上游 validateServerConfig + schemastery 默认）。"""
    if not isinstance(provider_id, str) or provider_id.strip() == "":
        raise ValueError("lsp-stdio: server ids must be non-empty strings")
    if not isinstance(raw, dict):
        raise ValueError(f"lsp-stdio: servers.{provider_id} must be an object")
    command = raw.get("command")
    if not isinstance(command, str) or command.strip() == "":
        raise ValueError(f"lsp-stdio: servers.{provider_id}.command is required")
    ext = raw.get("extensionToLanguage")
    if not isinstance(ext, dict) or not ext:
        raise ValueError(
            f"lsp-stdio: servers.{provider_id}.extensionToLanguage is required")
    resolved = dict(_SERVER_DEFAULTS)
    resolved.update({k: v for k, v in raw.items() if k != "command"})
    resolved["command"] = command
    resolved["extensionToLanguage"] = ext
    _assert_timer(provider_id, "shutdownTimeoutMs", resolved["shutdownTimeoutMs"])
    _assert_timer(provider_id, "killGraceMs", resolved["killGraceMs"])
    _assert_positive(provider_id, "maxStderrBytes", resolved["maxStderrBytes"])
    _assert_positive(provider_id, "maxMessageBytes", resolved["maxMessageBytes"])
    _assert_positive(provider_id, "maxDocumentBytes", resolved["maxDocumentBytes"])
    return resolved


def _assert_timer(provider_id: str, name: str, value: Any) -> None:
    if (not isinstance(value, int) or isinstance(value, bool)
            or value < 1 or value > _MAX_TIMER_DELAY_MS):
        raise ValueError(
            f"lsp-stdio: servers.{provider_id}.{name} must be a positive integer "
            f"no greater than {_MAX_TIMER_DELAY_MS}")


def _assert_positive(provider_id: str, name: str, value: Any) -> None:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"lsp-stdio: servers.{provider_id}.{name} must be a positive integer")


class LocalLspProvider:
    """池化的通用 provider：每个规范工作区一个服务端进程，按需创建。"""

    def __init__(self, provider_id: str, fs: Any, config: dict, executable: str):
        self.id = provider_id
        self.extension_to_language = config["extensionToLanguage"]
        self._fs = fs
        self._config = config
        self._executable = executable
        self._instances: dict[str, LspInstance] = {}
        self._queues: dict[str, asyncio.Future] = {}
        self._lifetime = threading.Event()
        self._disposed = False

    # ---------- 查询 ----------

    def _is_disposed(self) -> bool:
        return self._disposed

    def _assert_active(self, signal: Any = None) -> None:
        if self._is_disposed():
            raise LspError("lsp-stdio provider is disposed", "LSP_DISPOSED")
        if signal_aborted(signal):
            raise abort_error(signal)

    def _query_signal(self, signal: Any) -> Any:
        if signal is None:
            return self._lifetime
        return _FusedSignal(signal, self._lifetime)

    async def query(self, request: dict, signal: Any = None) -> dict:
        self._assert_active(signal)
        query_signal = self._query_signal(signal)
        workspace = await canonicalize_workspace(
            self._fs, request["workspaceRoot"], query_signal)
        self._assert_active(query_signal)
        key = workspace["target"].target_key
        return await self._enqueue(key, query_signal, workspace, request)

    async def _enqueue(self, key: str, signal: Any, workspace: dict, request: dict) -> dict:
        prior = self._queues.get(key)
        loop = asyncio.get_running_loop()
        done = loop.create_future()
        self._queues[key] = done

        async def finish() -> None:
            if not done.done():
                done.set_result(None)
            if self._queues.get(key) is done:
                self._queues.pop(key, None)

        if prior is not None:
            try:
                await abortable(prior, signal)
            except Exception:  # noqa: BLE001
                await finish()
                raise
        try:
            return await self._run(key, workspace, request, signal)
        finally:
            await finish()

    async def _run(self, key: str, workspace: dict, request: dict, signal: Any) -> dict:
        self._assert_active(signal)
        # 在工作区队列内、spawn 前读源：排队查询在其轮次开始时看到当前字节，
        # 而非法源仍不能留下一个空闲池化进程。
        source = await read_host_source(
            self._fs, request["filePath"], workspace,
            self._config["maxDocumentBytes"], signal)
        self._assert_active(signal)
        instance = await self._instance_for(key, workspace)
        can_retry_transport = True
        while True:
            try:
                result = await instance.query(request, source, signal)
            except Exception as error:  # noqa: BLE001
                if instance.dead:
                    await self._dispose_instance(key, instance)
                # 选定的子进程可能空闲死亡或在下次写时失败；查询是只读的，故干净拆除后
                # 透明地替换该传输一次并重试。
                if (can_retry_transport and instance.is_transport_failure(error)):
                    can_retry_transport = False
                    self._assert_active(signal)
                    instance = await self._instance_for(key, workspace)
                    continue
                raise
            if instance.dead:
                await self._dispose_instance(key, instance)
            return result

    async def _instance_for(self, key: str, workspace: dict) -> LspInstance:
        self._assert_active()
        existing = self._instances.get(key)
        if existing is not None:
            return existing
        instance = LspInstance(self._spec_for(workspace))
        self._instances[key] = instance
        await instance.start()
        return instance

    async def _dispose_instance(self, key: str, instance: LspInstance) -> None:
        if self._instances.get(key) is instance:
            self._instances.pop(key, None)
        await instance.dispose()

    def _spec_for(self, workspace: dict) -> dict:
        cfg = self._config
        return {
            "command": self._executable,
            "args": list(cfg["args"]),
            "cwd": workspace["canonical_path"],
            "workspace_uri": workspace["file_url"],
            "env": dict(cfg["env"] or {}),
            "configuration": cfg["configuration"],
            "initialization_options": cfg["initializationOptions"],
            "max_message_bytes": cfg["maxMessageBytes"],
            "max_stderr_bytes": cfg["maxStderrBytes"],
            "shutdown_timeout_ms": cfg["shutdownTimeoutMs"],
            "kill_grace_ms": cfg["killGraceMs"],
        }

    # ---------- 拆除 ----------

    def terminate_all(self) -> None:
        """同步终止全部活实例并阻断后续查询（effect 拆除路径）。"""
        self._disposed = True
        self._lifetime.set()
        for instance in list(self._instances.values()):
            instance.terminate()

    async def dispose_all(self) -> None:
        """拆除全部活实例并阻断后续查询（异步，等各实例静止）。"""
        self._disposed = True
        self._lifetime.set()
        live = list(self._instances.values())
        draining = list(self._queues.values())
        self._instances.clear()
        tasks = [asyncio.ensure_future(instance.dispose()) for instance in live]
        tasks.extend(asyncio.ensure_future(asyncio.shield(q)) for q in draining)
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self._queues.clear()
        failures = [r for r in results if isinstance(r, BaseException)]
        if len(failures) == 1:
            raise failures[0]
        if len(failures) > 1:
            raise RuntimeError(f"lsp-stdio instance teardown failed: {failures!r}")


class LspStdioRuntime:
    """已装配的 lsp-stdio 后端：持有 providers 供拆除。"""

    def __init__(self, providers: list[LocalLspProvider], disposers: list[Callable]):
        self.providers = providers
        self._disposers = disposers

    def unregister(self) -> None:
        for dispose in reversed(self._disposers):
            dispose()

    def terminate_all(self) -> None:
        for provider in self.providers:
            provider.terminate_all()

    async def dispose(self) -> None:
        self.unregister()
        failures = [p for p in self.providers]
        results = await asyncio.gather(
            *(p.dispose_all() for p in failures), return_exceptions=True)
        errors = [r for r in results if isinstance(r, BaseException)]
        if len(errors) == 1:
            raise errors[0]
        if len(errors) > 1:
            raise RuntimeError(f"lsp-stdio provider teardown failed: {errors!r}")


def install_lsp_stdio(ctx: Any, config: dict) -> LspStdioRuntime:
    """装配 stdio LSP providers（对齐上游 lsp-stdio apply，装配期同步化）。

    解析每个 server 的可执行文件（在注册任何 provider 前）→ 建 providers → 经
    `ctx.lsp.register_provider` 原子登记；effect 拆除时先注销路由再同步终止活进程。
    """
    servers = (config or {}).get("servers") or {}
    if not isinstance(servers, dict) or not servers:
        raise ValueError("lsp-stdio: servers must contain at least one server")
    if ctx.get("lsp") is None:
        from ..lsp import install_lsp

        install_lsp(ctx)
    subprocess = ctx.get("subprocess")
    if subprocess is None:
        raise RuntimeError(
            "lsp-stdio: the ctx.subprocess service is required (mount the "
            "subprocess provider in this composition)")
    fs = ctx.get("fs")
    if fs is None:
        raise RuntimeError(
            "lsp-stdio: the ctx.fs service is required (mount a filesystem provider)")

    providers: list[LocalLspProvider] = []
    for provider_id, raw in servers.items():
        resolved = _resolve_server_config(provider_id, raw)
        env = {**subprocess.scrubbed_parent_env(), **(resolved["env"] or {})}
        executable = subprocess.resolve_executable(resolved["command"], env)
        providers.append(LocalLspProvider(provider_id, fs, resolved, executable))

    disposers: list[Callable] = []
    try:
        for provider in providers:
            disposers.append(ctx.get("lsp").register_provider(provider))
    except Exception:
        for dispose in reversed(disposers):
            dispose()
        raise
    runtime = LspStdioRuntime(providers, disposers)

    def setup() -> Callable[[], None]:
        def dispose() -> None:
            # 先注销全部路由（无新查询进入排空中的 provider），再同步终止活进程。
            runtime.unregister()
            runtime.terminate_all()
        return dispose

    ctx.effect(setup, "lsp-stdio.registerProviders")
    return runtime
