"""MCP 传输面：子进程 stdio 参数 + Streamable HTTP URL/请求头。

对应 dsh 真实源码：packages/mcp/mcp-client/src/transport.ts。子进程 env =
`{...scrubbedParentEnv(), ...config.env}`（提前 scrubbed_parent_env 为规范
基底，显式 env 其后合并；对齐 subagent-dsh-sdk run.ts:123 同款形态）。
"""
from __future__ import annotations

import sys
from typing import Any
from urllib.parse import urlparse

from mcp import StdioServerParameters

from ..seams.subprocess_env import scrubbed_parent_env

__all__ = ["build_child_env", "create_stdio_parameters", "create_http_parameters"]


def build_child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """子进程环境基底：父环境 scrubbed 后合并且不做隐式凭据/DSH_* 泄漏。

    @param extra - 调用方显式 env（在 scrub 之后合并，刻意转发生效）。
    """
    return {**scrubbed_parent_env(), **(extra or {})}


def create_stdio_parameters(config: dict[str, Any]) -> StdioServerParameters:
    """构建 stdio 传输参数（对齐上游 createTransport 的 stdio 分支）。

    cwd 空串视为未设置（SDK 缺省继承父进程工作目录）。
    """
    return StdioServerParameters(
        command=config["command"],
        args=list(config.get("args", [])),
        env=build_child_env(config.get("env") or None),
        cwd=config.get("cwd") or None,
    )


def create_http_parameters(config: dict[str, Any]) -> dict[str, Any]:
    """校验 + 归一 Streamable HTTP 端点参数。

    @returns {"url": str, "headers": dict}；上游创建 URL 对象 + requestInit。
    @raises ValueError - url 不是合法 HTTP(S) 绝对地址。
    """
    url = config["url"]
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ValueError(f"mcp-client: invalid streamable-http url: {url}")
    return {"url": url, "headers": dict(config.get("headers", {}) or {})}


def pick_errlog():
    """默认 errlog 目标：对齐 SDK stdio_client 缺省（stderr）。

    SDK 的 stdio errlog 透传给子进程 stderr 管道；mini 保持缺省 stderr。
    """
    return sys.stderr