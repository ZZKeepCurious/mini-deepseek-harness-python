"""mcp-client 插件装载：连接一个 MCP server 并在激活前发布其初始工具代。

对应 dsh 真实源码：packages/mcp/mcp-client/src/index.ts（apply 语义）。
mini 以显式 `apply(ctx, config)` 编程式装载（无 Cordis loader / cordis.yml
插件树），返回连接句柄；fiber 拆解经 ctx.effect 自动 dispose（HMR 安全契约
等价物）。

装载顺序与上游一致：
1. fail-loud：重连配置（含程序化构造）先解析。
2. serverName 命名空间占位：重复 serverName 立即失败，早先实例不受影响。
3. 启动 supervisor（连接代 + 重连），接线 server 上下文。
4. `await ready`：首次连接 + 初始工具发现结算；failOnStartupError 时失败上抛。
"""
from __future__ import annotations

import asyncio
import logging
import threading
from typing import Any

from ..core.scope import Context
from .connection import McpServerConnection
from .server_context import register_server_context
from .types import resolve_mcp_config, resolve_reconnect_policy

logger = logging.getLogger("miniharness.mcp.client")

__all__ = ["apply", "McpServerConnection"]

#: 每个注册 scope 的活跃 serverName 集合（scope-of(ctx) → set；同名实例
#: 在同一 scope 内互斥。mini 用普通 dict + 拆除时删除，等价上游 WeakMap）。
_active_server_names: dict[int, set[str]] = {}
_active_server_names_lock = threading.Lock()

SCOPE_UNSET = object()


def _scope_key(ctx: Context) -> Any:
    try:
        from ..core.dsh_scope import scope_of

        key = scope_of(ctx)
        return key if key is not None else ctx.root
    except Exception:  # noqa: BLE001 - 无 scope 键时退回 root 对象
        return getattr(ctx, "root", ctx)


def _reserve_server_name(ctx: Context, server_name: str) -> None:
    owner = _scope_key(ctx)
    with _active_server_names_lock:
        names = _active_server_names.setdefault(id(owner), set())
        if server_name in names:
            raise ReferenceError(
                f'mcp-client: serverName "{server_name}" is already in use by another '
                "mcp-client instance — pick a unique serverName")
        names.add(server_name)


def _release_server_name(ctx: Context, server_name: str) -> None:
    owner = _scope_key(ctx)
    with _active_server_names_lock:
        names = _active_server_names.get(id(owner))
        if names:
            names.discard(server_name)
            if not names:
                _active_server_names.pop(id(owner), None)


async def apply(ctx: Context, config: dict) -> McpServerConnection:
    """装载一个 mcp-client 实例。

    @param ctx - 提供 `tools` registry（必需；mcpResources/systemPrompt 可选
      opt-in）的组合根。
    @param config - 原始配置（transport/serverName + stdio/http 专属字段 + 可选
      toolCallTimeoutMs/reconnect/failOnStartupError/maxInstructionBytes）。
    @returns 已连接的连接句柄（保留给调用方做 dispose / 资源 provider）。
    @raises ValueError - 配置非法（fail-loud）。
    @raises ReferenceError - serverName 与另一实例冲突。
    """
    normalized = resolve_mcp_config(config)
    reconnect = resolve_reconnect_policy(
        normalized["reconnect"], f"mcp-client({normalized['serverName']}): reconnect")
    normalized["reconnect"] = reconnect

    _reserve_server_name(ctx, normalized["serverName"])

    def _server_name_disposer() -> Callable:
        def _dispose() -> None:
            _release_server_name(ctx, normalized["serverName"])

        return _dispose

    # effect(execute, label)：execute 立即运行、其返回值被吸收为 disposer；
    # 这里 execute 返回 release 闭包 → fiber 拆解时才真正释放占位。
    ctx.effect(_server_name_disposer, "mcp-client.serverName")

    connection = McpServerConnection(ctx, normalized, reconnect)
    register_server_context(ctx, normalized["serverName"], connection)
    connection.start()
    # fiber 拆解 → dispose 连接（HMR 安全契约的 mini 等价物）。effect execute
    # 返回 bound method → 被吸收为 disposer，拆解时真正执行。
    ctx.effect(lambda: connection.dispose, "mcp-client.connection")

    try:
        outcome = await asyncio.wrap_future(connection.ready)
    except Exception:
        connection.dispose()
        raise
    if outcome.get("error") is not None and normalized.get("failOnStartupError"):
        error = outcome["error"]
        raise RuntimeError(
            f"mcp-client({normalized['serverName']}): initial connection or tool "
            "synchronization failed") from error
    return connection