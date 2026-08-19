"""web profile 启动器：组装 WebApi + StreamHub + FastAPI 应用并监听。

对齐上游 `packages/host/webserver` 的监听契约（index.ts Config）：
  * host 只允许两值 '127.0.0.1' / '0.0.0.0'（loopback / all-interfaces）；
  * port 为自然数，0 表示由操作系统分配（listen 后经 server.port 读取实际端口）。

mini 教学简化（须在 AGENTS.md 标注）：host/port 从
MINIHARNESS_WEB_HOST / MINIHARNESS_WEB_PORT 环境变量读（缺省
'127.0.0.1' / '0'），而非上游组合配置节；本 profile 的持久化沿用
WebApi 的默认内存 SessionStore（无 JSONL 落盘，与 headless 不同）。
"""
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from ..core.scope import Context
from .api import WebApi
from .server import create_app
from .streams import StreamHub

if TYPE_CHECKING:  # 可选依赖：fastapi 缺失时 launcher 仍可导入（[web] extra）
    from fastapi import FastAPI

__all__ = ["build_app", "run_web"]

HOSTS = ("127.0.0.1", "0.0.0.0")


def _resolve_bind(host: str | None, port: int | None) -> tuple[str, int]:
    host = host if host is not None else os.environ.get("MINIHARNESS_WEB_HOST", "127.0.0.1")
    if host not in HOSTS:
        raise ValueError(f"web host must be one of {list(HOSTS)}, got {host!r}")
    raw_port = port if port is not None else int(os.environ.get("MINIHARNESS_WEB_PORT", "0"))
    if not 0 <= raw_port <= 65535:
        raise ValueError(f"web port must be in 0..65535, got {raw_port!r}")
    return host, raw_port


def build_app(adapter: Any, tools: Any, ctx: Context | None = None) -> FastAPI:
    """纯装配：上下文 + 适配器 + 工具 → 可挂载的 FastAPI 应用。

    返回前把 WebApi/StreamHub 挂到 root ctx（供测试/launcher 复用，
    与 headless 的 ctx 装配对称）。
    """
    ctx = ctx or Context(name="web")
    api = WebApi(ctx, adapter, tools)
    hub = StreamHub(ctx, api)
    return create_app(api, hub)


def run_web(adapter: Any, tools: Any, ctx: Context | None = None,
            host: str | None = None, port: int | None = None) -> None:
    """构建应用并阻塞监听（`--profile web` 的进程级入口）。"""
    import uvicorn

    host, port = _resolve_bind(host, port)
    app = build_app(adapter, tools, ctx)
    uvicorn.run(app, host=host, port=port, log_level="info")