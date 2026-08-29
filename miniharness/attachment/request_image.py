"""模型请求的确定性缓存图片版本。

对应 dsh 真实源码：packages/attachment/attachment-local/src/request-image.ts。
alpha.1 重构（2026-08-24，上游 commit 30704dc1df / 4863890535）：
  * 变换版本 request-image-v4 → request-image-v5；
  * DESC 编码块与规范化共享同一质量阶梯 [85,75,60]（webpEffort=0），路由
    改为按 alpha 分流 ['alpha:webp','opaque:jpeg']，废弃 rc.2 的低彩色调色板
    PNG 分类；
  * 尺寸投影 requestImageDimensions 抽到 seam 包（projection.py）；
  * 尺寸未变且字节已在预算内 → 原样直通；否则单一阶梯编码，每个阶梯质量都
    超字节目标时保留最小阶梯输出（不再抛 IMAGE_TOO_LARGE）。载体：sharp →
    Pillow。

上游语义：
  * variantId 是覆盖每个变换输入的完整确定身份（sha256 over descriptor），
    同时是缓存与上传索引键；
  * read_cached 回读复验（uchar / srgb / 尺寸不超投影 / alpha 兼容；不再按
    字节超限判失效——阶梯产物可合法大于字节目标），任何不符当作未命中重新生成。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import uuid
from dataclasses import dataclass

from PIL import Image

from .encoding import (
    IMAGE_ENCODING_QUALITIES,
    WEBP_ENCODING_EFFORT,
    EncodedCandidate,
    encode_first_within_limit,
    encoding_ladder,
    is_exhausted_encoding,
)
from .error import ATTACHMENT_WRITE_FAILED, INVALID_ATTACHMENT_REF, AttachmentError
from .image import (
    DetectedImage,
    detect_image,
    encoded_alpha_is_compatible,
    probe_image,
)
from .normalization import prepared_source, resized
from .projection import request_image_dimensions
from .types import (
    ImageAttachmentRef,
    ImageRequestPolicy,
    ImageVariantId,
    RequestImageAttachment,
    StoredImageAttachment,
)

__all__ = [
    "REQUEST_IMAGE_TRANSFORM_VERSION",
    "read_request_image_file",
    "request_image_variant_id",
    "validate_policy",
]

#: 每个缓存与上传索引身份都包含的变换版本
REQUEST_IMAGE_TRANSFORM_VERSION = "request-image-v5"


@dataclass(frozen=True)
class _EncodedRequestImage:
    data: bytes
    mediaType: str
    width: int
    height: int


@dataclass(frozen=True)
class _VerifiedRequestImage(_EncodedRequestImage):
    hasAlpha: bool


def _digest(value: bytes | str) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def _checked_integer(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AttachmentError(f"{name} must be a positive integer.", INVALID_ATTACHMENT_REF)
    return value


def validate_policy(policy: ImageRequestPolicy) -> None:
    """路由策略准入：正整数像素与字节预算（上游 validatePolicy）。"""
    _checked_integer(policy.maxPixels, "Image request maxPixels")
    _checked_integer(policy.maxBytes, "Image request maxBytes")


def _descriptor(attachment: ImageAttachmentRef, policy: ImageRequestPolicy) -> str:
    # JSON.stringify 无空格形态；键序与上游逐字一致（dict 保序）
    return json.dumps(
        {
            "transformVersion": REQUEST_IMAGE_TRANSFORM_VERSION,
            "attachmentId": str(attachment.attachmentId),
            "routePixelBudget": policy.maxPixels,
            "encodedByteBudget": policy.maxBytes,
            "encoding": {
                "webpQualities": list(IMAGE_ENCODING_QUALITIES),
                "webpEffort": WEBP_ENCODING_EFFORT,
                "jpegQualities": list(IMAGE_ENCODING_QUALITIES),
                "order": ["alpha:webp", "opaque:jpeg"],
                "colourspace": "srgb",
            },
        },
        separators=(",", ":"),
    )


def request_image_variant_id(
    attachment: ImageAttachmentRef,
    policy: ImageRequestPolicy,
) -> ImageVariantId:
    """一个附件与路由自有请求策略的完整确定身份（上游同名函数）。"""
    return ImageVariantId(f"sha256:{_digest(_descriptor(attachment, policy))}")


def _create_request_image(
    stored: StoredImageAttachment,
    policy: ImageRequestPolicy,
    has_alpha: bool,
) -> _EncodedRequestImage:
    dimensions = request_image_dimensions(stored.ref.width, stored.ref.height, policy.maxPixels)
    if (
        dimensions == (stored.ref.width, stored.ref.height)
        and len(stored.data) <= policy.maxBytes
    ):
        return _EncodedRequestImage(
            data=stored.data,
            mediaType=stored.ref.mediaType,
            width=stored.ref.width,
            height=stored.ref.height,
        )
    source = prepared_source(stored.data, has_alpha)
    prepared = resized(source, dimensions[0], dimensions[1])
    encoded = encode_first_within_limit(
        encoding_ladder(prepared, has_alpha), policy.maxBytes
    )
    chosen: EncodedCandidate = (
        encoded.smallest if is_exhausted_encoding(encoded) else encoded
    )
    return _EncodedRequestImage(
        data=chosen.data,
        mediaType=chosen.mediaType,
        width=chosen.width,
        height=chosen.height,
    )


def _cache_path(root: str, hash_hex: str) -> str:
    return os.path.join(root, "request-images", hash_hex[:2], hash_hex)


def read_cached(
    path: str,
    stored: StoredImageAttachment,
    policy: ImageRequestPolicy,
    expected_alpha: bool,
) -> _VerifiedRequestImage | None:
    """读回缓存版本并复验；任何不符（含缺失）都视为未命中。"""
    try:
        with open(path, "rb") as handle:
            data = handle.read()
        detected = probe_image(data)
        maximum = request_image_dimensions(stored.ref.width, stored.ref.height, policy.maxPixels)
        if (
            detected.depth != "uchar"
            or detected.space != "srgb"
            or detected.width > maximum[0]
            or detected.height > maximum[1]
            or not encoded_alpha_is_compatible(expected_alpha, detected.media_type, detected.has_alpha)
        ):
            return None
        return _VerifiedRequestImage(
            data=data,
            mediaType=detected.media_type,
            width=detected.width,
            height=detected.height,
            hasAlpha=detected.has_alpha,
        )
    except (OSError, AttachmentError):
        return None


def verify_request_image(
    image: _EncodedRequestImage,
    expected_alpha: bool,
) -> _VerifiedRequestImage:
    """新编码产物全量解码验证 8-bit sRGB 元数据一致（上游同名函数）。"""
    detected = detect_image(image.data)
    if (
        detected.depth != "uchar"
        or detected.space != "srgb"
        or detected.width != image.width
        or detected.height != image.height
        or detected.media_type != image.mediaType
        or not encoded_alpha_is_compatible(expected_alpha, detected.media_type, detected.has_alpha)
    ):
        raise AttachmentError(
            "Encoded model-request image does not match its verified 8-bit sRGB metadata.",
            ATTACHMENT_WRITE_FAILED,
        )
    return _VerifiedRequestImage(
        data=image.data,
        mediaType=image.mediaType,
        width=image.width,
        height=image.height,
        hasAlpha=detected.has_alpha,
    )


def write_cached(path: str, data: bytes) -> None:
    """原子写缓存条目（目录 0700、临时文件后改名）。"""
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    temporary = f"{path}.{uuid.uuid4()}.tmp"
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        fd = os.open(temporary, flags, 0o600)
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(data)
        except BaseException:
            raise
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_request_image_file(
    root: str,
    stored: StoredImageAttachment,
    policy: ImageRequestPolicy,
) -> RequestImageAttachment:
    """生成或复用一个存储根下的请求图（上游同名函数，同步载体）。"""
    validate_policy(policy)
    source_facts: DetectedImage = probe_image(stored.data)
    variant_id = request_image_variant_id(stored.ref, policy)
    hash_hex = str(variant_id)[len("sha256:"):]
    path = _cache_path(root, hash_hex)
    cached = read_cached(path, stored, policy, source_facts.has_alpha)
    if cached is not None:
        created: _EncodedRequestImage | _VerifiedRequestImage = cached
    else:
        created = _create_request_image(stored, policy, source_facts.has_alpha)
    if cached is not None:
        version: _VerifiedRequestImage = cached
    elif created.data == stored.data:
        version = _VerifiedRequestImage(
            data=created.data,
            mediaType=created.mediaType,
            width=created.width,
            height=created.height,
            hasAlpha=source_facts.has_alpha,
        )
    else:
        version = verify_request_image(created, source_facts.has_alpha)
    if cached is None and version.data != stored.data:
        write_cached(path, version.data)
    return RequestImageAttachment(
        variantId=variant_id,
        attachment=stored.ref,
        data=version.data,
        mediaType=version.mediaType,
        bytes=len(version.data),
        width=version.width,
        height=version.height,
        depth="uchar",
        space="srgb",
        hasAlpha=version.hasAlpha,
    )
