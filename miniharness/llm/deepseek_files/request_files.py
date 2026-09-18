"""共享 Files 解析、有界 stale-id 恢复与规范化图片诊断。

对应 dsh 真实源码：packages/llm/llm-deepseek/src/common/request-files.ts。
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from .file_id import DeepSeekFileId

__all__ = [
    "FileResolutionFailure",
    "ImageWireLocation",
    "RequestFiles",
    "detail_names_file_id",
    "normalized_image_diagnostic",
    "normalized_image_facts",
    "provider_rejected_file_id",
    "provider_rejected_normalized_image",
    "stale_mappings",
]

_REASON_BEFORE_IMAGE = re.compile(
    r"(?:unsupported|invalid|cannot read|failed to (?:decode|process)).{0,40}image",
    re.IGNORECASE)
_IMAGE_BEFORE_REASON = re.compile(
    r"image.{0,40}(?:unsupported|invalid|cannot be decoded)", re.IGNORECASE)
_FILE_TOKEN = re.compile(
    r"\bfile(?:[_ -]?(?:id|api|not[_ -]?found|deleted|expired))?", re.IGNORECASE)
_FILE_MISSING = re.compile(
    r"(?:expired|not[_ -]?found|deleted|do(?:es)? not exist|not created under (?:this|your) account)",
    re.IGNORECASE)
_FILE_INVALID_ID = re.compile(
    r"(?:invalid.{0,20}file[_ -]?(?:id|api)|file[_ -]?(?:id|api).{0,20}invalid)",
    re.IGNORECASE)
_BOUNDARY = re.compile(r"[\w-]", re.UNICODE)


@dataclass(frozen=True)
class ImageWireLocation:
    """一个图片 occurrence 在请求会话消息中的位置。"""

    message: int
    image: int


class FileResolutionFailure(Exception):
    """一次可整请求回退内联的 file 上传失败（上游 FileResolutionFailure）。"""

    def __init__(self, cause: object) -> None:
        super().__init__("DeepSeek Files API could not resolve a request image.")
        self.name = "FileResolutionFailure"
        self.cause = cause


@dataclass
class _UsedRequestFile:
    version: object
    fileId: DeepSeekFileId
    location: ImageWireLocation


def provider_rejected_normalized_image(detail: str) -> bool:
    """provider 错误是否点名规范化图片（上游 providerRejectedNormalizedImage）。"""
    return bool(_REASON_BEFORE_IMAGE.search(detail)
                or _IMAGE_BEFORE_REASON.search(detail))


def provider_rejected_file_id(detail: str) -> bool:
    """provider 错误是否指出 file id 过期/不存在/非法（上游 providerRejectedFileId）。"""
    file = _FILE_TOKEN.search(detail)
    missing = _FILE_MISSING.search(detail)
    invalid_id = _FILE_INVALID_ID.search(detail)
    return bool(file and (missing or invalid_id))


def detail_names_file_id(detail: str, file_id: DeepSeekFileId) -> bool:
    """detail 是否以词边界提及该 file id（上游 detailNamesFileId）。"""
    text = str(file_id)
    start = detail.find(text)
    while start >= 0:
        before = detail[start - 1] if start > 0 else None
        end = start + len(text)
        after = detail[end] if end < len(detail) else None
        if ((before is None or not _BOUNDARY.match(before))
                and (after is None or not _BOUNDARY.match(after))):
            return True
        start = detail.find(text, start + 1)
    return False


def stale_mappings(files: list, detail: str) -> list:
    """分类一次 stale-id 响应涉及的映射（上游 staleMappings）。"""
    unique = list({
        f"{f.version.variantId}\0{f.fileId}": f for f in files
    }.values())
    exact = [f for f in unique if detail_names_file_id(detail, f.fileId)]
    return exact if exact else unique


def normalized_image_facts(file) -> str:
    """一个规范化图片诊断事实块（上游 normalizedImageFacts）。"""
    version = file.version
    attachment = version.attachment
    name = attachment.name if attachment.name is not None else attachment.attachmentId
    colour = "sRGBA" if version.hasAlpha else "sRGB"
    return (f"\"{name}\" at message {file.location.message}, image {file.location.image} "
            f"({version.mediaType}, 8-bit {colour}, {version.width}x{version.height})")


def normalized_image_diagnostic(files: list, provider_message: str,
                                provider_detail: str) -> str:
    """把一次规范化图片拒绝归因到实际上传的图片 occurrence（上游 normalizedImageDiagnostic）。"""
    exact = next((f for f in files if detail_names_file_id(provider_detail, f.fileId)), None)
    target = exact if exact is not None else (files[0] if len(files) == 1 else None)
    if target is not None:
        return (f"DeepSeek rejected normalized image {normalized_image_facts(target)}: "
                f"{provider_message}. The provider rejected bytes already normalized by the "
                "harness; PNG, JPEG, WebP, and GIF remain supported input formats.")
    candidates = list({
        f"{f.version.variantId}\0{f.location.message}\0{f.location.image}": f
        for f in files
    }.values())
    return (f"DeepSeek rejected a normalized request image: {provider_message}. "
            "Candidate images: "
            + "; ".join(normalized_image_facts(f) for f in candidates)
            + ". The provider rejected bytes already normalized by the harness; "
            "PNG, JPEG, WebP, and GIF remain supported input formats.")


class RequestFiles:
    """一次模型请求拥有的 Files 状态，含至多一次 stale-id 重试。"""

    def __init__(self, files, connection, policy, timeout_ms: int,
                 signal, activity) -> None:
        self._files = files
        self._connection = connection
        self._policy = policy
        self._timeout_ms = timeout_ms
        self._signal = signal
        self._activity = activity
        self._used: list[_UsedRequestFile] = []
        self._retried = False

    def begin_attempt(self) -> None:
        """序列化下一次 HTTP attempt 前重置 occurrence 跟踪（上游 beginAttempt）。"""
        self._used = []

    async def resolve(self, version, location: ImageWireLocation) -> DeepSeekFileId:
        """在一张图片自己的上传截止时间内解析它（上游 resolve）。"""
        try:
            resolved = await self._files.ensure_uploaded(
                version, self._connection, self._policy, self._signal)
        except Exception as error:  # noqa: BLE001 - 分类后原样重抛
            if self._signal is not None and getattr(self._signal, "aborted", False):
                raise
            raise FileResolutionFailure(error) from error
        self._activity()
        self._used.append(_UsedRequestFile(version, resolved.record.fileId, location))
        return resolved.record.fileId

    async def retry(self, detail: str) -> bool:
        """使被拒绝的映射失效；仅第一次 stale-id 响应允许再次请求（上游 retry）。"""
        if len(self._used) == 0 or not provider_rejected_file_id(detail):
            return False
        for file in stale_mappings(self._used, detail):
            await self._files.invalidate(file.version, file.fileId, self._connection)
        if self._retried:
            return False
        self._retried = True
        return True

    def error_message(self, status: int, message: str, detail: str) -> str:
        """把一次规范化图片拒绝归因到实际 occurrence（上游 errorMessage）。"""
        return (normalized_image_diagnostic(self._used, message, detail)
                if status == 400 and len(self._used) > 0
                and provider_rejected_normalized_image(detail)
                else message)
