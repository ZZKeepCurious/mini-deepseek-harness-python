"""web 表面（L3 应用与入口）：两信封 RPC + 会话服务 + 逻辑流 + HTTP/WS 传输。

对应上游 `packages/client/connection` + `packages/api/gateway` +
`packages/api/session-controller` + `packages/host/webserver`
（Web Host 层的 mini 复现；前端 React 不在本仓库，教学 SPA 在 `web/static/`）。
"""
from .envelope import (
    RPC_ERROR_CODES,
    EnvelopeError,
    client_request,
    parse_message,
    rpc_id,
    rpc_error,
    rpc_result_error,
    rpc_result_ok,
    server_response,
    transport_error,
)
from .api import WebApi, canonical_client_time_zone
from .streams import StreamHub

__all__ = [
    "RPC_ERROR_CODES",
    "EnvelopeError",
    "StreamHub",
    "WebApi",
    "canonical_client_time_zone",
    "client_request",
    "parse_message",
    "rpc_id",
    "rpc_error",
    "rpc_result_error",
    "rpc_result_ok",
    "server_response",
    "transport_error",
]