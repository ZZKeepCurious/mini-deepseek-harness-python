"""web 传输层：FastAPI 应用（对齐 `packages/api/gateway` + `packages/client/connection`）。

载体契约（逐条对应上游 alpha.1）：
  * **unary RPC**：`POST /api/<endpoint>`，body 为 `client-request` 全形
    `{type:'client-request', rpcId, method, payload}`，payload 恰为 `{args: {...}}`
    单字段 plain object（对齐 gateway `remoteRequest` 严格校验：多余键/缺 args
    拒绝）。响应 `server-response` `{type, rpcId, result}`。载体状态码 =
    404（非 POST / 不在 /api/ 下 / 方法不在路由表）/ 415（content-type 非
    application/json，跨站写围栏）/ 400（body 非 JSON）；业务错误恒 200 +
    result.ok=false。信封不合法 → 200 bad-request；path 与 message.method 不一致
    → 200 bad-request（details.issues=[]）。
  * **`$events/result` unary**：endpoint 特判；payload 恰
    `{args:{clientId,eventId,outcome}}`，词法经 `parse_remote_event_result_payload`
    校验后交 `gateway.receive_result` 结算（对齐 gateway dispatchRpc 的
    $events/result + receiveRemoteEventResult）。合法 → 200 `{ok:true}`；
    非法/未知 clientId → 200 `{ok:false, error:{code,message,details}}`
    （RpcResult 形态，rpcFailure 折叠）。
  * **WS `/api/remote.mux`**：`package/api/gateway` 单一路径承载所有 Remote 流
    （open/cancel/item/end/error 帧），由 `web/mux.py` 消费。二进制/非法/重复
    open 的 close 码与错误帧语义见 mux docstring。
  * **`GET /api/session.export`**：downloads 域数据导出（沿用 `web/downloads.py`，
    无 CORS 头，跨站写围栏即安全机制）。
  * 非 `/api/` 的 GET/HEAD → 静态服务（SPA，`web/frontend.py`），其余方法 405。

教学简化（须在 AGENTS.md 标注）：session 日志事件是 mappingproxy/tuple 冻结形态
（core/session/json.py deep_freeze），序列化前经 thaw 还原；`$events` 为单帧
载体（无 mux `since` 恢复游标，见 verified-diffs §3.4）；心跳 = launcher
transport 级 uvicorn 选项（2s Ping + 4s 判死，见 mux docstring）。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import Response

from ..core.session.json import thaw
from .api import WebApi
from .downloads import build_session_export, parse_export_query
from .envelope import (
    parse_message,
    rpc_error,
    rpc_result_error,
    rpc_result_ok,
)
from .frontend import DIST_INDEX, DIST_ROOT, serve_static
from .mux import RemoteStreamMuxConnection
from .stream_protocol import (
    REMOTE_STREAM_MUX_PATH,
    StreamProtocolError,
    parse_remote_event_result_payload,
)
from .streams import GatewayStreams, RemoteStreamError

__all__ = ["create_app"]

#: 信封无法读取 rpcId 时的回执哨兵（gateway INVALID_REQUEST_RPC_ID）
INVALID_REQUEST_RPC_ID_VALUE = "invalid-request"


def _dumps(value: Any) -> str:
    """解冻（session 日志事件是 mappingproxy/tuple 冻结形态）后序列化。"""
    return json.dumps(thaw(value), ensure_ascii=False)


def _error_response(rpc_id_: str, code: str, message: str, details: dict) -> Response:
    """200 载体 + 业务错误 server-response（gateway errorResponse 同款）。"""
    body = {
        "type": "server-response",
        "rpcId": rpc_id_,
        "result": {"ok": False, "error": {"code": code, "message": message,
                                          "details": details}},
    }
    return Response(_dumps(body), status_code=200, media_type="application/json")


def _unwrap_args(payload: Any, method: str) -> Any:
    """严格 payload：恰 `{args: {...}}` 单字段（gateway remoteRequest 同款）。

    返回 unwrapped args；不合法抛 RemoteStreamError（统一折 bad-request）。
    """
    if not isinstance(payload, dict) or set(payload) != {"args"}:
        raise RemoteStreamError(
            "gateway/bad-request",
            f"typert gateway: {method}: requires exactly one plain-object args field")
    if not isinstance(payload["args"], dict):
        raise RemoteStreamError(
            "gateway/bad-request",
            f"typert gateway: {method}: payload args must be a plain object")
    return payload["args"]


def create_app(api: WebApi, gateway: GatewayStreams) -> FastAPI:
    """把 WebApi + GatewayStreams 装成 FastAPI 应用（供 launcher/uvicorn 挂载）。

    @param api - web 会话服务（unary 域处理）。
    @param gateway - Remote 方法面（$events + follow/control + 审批桥）。
    @returns 可挂载的 FastAPI 实例。
    """
    app = FastAPI(title="mini-deepseek-harness web", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH",
                                                "OPTIONS", "HEAD"])
    async def api_entry(path: str, request: Request) -> Response:
        pathname = request.url.path
        method = request.method

        # 无信封下载通道：GET /api/session.export（物理路由，先于 POST 围栏）
        if method in ("GET", "HEAD") and pathname == "/api/session.export":
            session_id = request.query_params.get("sessionId")
            include_raw = request.query_params.get("includeDescendants")
            query = parse_export_query(
                {"sessionId": session_id or "", "includeDescendants": include_raw})
            if query is None:
                return Response("missing or invalid session.export query parameters",
                                status_code=400)
            session_id, include_descendants = query
            result = build_session_export(api.ctx, session_id, include_descendants,
                                         method=method)
            return Response(result.body, status_code=result.status, headers=result.headers)

        # 载体状态码 404：非 POST 或不在 /api/ 下的路径
        if method != "POST" or not pathname.startswith("/api/"):
            return Response("not found", status_code=404)

        # 跨站写围栏：只收 application/json（上游 415 = 载体层）
        media_type = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            return Response("content type must be application/json", status_code=415)

        # body 非 JSON → 400（载体层；JSON 但形状坏 → 200 + bad-request）
        try:
            body: Any = await request.json()
        except Exception:  # noqa: BLE001 - json 解析失败（含空体）
            return Response("body is not JSON", status_code=400)

        method_name = pathname[len("/api/"):]

        # 可应答交互结算入口：POST /api/$events/result（gateway dispatchRpc 特判）
        if method_name == "$events/result":
            return _events_result_response(gateway, body)

        if method_name not in api.methods():
            return Response("not found", status_code=404)

        # 信封校验：不合法 → 200 bad-request，尽力 salvage rpcId
        try:
            message = parse_message(body)
        except Exception as error:  # noqa: BLE001 - EnvelopeError
            raw_id = body.get("rpcId") if isinstance(body, dict) else None
            rpc_id_ = raw_id if isinstance(raw_id, str) else INVALID_REQUEST_RPC_ID_VALUE
            return _error_response(rpc_id_, "gateway/bad-request", "invalid client-request message",
                                   {"issues": [{"path": [], "message": str(error)}]})

        # path 与 message.method 不一致 → 200 bad-request
        if message["method"] != method_name:
            return _error_response(message["rpcId"], "gateway/bad-request",
                                   f'method "{message["method"]}" does not match path "{method_name}"',
                                   {"issues": []})

        # payload 严格 {args} 单字段 → unwrap 后派发（bad-request 折 RpcResult）
        try:
            args = _unwrap_args(message["payload"], method_name)
        except RemoteStreamError as error:
            return _error_response(message["rpcId"], error.code, error.message,
                                   {"issues": []})

        # 业务派发：impl 不抛业务错误；到达这里仍抛 = 实现崩溃 → 500 载体层
        try:
            response = api.dispatch(method_name, message["rpcId"], args)
        except Exception as error:  # noqa: BLE001 - 实现崩溃
            return Response(f"handler failure: {error}", status_code=500)
        if response is None:
            return Response("not found", status_code=404)
        return Response(_dumps(response), status_code=200, media_type="application/json")

    @app.websocket(REMOTE_STREAM_MUX_PATH)
    async def remote_mux(websocket: WebSocket) -> None:
        """`/api/remote.mux` WebSocket：全部 Remote 流的单一路径载体。"""
        await websocket.accept()
        conn = RemoteStreamMuxConnection(gateway, websocket)
        try:
            await conn.run()
        except WebSocketDisconnect:
            pass
        finally:
            conn.dispose()

    # 非 /api/ 路径：SPA 静态服务（GET/HEAD → frontend-static 契约，其余方法 405）
    @app.api_route("/{path:path}", methods=["GET", "HEAD", "POST", "PUT", "DELETE", "PATCH",
                                            "OPTIONS"])
    async def static_entry(path: str, request: Request) -> Response:
        if request.url.path.startswith("/api/"):
            return Response("not found", status_code=404)
        if request.method not in ("GET", "HEAD"):
            return Response("method not allowed", status_code=405)
        served = serve_static(request.url.path, DIST_ROOT, DIST_INDEX)
        if served is None:
            return Response("not found", status_code=404)
        status, headers, body = served
        return Response(body, status_code=status, headers=headers)

    return app


def _events_result_response(gateway: GatewayStreams, body: Any) -> Response:
    """`$events/result` unary：词法校验 + 结算，返回 server-response（失败折 RpcResult）。"""
    def envelope(result: dict) -> Response:
        rpc_id = body.get("rpcId") if isinstance(body, dict) and isinstance(
            body.get("rpcId"), str) else INVALID_REQUEST_RPC_ID_VALUE
        return Response(_dumps({"type": "server-response", "rpcId": rpc_id,
                                "result": result}), status_code=200,
                        media_type="application/json")

    try:
        payload = body if isinstance(body, dict) and "args" in body else None
        if payload is None:
            raise RemoteStreamError("gateway/bad-request",
                                    "typert gateway: $events/result requires an args field")
        result = parse_remote_event_result_payload(payload)
    except StreamProtocolError as error:
        return envelope(rpc_result_error(rpc_error(
            "gateway/bad-request", str(error), {"issues": []})))
    except Exception as error:  # noqa: BLE001
        return envelope(rpc_result_error(rpc_error(
            "gateway/internal", str(error), {})))
    try:
        gateway.receive_result(result)
    except RemoteStreamError as error:
        return envelope(rpc_result_error(rpc_error(
            error.code, error.message, {})))
    except Exception as error:  # noqa: BLE001
        return envelope(rpc_result_error(rpc_error(
            "gateway/internal", str(error), {})))
    return envelope(rpc_result_ok())
