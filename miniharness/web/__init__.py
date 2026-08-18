"""web 表面（L3 应用与入口）：四象限 RPC 信封 + 会话服务 + 事件流 + HTTP 传输。

对应上游 `packages/host/apiproxy` + `packages/host/webserver` + `packages/bundle/web-app`
（Web Host 层的 mini 复现；前端 React 不在本仓库）。
"""
from .envelope import (
    RPC_ERROR_CODES,
    EnvelopeError,
    client_request,
    client_response,
    parse_message,
    rpc_id,
    rpc_error,
    rpc_receipt_accepted,
    rpc_receipt_rejected,
    rpc_result_error,
    rpc_result_ok,
    server_request,
    server_response,
    transport_error,
)

__all__ = [
    "RPC_ERROR_CODES",
    "EnvelopeError",
    "client_request",
    "client_response",
    "parse_message",
    "rpc_id",
    "rpc_error",
    "rpc_receipt_accepted",
    "rpc_receipt_rejected",
    "rpc_result_error",
    "rpc_result_ok",
    "server_request",
    "server_response",
    "transport_error",
]