"""DeepSeek Files API 传输（Chat Completions 与 Messages 端点）。

对应 dsh 真实源码：packages/llm/llm-deepseek/src/common/files-api.ts。

载体差异：上游以 Web `fetch`/`FormData`/`Blob` 实现；mini 用 httpx（异步）。
`redirect: 'error'` 语义以 httpx 缺省不跟随重定向承载——凭据不会离开配置源。
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..protocol import (
    AUTH,
    FILES_API,
    INVALID_REQUEST,
    INVALID_RESPONSE,
    RATE_LIMIT,
    SERVER,
    TRANSPORT,
    LlmFailure,
)
from .file_id import DeepSeekFileId

__all__ = [
    "MAX_FILE_EXPIRY_SECONDS",
    "MAX_FILE_UPLOAD_BYTES",
    "MAX_STORED_FILE_BYTES",
    "MAX_STORED_FILE_COUNT",
    "MESSAGES_FILES_BETA",
    "MIN_FILE_EXPIRY_SECONDS",
    "DeepSeekFileObject",
    "DeepSeekFilePage",
    "DeepSeekFilesClient",
    "DeepSeekFilesError",
    "is_files_quota_error",
    "parse_file_object",
    "parse_messages_file",
    "provider_error_detail",
]

#: Messages 文件操作与 file-referenced 图片请求所需的显式 opt-in。
MESSAGES_FILES_BETA = "files-api-2025-04-14"
#: provider 支持的最小文件寿命。
MIN_FILE_EXPIRY_SECONDS = 3_600
#: provider 支持的最大文件寿命。
MAX_FILE_EXPIRY_SECONDS = 2_592_000
#: Files API 上传的字节上限。
MAX_FILE_UPLOAD_BYTES = 128 * 1024 * 1024
#: 当前 per-key 文件数配额。
MAX_STORED_FILE_COUNT = 10_000
#: 当前 per-key 存储配额。
MAX_STORED_FILE_BYTES = 25 * 1024 * 1024 * 1024

_QUOTA_DETAIL = re.compile(
    r"(?:quota|storage|stored files|file count|too many files)", re.IGNORECASE)


class DeepSeekFilesError(LlmFailure):
    """保留 HTTP 状态以供恢复策略使用的 Files API 操作失败（上游 DeepSeekFilesError）。"""

    def __init__(self, message: str, status: int, detail: str) -> None:
        code = (AUTH if status in (401, 403)
                else RATE_LIMIT if status == 429
                else SERVER if status >= 500
                else FILES_API)
        super().__init__(code, message, status=status)
        self.name = "DeepSeekFilesError"
        self.detail = detail


def is_files_quota_error(error: object) -> bool:
    """上传失败是否报告 provider 存储或文件数配额（上游 isFilesQuotaError）。"""
    return isinstance(error, DeepSeekFilesError) and bool(_QUOTA_DETAIL.search(error.detail))


class DeepSeekFileObject:
    """从任一 DeepSeek Files 协议归一化出的已校验文件元数据。"""

    id: DeepSeekFileId
    bytes: int
    createdAt: int
    filename: str
    purpose: str
    expiresAt: int | None

    def __init__(self, id: DeepSeekFileId, bytes: int, createdAt: int,
                 filename: str, purpose: str = "user_data",
                 expiresAt: int | None = None) -> None:
        self.id = id
        self.bytes = bytes
        self.createdAt = createdAt
        self.filename = filename
        self.purpose = purpose
        self.expiresAt = expiresAt


class DeepSeekFilePage:
    """`GET /files` 返回的一页。"""

    data: list
    firstId: DeepSeekFileId | None
    lastId: DeepSeekFileId | None
    hasMore: bool

    def __init__(self, data: list, hasMore: bool,
                 firstId: DeepSeekFileId | None = None,
                 lastId: DeepSeekFileId | None = None) -> None:
        self.data = data
        self.firstId = firstId
        self.lastId = lastId
        self.hasMore = hasMore


def _invalid_response(operation: str) -> LlmFailure:
    return LlmFailure(
        INVALID_RESPONSE,
        f"DeepSeek Files API returned an invalid {operation} response.")


def _is_safe_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def parse_file_object(value: Any, operation: str) -> DeepSeekFileObject:
    """严格归一化 Chat Completions 文件对象（上游 parseFileObject）。"""
    if not isinstance(value, dict):
        raise _invalid_response(operation)
    wire_id = value.get("id")
    if (not isinstance(wire_id, str) or len(wire_id) == 0
            or value.get("object") != "file"
            or not _is_safe_int(value.get("bytes"))
            or not _is_safe_int(value.get("created_at"))
            or not isinstance(value.get("filename"), str)
            or len(value["filename"]) == 0
            or value.get("purpose") != "user_data"
            or (value.get("expires_at") is not None
                and not _is_safe_int(value.get("expires_at")))):
        raise _invalid_response(operation)
    return DeepSeekFileObject(
        id=DeepSeekFileId(wire_id),
        bytes=value["bytes"],
        createdAt=value["created_at"],
        filename=value["filename"],
        purpose="user_data",
        expiresAt=value.get("expires_at"),
    )


def parse_messages_file(value: Any, operation: str) -> DeepSeekFileObject:
    """归一化 Messages wire 对象，不把省略的过期时间解释为永久（上游 parseMessagesFile）。"""
    if not isinstance(value, dict):
        raise _invalid_response(operation)
    created_at_raw = value.get("created_at")
    created_at = _parse_iso_seconds(created_at_raw) if isinstance(created_at_raw, str) else None
    if (value.get("type") != "file" or not isinstance(value.get("mime_type"), str)
            or created_at is None):
        raise _invalid_response(operation)
    return parse_file_object({
        "id": value.get("id"),
        "object": "file",
        "bytes": value.get("size_bytes"),
        "created_at": created_at,
        "filename": value.get("filename"),
        "purpose": "user_data",
    }, operation)


def _parse_iso_seconds(text: str) -> int | None:
    from datetime import datetime
    try:
        return int(datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def provider_error_detail(value: Any) -> dict:
    """提取 provider 错误字段用于分类（上游 providerErrorDetail）。"""
    if not isinstance(value, dict):
        return {"detail": ""}
    error = value.get("error")
    if not isinstance(error, dict):
        return {"detail": ""}
    message = error.get("message") if isinstance(error.get("message"), str) else None
    detail = " ".join(
        field for field in (error.get("code"), error.get("type"), error.get("message"))
        if isinstance(field, str))
    result: dict = {"detail": detail}
    if message is not None:
        result["message"] = message
    return result


class DeepSeekFilesClient:
    """直接 Files 客户端，保留配置的 URL 根并拒绝重定向以免凭据离开源。"""

    def __init__(self, *, baseURL: str, apiKey: str, protocol: str,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.baseURL = baseURL.rstrip("/")
        self.apiKey = apiKey
        self.protocol = protocol
        self._transport = transport
        self.path = "/v1/files" if protocol == "messages" else "/files"

    def _parse_file(self, value: Any, operation: str) -> DeepSeekFileObject:
        return (parse_messages_file(value, operation)
                if self.protocol == "messages" else parse_file_object(value, operation))

    def _headers(self) -> dict:
        if self.protocol == "messages":
            return {"x-api-key": self.apiKey, "anthropic-version": "2023-06-01",
                    "anthropic-beta": MESSAGES_FILES_BETA}
        return {"authorization": f"Bearer {self.apiKey}"}

    async def _request(self, method: str, path: str,
                       *, data=None, params=None, files=None, signal=None) -> httpx.Response:
        kwargs: dict = {"follow_redirects": False}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                response = await client.request(
                    method, f"{self.baseURL}{path}",
                    headers=self._headers(), data=data, params=params, files=files,
                )
        except httpx.HTTPError as error:
            if signal is not None and getattr(signal, "aborted", False):
                raise error from None
            raise LlmFailure(
                TRANSPORT,
                f"DeepSeek Files API request to {self.baseURL} failed",
                status=None) from error
        if response.is_success:
            return response
        parsed: Any = None
        try:
            parsed = response.json()
        except (ValueError, json.JSONDecodeError):
            # A status remains sufficient to report the provider failure.
            pass
        detail = provider_error_detail(parsed)
        message = detail.get("message") or f"DeepSeek Files API error (HTTP {response.status_code})"
        raise DeepSeekFilesError(message, response.status_code, detail["detail"])

    async def upload(self, *, data: bytes, mediaType: str, filename: str,
                     expiresAfterSeconds: int, signal=None) -> DeepSeekFileObject:
        """上传一张图片并带显式寿命（上游 upload）。"""
        if len(data) > MAX_FILE_UPLOAD_BYTES:
            raise LlmFailure(INVALID_REQUEST, "DeepSeek Files API upload exceeds 128 MiB.")
        if (not isinstance(expiresAfterSeconds, int) or isinstance(expiresAfterSeconds, bool)
                or expiresAfterSeconds < MIN_FILE_EXPIRY_SECONDS
                or expiresAfterSeconds > MAX_FILE_EXPIRY_SECONDS):
            raise LlmFailure(
                INVALID_REQUEST,
                "DeepSeek file expiry must be between 3600 and 2592000 seconds.")
        form: dict = {
            "expires_after[anchor]": "created_at",
            "expires_after[seconds]": str(expiresAfterSeconds),
        }
        if self.protocol == "chat-completions":
            form["purpose"] = "user_data"
        response = await self._request(
            "POST", self.path, data=form,
            files={"file": (filename, data, mediaType)}, signal=signal)
        file = self._parse_file(response.json(), "upload")
        if self.protocol == "messages":
            return DeepSeekFileObject(file.id, file.bytes, file.createdAt, file.filename,
                                      expiresAt=file.createdAt + expiresAfterSeconds)
        if file.expiresAt is None:
            raise _invalid_response("upload")
        return file

    async def list(self, *, after: DeepSeekFileId | None = None, limit: int | None = None,
                   order: str | None = None, signal=None) -> DeepSeekFilePage:
        """列出一页文件；排序仅适用于 Chat Completions（上游 list）。"""
        query: dict = {} if self.protocol == "messages" else {"purpose": "user_data"}
        if after is not None:
            query["after_id" if self.protocol == "messages" else "after"] = str(after)
        if limit is not None:
            query["limit"] = str(limit)
        if order is not None and self.protocol == "chat-completions":
            query["order"] = order
        response = await self._request("GET", self.path, params=query, signal=signal)
        value = response.json()
        if not isinstance(value, dict):
            raise _invalid_response("list")
        first_raw = value.get("first_id")
        last_raw = value.get("last_id")
        first_id = first_raw if self.protocol == "messages" else first_raw
        last_id = last_raw if self.protocol == "messages" else last_raw
        if ((self.protocol == "chat-completions" and value.get("object") != "list")
                or not isinstance(value.get("data"), list)
                or not isinstance(value.get("has_more"), bool)
                or (first_id is not None and not isinstance(first_id, str))
                or (last_id is not None and not isinstance(last_id, str))):
            raise _invalid_response("list")
        return DeepSeekFilePage(
            data=[self._parse_file(item, "list") for item in value["data"]],
            firstId=DeepSeekFileId(first_id) if isinstance(first_id, str) else None,
            lastId=DeepSeekFileId(last_id) if isinstance(last_id, str) else None,
            hasMore=value["has_more"],
        )

    async def retrieve(self, file_id: DeepSeekFileId, signal=None) -> DeepSeekFileObject:
        """取回一个文件对象（上游 retrieve）。"""
        response = await self._request(
            "GET", f"{self.path}/{_encode(file_id)}", signal=signal)
        return self._parse_file(response.json(), "retrieve")

    async def delete(self, file_id: DeepSeekFileId, signal=None) -> None:
        """删除一个 provider 文件（上游 delete）。"""
        response = await self._request(
            "DELETE", f"{self.path}/{_encode(file_id)}", signal=signal)
        value = response.json()
        if not isinstance(value, dict):
            raise _invalid_response("delete")
        if self.protocol == "messages":
            valid = value.get("id") == file_id and value.get("type") == "file_deleted"
        else:
            valid = (value.get("id") == file_id and value.get("object") == "file"
                     and value.get("deleted") is True)
        if not valid:
            raise _invalid_response("delete")


def _encode(file_id: str) -> str:
    from urllib.parse import quote
    return quote(str(file_id), safe="")
