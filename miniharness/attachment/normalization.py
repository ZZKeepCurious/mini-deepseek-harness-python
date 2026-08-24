"""确定性的 provider 无关图片规范化。

对应 dsh 真实源码：packages/attachment/attachment-local/src/normalization.ts
（rc.2 新增，已核实）。载体：sharp/libvips 编码管线 → Pillow（依赖政策
2026-08-23 修订）。

上游语义：
  * 源已是干净单帧 8-bit sRGB/sRGBA 且在两个规范化限额内 → 字节原样直通；
  * 否则重编码：固定质量阶梯 [85,80,75]，按采样色彩复杂度与 alpha 分流——
    低彩色优先调色板 PNG（不透明时），带 alpha 走 WebP，不透明走 JPEG；
    质量地板到顶后继续按比例缩边直到独立字节上限成立；
  * 重编码永不移除透明度（WebP 允许省略全不透明平面，见
    encoded_alpha_is_compatible）；产物回读验证事实一致后才发布。
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Callable

from PIL import Image, ImageOps

from .encoding import EncodedCandidate, encode_first_within_limit, is_exhausted_encoding
from .error import ATTACHMENT_WRITE_FAILED, IMAGE_TOO_LARGE, AttachmentError
from .image import DetectedImage, detect_image, encoded_alpha_is_compatible

__all__ = [
    "NormalizedImage",
    "NormalizationPolicy",
    "can_pass_through_normalization",
    "has_low_colour_count",
    "normalize_image",
]

NORMALIZATION_QUALITIES: tuple[int, ...] = (85, 80, 75)
_LOW_COLOUR_SAMPLE_EDGE = 128
_LOW_COLOUR_LIMIT = 256
_MIN_SCALE_STEP = 0.9


@dataclass(frozen=True)
class NormalizationPolicy:
    """持久化规范化附件的部署解析策略。"""

    maxDimension: int = 2048
    maxBytes: int = 4 * 1024 * 1024


@dataclass(frozen=True)
class NormalizedImage:
    """规范化字节与其持久引用所记录的事实。"""

    data: bytes
    mediaType: str
    width: int
    height: int


@dataclass(frozen=True)
class TypedCandidate(EncodedCandidate):
    """携带产出媒体类型的编码候选（编码工厂共享，省去字节签名嗅探）。"""

    mediaType: str


def can_pass_through_normalization(
    detected: DetectedImage,
    byte_length: int,
    policy: NormalizationPolicy,
) -> bool:
    """字节是否已满足规范化要求（可字节原样直通）。"""
    return (
        detected.media_type != "image/gif"
        and not detected.animated
        and not detected.carries_metadata
        and detected.depth == "uchar"
        and detected.space == "srgb"
        and byte_length <= policy.maxBytes
        and max(detected.width, detected.height) <= policy.maxDimension
    )


def _prepared_source(data: bytes, has_alpha: bool) -> Image.Image:
    """提交字节 → 固定尺寸前的定向 sRGB 源（EXIF 定向 + 色彩空间归一）。"""
    image = Image.open(io.BytesIO(data))
    image.load()
    oriented = ImageOps.exif_transpose(image)
    if oriented is None:
        oriented = image
    return oriented.convert("RGBA" if has_alpha else "RGB")


def _resized(source: Image.Image, width: int, height: int) -> Image.Image:
    """长边封顶、纵横不变的目标尺寸（不放大；上游 resize inside 同款）。"""
    scale = min(1.0, width / source.width, height / source.height)
    target = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
    return source.resize(target, Image.Resampling.LANCZOS)


def _encode_png(image: Image.Image, palette: bool) -> TypedCandidate:
    payload = image.quantize(colors=256) if palette and image.mode != "P" else image
    buffer = io.BytesIO()
    payload.save(buffer, format="PNG", optimize=True)
    return TypedCandidate(data=buffer.getvalue(), mediaType="image/png")


def _encode_webp(image: Image.Image, quality: int) -> TypedCandidate:
    buffer = io.BytesIO()
    image.save(buffer, format="WEBP", quality=quality)
    return TypedCandidate(data=buffer.getvalue(), mediaType="image/webp")


def _encode_jpeg(image: Image.Image, quality: int) -> TypedCandidate:
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=quality)
    return TypedCandidate(data=buffer.getvalue(), mediaType="image/jpeg")


def has_low_colour_count(source: Image.Image) -> bool:
    """有界像素采样分类，不假设 PNG 源就是截图（上游同名函数）。

    最近邻采样缩进 128×128 以内，5-bit/通道 + 5-bit alpha 量化去重，
    超过 256 种即判高彩色提前退出。"""
    scale = min(
        1.0,
        _LOW_COLOUR_SAMPLE_EDGE / max(1, source.width),
        _LOW_COLOUR_SAMPLE_EDGE / max(1, source.height),
    )
    target = (
        max(1, round(source.width * scale)),
        max(1, round(source.height * scale)),
    )
    sampled = source.convert("RGBA").resize(target, Image.Resampling.NEAREST)
    colours: set[int] = set()
    pixels = sampled.tobytes()
    for offset in range(0, len(pixels), 4):
        red = pixels[offset]
        green = pixels[offset + 1]
        blue = pixels[offset + 2]
        alpha = pixels[offset + 3]
        colours.add(((red >> 3) << 15) | ((green >> 3) << 10) | ((blue >> 3) << 5) | (alpha >> 3))
        if len(colours) > _LOW_COLOUR_LIMIT:
            return False
    return True


def _encoding_attempts_at_size(
    source: Image.Image,
    has_alpha: bool,
    low_colour: bool,
) -> list[Callable[[], TypedCandidate]]:
    webp = [_encode_webp_factory(source, quality) for quality in NORMALIZATION_QUALITIES]
    if low_colour:
        return [_encode_png_factory(source, not has_alpha)] + webp
    if has_alpha:
        return webp
    return [_encode_jpeg_factory(source, quality) for quality in NORMALIZATION_QUALITIES]


def _encode_png_factory(image: Image.Image, palette: bool) -> Callable[[], EncodedCandidate]:
    def attempt() -> EncodedCandidate:
        return _encode_png(image.copy(), palette)
    return attempt


def _encode_webp_factory(image: Image.Image, quality: int) -> Callable[[], EncodedCandidate]:
    def attempt() -> EncodedCandidate:
        return _encode_webp(image.copy(), quality)
    return attempt


def _encode_jpeg_factory(image: Image.Image, quality: int) -> Callable[[], EncodedCandidate]:
    def attempt() -> EncodedCandidate:
        return _encode_jpeg(image.copy(), quality)
    return attempt


def _verify_normalized_image(
    image: NormalizedImage,
    expected_alpha: bool | None,
) -> NormalizedImage:
    """断言规范化产物是事实一致的单帧 8-bit sRGB 图片。"""
    detected = detect_image(image.data)
    if (
        detected.media_type != image.mediaType
        or detected.width != image.width
        or detected.height != image.height
        or detected.animated
        or detected.carries_metadata
        or detected.depth != "uchar"
        or detected.space != "srgb"
        or not encoded_alpha_is_compatible(expected_alpha, detected.media_type, detected.has_alpha)
    ):
        raise AttachmentError(
            "Image normalization did not produce a single-frame 8-bit sRGB image with matching metadata.",
            ATTACHMENT_WRITE_FAILED,
        )
    return image


def _initial_dimensions(detected: DetectedImage, max_dimension: int) -> tuple[int, int]:
    scale = min(1.0, max_dimension / max(detected.width, detected.height))
    return (
        max(1, round(detected.width * scale)),
        max(1, round(detected.height * scale)),
    )


def normalize_image(
    data: bytes,
    detected: DetectedImage,
    policy: NormalizationPolicy,
) -> NormalizedImage:
    """产出一个完整解码源的持久 provider 无关规范化版本（上游同名函数）。"""
    if can_pass_through_normalization(detected, len(data), policy):
        return NormalizedImage(
            data=data, mediaType=detected.media_type,
            width=detected.width, height=detected.height,
        )
    try:
        width, height = _initial_dimensions(detected, policy.maxDimension)
        prepared = _prepared_source(data, detected.has_alpha)
        low_colour = has_low_colour_count(prepared)
        while True:
            attempts = _encoding_attempts_at_size(
                _resized(prepared, width, height), detected.has_alpha, low_colour,
            )
            encoded = encode_first_within_limit(attempts, policy.maxBytes)
            if not is_exhausted_encoding(encoded):
                return _verify_normalized_image(
                    NormalizedImage(
                        data=encoded.data,
                        mediaType=encoded.mediaType,
                        width=width,
                        height=height,
                    ),
                    None if detected.media_type == "image/gif" else detected.has_alpha,
                )
            if width == 1 and height == 1:
                break
            size_scale = (policy.maxBytes / len(encoded.smallest.data)) ** 0.5 * 0.95
            scale = min(_MIN_SCALE_STEP, size_scale)
            width = max(1, int(width * scale))
            height = max(1, int(height * scale))
    except AttachmentError:
        raise
    except Exception as error:
        if detected.media_type == "image/png" and detected.depth != "uchar":
            source = f"{'16-bit' if detected.depth == 'ushort' else detected.depth} PNG"
        else:
            source = f"{detected.depth} {detected.media_type[len('image/'):].upper()}"
        raise AttachmentError(
            f"The {source} could not be converted to the normalized 8-bit sRGB form.",
            ATTACHMENT_WRITE_FAILED,
        ) from error
    raise AttachmentError(
        "Image cannot be encoded within the configured normalized-image byte cap.",
        IMAGE_TOO_LARGE,
    )
