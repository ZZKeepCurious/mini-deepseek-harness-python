"""web 传输层：FastAPI 应用（对齐 `packages/host/apiproxy/src/fetch/handler.ts`）。

载体契约（逐条对应上游，HTTP 状态只表达载体层）：
  * `GET /api/events.mux` / `GET /api/events.host` → SSE：响应头
    content-type=text/event-stream + cache-control=no-cache；打开即写一行
    `: connected` 注释（host 流无基线，否则空闲时零字节）；每帧
    `data: <server-request 全形>\n\n`（method = 帧 type，rpcId 流级单一、
    连接打开时签发，对齐上游 `api.events.mux({rpcId: RpcId(randomUUID())...})`）。
    流中途异常 → 单条 `stream/error` 帧（新 rpcId，服务端主动推送）后关闭。
  * `POST /api/<method>`：body 为 client-request 全形。载体状态码 =
    404（非 POST / 不在 /api/ 下 / 方法不在路由表）/ 415（content-type
    非 application/json，跨站写围栏）/ 400（body 非 JSON）/ 500（实现崩溃，
    纯文本 `handler failure: ...`）；业务错误恒 200 + server-response
    （result.ok=false）。信封不合法 → 200 bad-request（尽力 salvage rpcId，
    兜底 `invalid-request` 哨兵）；path 与 message.method 不一致 → 200
    bad-request（details.issues=[]）。

教学简化（须在 AGENTS.md 标注）：
  * 无 `/api/respond`（无 approval/question 可应答交互）、无
    `GET /api/session.export`（无 downloads 域）、无 CORS 头。
  * 载荷 schema 校验在 WebApi 内做（上游先过 zod schema，mini 的 dispatch
    按各方法逐字段查并返回同款 bad-request）。
  * session 日志事件是 mappingproxy/tuple 冻结形态（core/session/json.py
    deep_freeze），序列化前经 thaw 还原为普通 JSON（上游 web 层拿的是活对象）。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import Response, StreamingResponse

from ..core.session.json import thaw
from .api import WebApi
from .envelope import parse_message, rpc_id, server_request
from .streams import StreamHub

__all__ = ["create_app"]

#: 信封无法读取 rpcId 时的回执哨兵（handler.ts INVALID_REQUEST_RPC_ID）
INVALID_REQUEST_RPC_ID = "invalid-request"


def _dumps(value: Any) -> str:
    """解冻（session 日志事件是 mappingproxy/tuple 冻结形态）后序列化。"""
    return json.dumps(thaw(value), ensure_ascii=False)


def _error_response(rpc_id_: str, code: str, message: str, details: dict) -> Response:
    """200 载体 + 业务错误 server-response（上游 errorResponse）。"""
    body = {
        "type": "server-response",
        "rpcId": rpc_id_,
        "result": {"ok": False, "error": {"code": code, "message": message, "details": details}},
    }
    return Response(_dumps(body), status_code=200, media_type="application/json")


def _sse_response(frames: Any) -> StreamingResponse:
    """把一个帧流包成 SSE（上游 sseResponse）：注释开播 + data 帧 + 中途失败折 stream/error。"""
    stream_rpc_id = rpc_id()

    async def event_source():
        yield ": connected\n\n"
        try:
            async for frame in frames:
                envelope = server_request(stream_rpc_id, frame["type"], frame)
                yield "data: " + _dumps(envelope) + "\n\n"
        except Exception as error:  # noqa: BLE001 - 流中途失败折成单帧后关闭（上游同款）
            failure = {"type": "stream/error",
                       "error": {"code": "internal", "message": str(error), "details": {}}}
            yield "data: " + _dumps(server_request(rpc_id(), "stream/error", failure)) + "\n\n"

    return StreamingResponse(event_source(), media_type="text/event-stream",
                             headers={"cache-control": "no-cache"})


def create_app(api: WebApi, hub: StreamHub) -> FastAPI:
    """把 WebApi + StreamHub 装成 FastAPI 应用（供 launcher/uvicorn 挂载）。

    @param api - web 会话服务（提交 B）。
    @param hub - mux/host 事件流中心（提交 C）。
    @returns 可挂载的 FastAPI 实例。
    """
    app = FastAPI(title="mini-deepseek-harness web", docs_url=None, redoc_url=None,
                  openapi_url=None)

    @app.api_route("/api/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH",
                                                "OPTIONS", "HEAD"])
    async def api_entry(path: str, request: Request) -> Response:
        pathname = request.url.path
        method = request.method

        # 无信封读通道：SSE 事件流（handler.ts 物理路由，先于 POST 围栏）
        if method == "GET" and pathname == "/api/events.mux":
            return _sse_response(hub.mux())
        if method == "GET" and pathname == "/api/events.host":
            return _sse_response(hub.host())

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
        if method_name not in api.methods():
            return Response("not found", status_code=404)

        # 信封校验：不合法 → 200 bad-request，尽力 salvage rpcId
        try:
            message = parse_message(body)
        except Exception as error:  # noqa: BLE001 - EnvelopeError
            raw_id = body.get("rpcId") if isinstance(body, dict) else None
            rpc_id_ = raw_id if isinstance(raw_id, str) else INVALID_REQUEST_RPC_ID
            return _error_response(rpc_id_, "bad-request", "invalid client-request message",
                                   {"issues": [{"path": [], "message": str(error)}]})

        # path 与 message.method 不一致 → 200 bad-request
        if message["method"] != method_name:
            return _error_response(message["rpcId"], "bad-request",
                                   f'method "{message["method"]}" does not match path "{method_name}"',
                                   {"issues": []})

        # 业务派发：impl 不抛业务错误；到达这里仍抛 = 实现崩溃 → 500 载体层
        try:
            response = api.dispatch(method_name, message["rpcId"], message["payload"])
        except Exception as error:  # noqa: BLE001 - 实现崩溃
            return Response(f"handler failure: {error}", status_code=500)
        if response is None:
            return Response("not found", status_code=404)
        return Response(_dumps(response), status_code=200, media_type="application/json")

    return app