"""web profile 启动器：组装 WebApi + GatewayStreams + FastAPI 应用并监听。

对齐上游 `packages/host/webserver` 的监听契约（index.ts Config）：
  * host 只允许两值 '127.0.0.1' / '0.0.0.0'（loopback / all-interfaces）；
  * port 为自然数，0 表示由操作系统分配（listen 后经 server.port 读取实际端口）。

mini 教学简化（须在 AGENTS.md 标注）：host/port 从
MINIHARNESS_WEB_HOST / MINIHARNESS_WEB_PORT 环境变量读（缺省
'127.0.0.1' / '0'），而非上游组合配置节；本 profile 的持久化沿用
WebApi 的默认内存 SessionStore（无 JSONL 落盘，与 headless 不同）。

心跳（alpha.1 复核批对齐上游 gateway heartbeat 契约）：每条 WS 连接按
`websocketHeartbeatIntervalMs` 缺省 **2000ms** 发 transport 级 Ping，连续
**2 个周期未收到 Pong 即 terminate**（上游 `RemoteStreamMuxServer`
MAX_MISSED_HEARTBEATS=2）。mini 由 uvicorn `websockets` 透传等价语义：
`ws_ping_interval=2`、`ws_ping_timeout=4`（miss 2 周期 ≈ 4s 无 Pong 判死）。
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from ..core.scope import Context
from .api import WebApi
from .auth import resolve_web_token
from .server import create_app
from .streams import GatewayStreams

if TYPE_CHECKING:  # 可选依赖：fastapi 缺失时 launcher 仍可导入（[web] extra）
    from fastapi import FastAPI

__all__ = ["build_app", "run_web", "uvicorn_options"]

HOSTS = ("127.0.0.1", "0.0.0.0")

#: transport 级心跳间隔（秒）。对齐 upstream gateway `websocketHeartbeatIntervalMs`
#: 缺省 2000（index.ts Config @default 2000）。
WS_HEARTBEAT_INTERVAL = 2.0

#: 连续 miss 判死预算（秒）≈ MAX_MISSED_HEARTBEATS × interval（上游
#: stream-server.ts MAX_MISSED_HEARTBEATS=2：连续 2 周期无 Pong 即 terminate）。
WS_HEARTBEAT_TIMEOUT = 4.0


def uvicorn_options(heartbeat_interval: float | None = WS_HEARTBEAT_INTERVAL,
                    heartbeat_timeout: float | None = WS_HEARTBEAT_TIMEOUT) -> dict:
    """构造 `uvicorn.run` 的 WS 相关选项（可测的纯函数）。

    @param heartbeat_interval - 心跳间隔；None 关闭（测试/禁用）。
    @param heartbeat_timeout - Pong 判死预算（上游 miss 2 周期 terminate 的
    transport 级等价映射）；None = 只保活不强制 Pong。
    @returns 传给 uvicorn 的 kwargs。
    """
    if heartbeat_interval is None:
        return {}
    if not isinstance(heartbeat_interval, (int, float)) or not heartbeat_interval > 0:
        raise ValueError("ws heartbeat interval must be a positive number")
    options: dict[str, Any] = {"ws_ping_interval": heartbeat_interval}
    if heartbeat_timeout is None:
        options["ws_ping_timeout"] = None
    else:
        if (not isinstance(heartbeat_timeout, (int, float)) or not heartbeat_timeout > 0):
            raise ValueError("ws heartbeat timeout must be a positive number")
        options["ws_ping_timeout"] = heartbeat_timeout
    return options


def _resolve_bind(host: str | None, port: int | None) -> tuple[str, int]:
    host = host if host is not None else os.environ.get("MINIHARNESS_WEB_HOST", "127.0.0.1")
    if host not in HOSTS:
        raise ValueError(f"web host must be one of {list(HOSTS)}, got {host!r}")
    raw_port = port if port is not None else int(os.environ.get("MINIHARNESS_WEB_PORT", "0"))
    if not 0 <= raw_port <= 65535:
        raise ValueError(f"web port must be in 0..65535, got {raw_port!r}")
    return host, raw_port


def build_app(adapter: Any, tools: Any, ctx: Context | None = None,
              token: str | None = None) -> FastAPI:
    """纯装配：上下文 + 适配器 + 工具 → 可挂载的 FastAPI 应用。

    返回前把 WebApi/GatewayStreams 挂到 root ctx（供测试/launcher 复用，
    与 headless 的 ctx 装配对称）。token = 可选认证门（None 读
    MINIHARNESS_WEB_TOKEN 环境变量，仍未配置 = 无门；见 web/auth.py）。
    """
    ctx = ctx or Context(name="web")
    api = WebApi(ctx, adapter, tools)
    return create_app(api, api.gateway, token=token)


def run_web(adapter: Any, tools: Any, ctx: Context | None = None,
            host: str | None = None, port: int | None = None,
            token: str | None = None) -> None:
    """构建应用并阻塞监听（`--profile web` 的进程级入口）。

    生产纪律：监听 `0.0.0.0`（非回环）时**必须**已配置 token（参数或
    `MINIHARNESS_WEB_TOKEN`）——非回环裸听是安全实质风险，fail loud。
    """
    import uvicorn

    host, port = _resolve_bind(host, port)
    resolved_token = token if token is not None else resolve_web_token()
    if host == "0.0.0.0" and not resolved_token:
        raise ValueError(
            "listening on 0.0.0.0 requires a web token "
            "(pass token= or set MINIHARNESS_WEB_TOKEN)")
    app = build_app(adapter, tools, ctx, token=resolved_token)
    uvicorn.run(app, host=host, port=port, log_level="info", **uvicorn_options())