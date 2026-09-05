"""web 认证门：可选 token（上游 `connection.requestRejection(req)` 钩子的 mini 等价物）。

对齐上游 alpha.1 形态（packages/api/gateway）：
  * WS 升级被拒 = 先写 HTTP 401 响应再断开（`rejectRemoteStreamUpgrade`，
    stream-server.ts:213 写 ``HTTP/1.1 401 Unauthorized`` 后销毁 socket）；mini 经
    ASGI ``websocket.http.response.*`` 在 accept 前发 401（uvicorn 支持，形态一致）；
  * 拒绝决策是**可插拔的**（上游由宿主连接层注入 requestRejection）；mini 以
    可选 token 实现：未配置 = 无门（回环开发形态），配置后 /api/* 全域强制。

token 载体（浏览器约束驱动）：
  * `Authorization: Bearer <token>` 头（unary/fetch 与编程客户端）；
  * `?token=<token>` 查询参数（浏览器原生 WebSocket 无法设头；session.export
    下载链接同理）。两者任一命中即可。

部署纪律（生产就绪缺省）：`run_web` 监听 `0.0.0.0` 时**必须**已配置 token
（`MINIHARNESS_WEB_TOKEN`），否则 fail loud——非回环裸听是安全实质风险。
"""
from __future__ import annotations

import hmac
import os
from typing import Any
from urllib.parse import parse_qs

__all__ = [
    "WEB_TOKEN_ENV",
    "TokenGateMiddleware",
    "extract_bearer",
    "provided_token",
    "query_token",
    "resolve_web_token",
    "token_matches",
]

#: token 环境变量名（部署面；launcher/server 共用）。
WEB_TOKEN_ENV = "MINIHARNESS_WEB_TOKEN"

_UNAUTHORIZED_BODY = b'{"error":"unauthorized"}'


def resolve_web_token(explicit: str | None = None) -> str | None:
    """解析生效 token：显式参数 > 环境变量；空串视为未配置。"""
    token = explicit if explicit is not None else os.environ.get(WEB_TOKEN_ENV)
    return token if token else None


def extract_bearer(authorization: str | None) -> str | None:
    """从 ``Authorization: Bearer <token>`` 头取 token；非 Bearer/空 → None。"""
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    value = value.strip()
    if scheme.lower() != "bearer" or not value:
        return None
    return value


def query_token(query_string: bytes | str) -> str | None:
    """从 URL 查询串取 ``token`` 参数（浏览器 WebSocket/下载链接载体）。"""
    raw = query_string.decode("latin-1") if isinstance(query_string, bytes) else query_string
    values = parse_qs(raw or "").get("token")
    return values[0] if values else None


def provided_token(scope: dict) -> str | None:
    """从 ASGI scope 提取请求方 token：``?token=`` 优先，其次 Bearer 头。"""
    token = query_token(scope.get("query_string") or b"")
    if token:
        return token
    for name, value in scope.get("headers") or []:
        if name.decode("latin-1").lower() == "authorization":
            return extract_bearer(value.decode("latin-1"))
    return None


def token_matches(provided: str | None, expected: str) -> bool:
    """常数时间比较（防时序侧信道）。"""
    if provided is None:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


class TokenGateMiddleware:
    """ASGI 中间件：配置 token 后，``/api/*`` 全域强制（静态 SPA 不设门——
    shell 需可达，浏览器从页面 URL 带 token 调 API）。

    http 请求失败 → 401 JSON 载体（不泄策略细节）；websocket 升级失败 →
    ``websocket.http.response.*`` 写 HTTP 401 后 ``websocket.close``（上游
    rejectRemoteStreamUpgrade 同形：HTTP 401 响应 + socket 销毁）。
    """

    def __init__(self, app: Any, token: str) -> None:
        if not isinstance(token, str) or not token:
            raise ValueError("web token gate requires a non-empty token")
        self.app = app
        self.token = token

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") not in ("http", "websocket") \
                or not (scope.get("path") or "").startswith("/api"):
            await self.app(scope, receive, send)
            return
        if token_matches(provided_token(scope), self.token):
            await self.app(scope, receive, send)
            return
        if scope["type"] == "http":
            await send({
                "type": "http.response.start",
                "status": 401,
                "headers": [(b"content-type", b"application/json")],
            })
            await send({"type": "http.response.body", "body": _UNAUTHORIZED_BODY})
            return
        await send({
            "type": "websocket.http.response.start",
            "status": 401,
            "headers": [(b"content-type", b"application/json")],
        })
        await send({"type": "websocket.http.response.body", "body": _UNAUTHORIZED_BODY})
        await send({"type": "websocket.close", "code": 1008})
