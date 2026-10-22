"""原始字节上传路由（对齐 packages/client/file-upload/src/http-route.ts）。

`POST /api/session/uploadFileBinary?sessionId=<id>&name=<display>`：
  * 只接受 `application/octet-stream`，请求体按块流式落盘（不聚合、不 base64）；
  * 载体拒绝 405（allow: POST）/ 415 / 400（缺 sessionId）；
  * 业务结果固定 HTTP 200：`{ok:true, value:{receiptId,file}}` 或
    `{ok:false, error:{code,message,details}}`；
  * 成功后向 `ctx.fileUploads` 签收 receipt，prompt 以 `{type:'file',receiptId}`
    引用。

载体差异：上游 `request.signal` 由 Node 请求对象提供；mini 用显式 abort 标记，
客户端断连时在下一个块边界停止并让附件存储删除 spool。
"""
from __future__ import annotations

import asyncio
import json
from typing import Any

import anyio
from fastapi import Request
from fastapi.responses import Response
from starlette.requests import ClientDisconnect

from ..attachment.error import AttachmentError
from ..attachment.types import SaveFileStreamAttachment

__all__ = [
    "UPLOAD_FILE_BINARY_PATH",
    "handle_file_upload_http",
]

UPLOAD_FILE_BINARY_PATH = "/api/session/uploadFileBinary"
_END = object()


class _Abort:
    aborted = False


def _json(body: dict) -> Response:
    return Response(json.dumps(body, ensure_ascii=False), status_code=200,
                    headers={"content-type": "application/json; charset=utf-8",
                             "cache-control": "no-store"})


def _failure(code: str, message: str, details: dict | None = None) -> Response:
    return _json({"ok": False,
                  "error": {"code": code, "message": message,
                            "details": details or {}}})


async def handle_file_upload_http(api: Any, request: Request) -> Response:
    """处理一次已认证的原始字节上传（无信封）。"""
    if request.method != "POST":
        return Response(status_code=405, headers={"allow": "POST"})
    media_type = (request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if media_type != "application/octet-stream":
        return Response("content type must be application/octet-stream", status_code=415)
    session_id = request.query_params.get("sessionId")
    if session_id is None or session_id == "":
        return Response("sessionId is required", status_code=400)
    name = request.query_params.get("name")

    session = api.store.get(session_id)
    if session is None:
        return _failure("session/not-found", f'session "{session_id}" not found',
                        {"sessionId": session_id})
    if session.meta.get("origin") == "subagent":
        return _failure("subagent/attachment-invalid",
                        "subagent conversations do not accept file uploads",
                        {"reason": "SUBAGENT_FILE_UNSUPPORTED"})
    try:
        found = api._agent_for(session_id)
    except Exception as error:  # noqa: BLE001 - _Reject 折业务错误体
        return _failure(getattr(error, "code", "gateway/internal"),
                        getattr(error, "message", str(error)),
                        getattr(error, "details", {}))
    if found is None:
        return _failure("session/not-found", f'session "{session_id}" not found',
                        {"sessionId": session_id})

    attachments = api.ctx.get("attachments")
    if attachments is None:
        return _failure("gateway/internal",
                        "failed to store file upload: attachment service is unavailable", {})

    loop = asyncio.get_running_loop()
    chunks: asyncio.Queue[Any] = asyncio.Queue(maxsize=8)
    abort = _Abort()

    async def pump() -> None:
        try:
            async for chunk in request.stream():
                await chunks.put(bytes(chunk))
        except ClientDisconnect:
            abort.aborted = True
        finally:
            await chunks.put(_END)

    def source():
        while True:
            item = asyncio.run_coroutine_threadsafe(chunks.get(), loop).result()
            if item is _END:
                return
            yield item

    def save():
        return attachments.save_file_stream(SaveFileStreamAttachment(
            data=source(), signal=abort, name=name))

    task = asyncio.create_task(pump())
    try:
        ref = await anyio.to_thread.run_sync(save)
    except AttachmentError as error:
        return _failure("session/attachment-invalid", error.message,
                        {"reason": error.code})
    except Exception as error:  # noqa: BLE001 - 非附件错误折 gateway/internal
        return _failure("gateway/internal", f"failed to store file upload: {error}", {})
    finally:
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, ClientDisconnect):
            pass

    if api.store.get(session_id) is None:
        return _failure("session/not-found",
                        f'session "{session_id}" was disposed before its file upload completed',
                        {"sessionId": session_id})
    service = api.ctx.get("fileUploads")
    if service is None:
        return _failure("gateway/internal",
                        "failed to store file upload: file uploads service is unavailable", {})
    return _json({"ok": True, "value": service.stage(session_id, ref)})
