from __future__ import annotations

import mimetypes
import os

from fastapi import Request
from fastapi.responses import Response

from ..fs.types import FsError
from .api import WebApi

MEDIA_FILE_PATH = "/api/file"

BASE_HEADERS = {
    "Cache-Control": "private, no-store",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": "sandbox; default-src 'none'",
}

ERROR_STATUS = {
    "FS_NOT_FOUND": 404,
    "FS_NOT_REGULAR_FILE": 403,
    "FS_PERMISSION_DENIED": 403,
    "FS_SANDBOX_DENIED": 403,
    "FS_TOO_LARGE": 413,
    "FS_ABORTED": 499,
}


def _fail(request: Request, status: int, text: str) -> Response:
    body = None if request.method == "HEAD" else text
    return Response(body, status_code=status, headers=BASE_HEADERS)


async def handle_file_request(api: WebApi, request: Request) -> Response:
    fs = api.ctx.get("fs")
    attachments = api.ctx.get("attachments")
    if fs is None or attachments is None:
        return Response("not found", status_code=404)

    path = request.query_params.get("path")
    if path is None or path == "":
        return _fail(request, 400, "missing path")
    if "\0" in path or not os.path.isabs(path):
        return _fail(request, 400, "absolute path required")

    max_bytes = attachments.image_limits.maxImageBytes
    try:
        target = await fs.resolve(path, {"signal": None})
        media_type = mimetypes.guess_type(target.display_path, strict=False)[0]
        headers = {**BASE_HEADERS, "Content-Type": media_type or "application/octet-stream"}
        if request.method == "HEAD":
            info = await fs.stat(target, None)
            if info is None:
                return _fail(request, 404, "not found")
            if info.type != "file":
                return _fail(request, 403, "not a regular file")
            if info.size is not None:
                if info.size > max_bytes:
                    return _fail(request, 413, "file exceeds byte limit")
                headers["Content-Length"] = str(info.size)
            return Response(None, headers=headers)
        data = await fs.read_bytes(target, None, max_bytes)
        headers["Content-Length"] = str(len(data))
        return Response(data, headers=headers)
    except FsError as error:
        return _fail(request, ERROR_STATUS.get(error.code, 500), error.code)
