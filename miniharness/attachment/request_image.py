"""模型请求的确定性缓存图片版本。

对应 dsh 真实源码：packages/attachment/attachment-local/src/request-image.ts
（rc.2 新增，已核实）。载体：sharp → Pillow。

上游语义：
  * variantId 是覆盖每个变换输入的完整确定身份（sha256 over descriptor：
    transformVersion + attachmentId + 路由像素/字节预算 + 固定编码器参数），
    同时是缓存与上传索引键；
  * 尺寸投影是纵横保持的整数内收算法，硬性总像素预算下小图不放大；
  * 源尺寸未变且字节已在预算内 → 原样直通；否则与规范化同款的低彩色/
    alpha 分流编码阶梯 + 缩边循环；
  * 产物按 variantId 缓存在存储根下，读回时复验（字节预算 / uchar / srgb /
    尺寸不超投影 / alpha 兼容），任何不符都当作未命中重新生成。
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import uuid
from dataclasses import dataclass
from typing import Callable

from PIL import Image

from .encoding import encode_first_within_limit, is_exhausted_encoding
from .error import ATTACHMENT_WRITE_FAILED, IMAGE_TOO_LARGE, INVALID_ATTACHMENT_REF, AttachmentError
from .image import (
    DetectedImage,
    detect_image,
    encoded_alpha_is_compatible,
    probe_image,
)
from .normalization import TypedCandidate, has_low_colour_count
from .types import (
    ImageAttachmentRef,
    ImageRequestPolicy,
    ImageVariantId,
    RequestImageAttachment,
    StoredImageAttachment,
)

__all__ = [
    "REQUEST_IMAGE_QUALITIES",
    "REQUEST_IMAGE_TRANSFORM_VERSION",
    "read_request_image_file",
    "request_image_dimensions",
    "request_image_variant_id",
]

#: 每个缓存与上传索引身份都包含的变换版本
REQUEST_IMAGE_TRANSFORM_VERSION = "request-image-v4"
#: DeepSeek 请求版本通常在这两个偏好质量内放得下
REQUEST_IMAGE_QUALITIES: tuple[int, ...] = (85, 80)


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


def request_image_dimensions(
    width: int,
    height: int,
    max_pixels: int,
) -> tuple[int, int]:
    """硬性总像素预算内的纵横保持整数尺寸（上游同名纯函数）。

    内收取整；小图不放大。"""
    scale = min(1.0, (max_pixels / (width * height)) ** 0.5)
    if scale == 1:
        return width, height
    if width >= height:
        projected_width = max(1, int(width * scale))
        projected_height = max(1, round(projected_width * height / width))
        while projected_width * projected_height > max_pixels and projected_width > 1:
            projected_width -= 1
            projected_height = max(1, round(projected_width * height / width))
        return projected_width, projected_height
    projected_height = max(1, int(height * scale))
    projected_width = max(1, round(projected_height * width / height))
    while projected_width * projected_height > max_pixels and projected_height > 1:
        projected_height -= 1
        projected_width = max(1, round(projected_height * width / height))
    return projected_width, projected_height


def _checked_integer(value: int, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise AttachmentError(f"{name} must be a positive integer.", INVALID_ATTACHMENT_REF)
    return value


def validate_policy(policy: ImageRequestPolicy) -> None:
    """路由策略准入：正整数像素与字节预算（上游 validatePolicy）。"""
    _checked_integer(policy.maxPixels, "Image request maxPixels")
    _checked_integer(policy.maxBytes, "Image request maxBytes")


def _descriptor(attachment: ImageAttachmentRef, policy: ImageRequestPolicy) -> str:
    # JSON.stringify 无空格形态；字段结构与上游逐字一致
    return json.dumps(
        {
            "transformVersion": REQUEST_IMAGE_TRANSFORM_VERSION,
            "attachmentId": str(attachment.attachmentId),
            "routePixelBudget": policy.maxPixels,
            "encodedByteBudget": policy.maxBytes,
            "encoding": {
                "png": {"compressionLevel": 9, "palette": "opaque-only"},
                "webpQualities": list(REQUEST_IMAGE_QUALITIES),
                "jpegQualities": list(REQUEST_IMAGE_QUALITIES),
                "order": ["low-colour:png-webp", "alpha:webp", "opaque:jpeg"],
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


def _prepared_source(stored: StoredImageAttachment) -> Image.Image:
    has_alpha = probe_image(stored.data).has_alpha
    image = Image.open(io.BytesIO(stored.data))
    image.load()
    return image.convert("RGBA" if has_alpha else "RGB")


def _resized(source: Image.Image, width: int, height: int) -> Image.Image:
    scale = min(1.0, width / source.width, height / source.height)
    target = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
    return source.resize(target, Image.Resampling.LANCZOS)


def _encode(image: Image.Image, media_type: str, quality: int | None = None,
            palette: bool = True) -> _EncodedRequestImage:
    buffer = io.BytesIO()
    if media_type == "image/png":
        payload = image.quantize(colors=256) if palette and image.mode != "P" else image
        payload.save(buffer, format="PNG", optimize=True)
    elif media_type == "image/webp":
        image.save(buffer, format="WEBP", quality=quality or 80)
    else:
        image.convert("RGB").save(buffer, format="JPEG", quality=quality or 80)
    with Image.open(io.BytesIO(buffer.getvalue())) as out:
        width, height = out.size
    return _EncodedRequestImage(data=buffer.getvalue(), mediaType=media_type, width=width, height=height)


def _encoding_attempts(
    stored: StoredImageAttachment,
    source: Image.Image,
    width: int,
    height: int,
    has_alpha: bool,
    low_colour: bool,
) -> list[Callable[[], TypedCandidate]]:
    prepared = _resized(source, width, height)
    webp = [
        (lambda q=q: _attempt_webp(prepared, q)) for q in REQUEST_IMAGE_QUALITIES
    ]
    if low_colour:
        return [(lambda: _encode(prepared.copy(), "image/png", palette=not has_alpha))] + webp
    if has_alpha:
        return list(webp)
    return [(lambda q=q: _encode(prepared.copy(), "image/jpeg", q)) for q in REQUEST_IMAGE_QUALITIES]


def _attempt_webp(image: Image.Image, quality: int) -> TypedCandidate:
    encoded = _encode(image.copy(), "image/webp", quality)
    return TypedCandidate(data=encoded.data, mediaType="image/webp")


def _create_request_image(
    stored: StoredImageAttachment,
    policy: ImageRequestPolicy,
    source: Image.Image,
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
    low_colour = has_low_colour_count(source)
    width, height = dimensions
    while True:
        attempts = _encoding_attempts(stored, source, width, height, has_alpha, low_colour)
        encoded_version = encode_first_within_limit(attempts, policy.maxBytes)
        if not is_exhausted_encoding(encoded_version):
            return _EncodedRequestImage(
                data=encoded_version.data,
                mediaType=encoded_version.mediaType,
                width=width,
                height=height,
            )
        if width == 1 and height == 1:
            break
        scale = min(0.9, (policy.maxBytes / len(encoded_version.smallest.data)) ** 0.5 * 0.95)
        width = max(1, int(width * scale))
        height = max(1, int(height * scale))
    raise AttachmentError(
        "Image cannot be encoded within the model-request byte budget.",
        IMAGE_TOO_LARGE,
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
            len(data) > policy.maxBytes
            or detected.depth != "uchar"
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
        source_image = None
    else:
        source_image = _prepared_source(stored)
        created = _create_request_image(stored, policy, source_image, source_facts.has_alpha)
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
