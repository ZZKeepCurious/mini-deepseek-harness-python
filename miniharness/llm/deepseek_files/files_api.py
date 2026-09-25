"""DeepSeek Files API 传输（Messages 协议端点）。

对应 dsh 真实源码：packages/llm/llm-deepseek/src/common/files-api.ts。

上游 llm-deepseek 自 dsh-v0.1.7-rc.1 起只保留 Anthropic 兼容 Messages；Files
资源挂在 ``messagesApiRoot(baseURL)`` 下（``/v1/files``），头为 ``x-api-key`` +
``anthropic-version: 2023-06-01`` + ``anthropic-beta: files-api-2025-04-14``，
列表游标为 ``after_id``（无升序查询），时间戳为 ISO 字符串，删除回执
``type == "file_deleted"``。

载体差异：上游以 Web `fetch`/`FormData`/`Blob` 实现；mini 用 httpx（异步）。
`redirect: 'error'` 语义以 httpx 缺省不跟随重定向承载——凭据不会离开配置源。
"""
from __future__ import annotations

import json
import re
from typing import Any

import httpx

from ..deepseek_messages import MESSAGES_FILES_BETA, messages_api_root
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
    """从 Messages Files 协议归一化出的已校验文件元数据。"""

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
    """严格归一化已解码的 Messages 文件对象（上游 parseFileObject）。"""
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
    """归一化 Messages wire 对象，不把省略的过期时间解释为永久（上游 parseFileObject）。"""
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

    def __init__(self, *, baseURL: str, apiKey: str, accountCredential: bool = False,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.baseURL = messages_api_root(baseURL)
        self.apiKey = apiKey
        self.accountCredential = accountCredential
        self._transport = transport

    def _parse_file(self, value: Any, operation: str) -> DeepSeekFileObject:
        return parse_messages_file(value, operation)

    def _headers(self) -> dict:
        return {(("x-dsh-auth-token" if self.accountCredential else "x-api-key")): self.apiKey,
                "anthropic-version": "2023-06-01",
                "anthropic-beta": MESSAGES_FILES_BETA}

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
        """上传一张图片并带显式寿命（上游 upload）。

        Messages 不上报过期时间；deadline 取上传创建时间加请求寿命。
        """
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
        response = await self._request(
            "POST", "/files", data=form,
            files={"file": (filename, data, mediaType)}, signal=signal)
        file = self._parse_file(response.json(), "upload")
        return DeepSeekFileObject(file.id, file.bytes, file.createdAt, file.filename,
                                  expiresAt=file.createdAt + expiresAfterSeconds)

    async def list(self, *, after: DeepSeekFileId | None = None,
                   limit: int | None = None, signal=None) -> DeepSeekFilePage:
        """列出一页文件（Messages 无升序查询，游标为 after_id）。"""
        query: dict = {}
        if after is not None:
            query["after_id"] = str(after)
        if limit is not None:
            query["limit"] = str(limit)
        response = await self._request("GET", "/files", params=query, signal=signal)
        value = response.json()
        if not isinstance(value, dict):
            raise _invalid_response("list")
        first_raw = value.get("first_id")
        last_raw = value.get("last_id")
        if (not isinstance(value.get("data"), list)
                or not isinstance(value.get("has_more"), bool)
                or (first_raw is not None and not isinstance(first_raw, str))
                or (last_raw is not None and not isinstance(last_raw, str))):
            raise _invalid_response("list")
        return DeepSeekFilePage(
            data=[self._parse_file(item, "list") for item in value["data"]],
            firstId=DeepSeekFileId(first_raw) if isinstance(first_raw, str) else None,
            lastId=DeepSeekFileId(last_raw) if isinstance(last_raw, str) else None,
            hasMore=value["has_more"],
        )

    async def retrieve(self, file_id: DeepSeekFileId, signal=None) -> DeepSeekFileObject:
        """取回一个文件对象（上游 retrieve）。"""
        response = await self._request(
            "GET", f"/files/{_encode(file_id)}", signal=signal)
        return self._parse_file(response.json(), "retrieve")

    async def delete(self, file_id: DeepSeekFileId, signal=None) -> None:
        """删除一个 provider 文件（上游 delete）。"""
        response = await self._request(
            "DELETE", f"/files/{_encode(file_id)}", signal=signal)
        value = response.json()
        if not isinstance(value, dict):
            raise _invalid_response("delete")
        if value.get("id") != file_id or value.get("type") != "file_deleted":
            raise _invalid_response("delete")


def _encode(file_id: str) -> str:
    from urllib.parse import quote
    return quote(str(file_id), safe="")
