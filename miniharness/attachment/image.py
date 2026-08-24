"""光栅探测：受理时完整解码，读取路径仅头部复验。

对应 dsh 真实源码：packages/attachment/attachment-local/src/image.ts（rc.2
已核实）。载体：sharp → Pillow（依赖政策 2026-08-23 修订：成熟开源库优先；
原 stdlib 头解析近似已退役——Pillow 能检出像素级损坏与 16-bit 深度，闭合
旧登记的语义缺口）。

上游语义（image.ts）：
  * detectImage 完整解码 → DetectedImage{mediaType, width, height, animated,
    carriesMetadata, depth, space, hasAlpha}；超 maxPixels 抛
    IMAGE_TOO_MANY_PIXELS、超单边 maxDimension 抛 IMAGE_DIMENSION_TOO_LARGE；
    畸形抛 INVALID_IMAGE；width/height 已应用 EXIF 定向（5-8 转置感知轴）；
  * probeImage 仅头部探测（摘要验证过的读取路径复用：受理已证明这些字节可
    完整解码，重放不再付全量栅格解码的逐请求像素放大）；
  * encodedAlphaIsCompatible：本包编码器产物中 WebP 允许省略全不透明 alpha
    平面，其余增删都不兼容。
"""
from __future__ import annotations

import io

from PIL import Image, ImageOps

from .error import (
    IMAGE_DIMENSION_TOO_LARGE,
    IMAGE_TOO_MANY_PIXELS,
    INVALID_IMAGE,
    AttachmentError,
)

__all__ = [
    "DetectedImage",
    "detect_image",
    "encoded_alpha_is_compatible",
    "probe_image",
]

_FORMAT_MEDIA_TYPES: dict[str, str] = {
    "PNG": "image/png",
    "JPEG": "image/jpeg",
    "WEBP": "image/webp",
    "GIF": "image/gif",
}

# Pillow mode → vips depth 近似（上游 sharp metadata.depth 词表）
_MODE_DEPTH: dict[str, str] = {
    "1": "uchar",
    "L": "uchar",
    "LA": "uchar",
    "P": "uchar",
    "PA": "uchar",
    "RGB": "uchar",
    "RGBA": "uchar",
    "RGBX": "uchar",
    "CMYK": "uchar",
    "YCbCr": "uchar",
    "HSV": "uchar",
    "I;16": "ushort",
    "I;16B": "ushort",
    "I;16L": "ushort",
    "I;16N": "ushort",
    "I": "int",
    "F": "float",
}

# Pillow mode → vips interpret/space 近似
_MODE_SPACE: dict[str, str] = {
    "RGB": "srgb",
    "RGBA": "srgb",
    "RGBX": "srgb",
    "P": "srgb",
    "PA": "srgb",
    "YCbCr": "srgb",
    "HSV": "srgb",
    "L": "b-w",
    "LA": "b-w",
    "1": "b-w",
    "I": "b-w",
    "F": "b-w",
    "CMYK": "cmyk",
}


class DetectedImage:
    """受支持图片的解码元数据（上游 DetectedImage）。"""

    __slots__ = ("media_type", "width", "height", "animated", "carries_metadata",
                 "depth", "space", "has_alpha")

    def __init__(
        self,
        media_type: str,
        width: int,
        height: int,
        animated: bool,
        carries_metadata: bool,
        depth: str,
        space: str,
        has_alpha: bool,
    ):
        self.media_type = media_type
        self.width = width
        self.height = height
        self.animated = animated
        self.carries_metadata = carries_metadata
        self.depth = depth
        self.space = space
        self.has_alpha = has_alpha


def _detected_from_image(image: Image.Image) -> DetectedImage:
    fmt = image.format if image.format is not None else ""
    media_type = _FORMAT_MEDIA_TYPES.get(fmt)
    if media_type is None:
        raise AttachmentError("Unsupported or malformed image data.", INVALID_IMAGE)
    # EXIF 定向 5-8 转置存储栅格：报告观看者感知的轴，让限制、来源事实与
    # 坐标建议共享同一坐标系（上游 transposed 同款）。
    orientation = 0
    try:
        exif = image.getexif()
        orientation = int(exif.get(274, 0) or 0)
    except Exception:
        orientation = 0
    transposed = 5 <= orientation <= 8
    stored_width, stored_height = image.size
    mode = image.mode
    info = image.info or {}
    carries_metadata = any(
        key in info for key in ("exif", "xmp", "icc_profile", "comment", "photoshop")
    ) or orientation != 0
    try:
        frames = getattr(image, "n_frames", 1)
    except Exception:
        frames = 1
    return DetectedImage(
        media_type=media_type,
        width=stored_height if transposed else stored_width,
        height=stored_width if transposed else stored_height,
        animated=frames > 1,
        carries_metadata=carries_metadata,
        depth=_MODE_DEPTH.get(mode, "uchar"),
        space=_MODE_SPACE.get(mode, mode.lower()),
        has_alpha=image.mode in ("RGBA", "LA", "PA") or (
            image.mode == "P" and "transparency" in info
        ),
    )


def _open(data: bytes, *, full_decode: bool = True) -> Image.Image:
    if len(data) == 0:
        raise AttachmentError("Unsupported or malformed image data.", INVALID_IMAGE)
    try:
        image = Image.open(io.BytesIO(data))
        if full_decode:
            image.load()
        return image
    except AttachmentError:
        raise
    except Exception as error:
        raise AttachmentError(
            "Unsupported or malformed image data.", INVALID_IMAGE
        ) from error


def detect_image(
    data: bytes,
    max_pixels: int | None = None,
    max_dimension: int | None = None,
) -> DetectedImage:
    """完整解码受支持光栅并返回固有元数据（上游 detectImage）。

    顺序与上游一致：先取头部事实，再校验固有尺寸限制，最后付全量解码。"""
    image = _open(data, full_decode=False)
    try:
        detected = _detected_from_image(image)
        if max_pixels is not None and detected.width * detected.height > max_pixels:
            raise AttachmentError(
                "Image exceeds the configured decoded-pixel limit.", IMAGE_TOO_MANY_PIXELS
            )
        if max_dimension is not None and max(detected.width, detected.height) > max_dimension:
            raise AttachmentError(
                "Image exceeds the configured per-side pixel limit.",
                IMAGE_DIMENSION_TOO_LARGE,
            )
        image.load()
        return detected
    except AttachmentError:
        raise
    except Exception as error:
        raise AttachmentError(
            "Unsupported or malformed image data.", INVALID_IMAGE
        ) from error


def probe_image(data: bytes) -> DetectedImage:
    """解析受支持光栅头部并返回元数据，不解码像素（上游 probeImage）。

    读取路径专用：受理已证明这些确切字节可完整解码，这里只重导引用字段。"""
    if len(data) == 0:
        raise AttachmentError("Unsupported or malformed image data.", INVALID_IMAGE)
    try:
        image = Image.open(io.BytesIO(data))
    except Exception as error:
        raise AttachmentError(
            "Unsupported or malformed image data.", INVALID_IMAGE
        ) from error
    try:
        return _detected_from_image(image)
    except AttachmentError:
        raise
    except Exception as error:
        raise AttachmentError(
            "Unsupported or malformed image data.", INVALID_IMAGE
        ) from error


def encoded_alpha_is_compatible(
    source_has_alpha: bool | None,
    output_media_type: str,
    output_has_alpha: bool,
) -> bool:
    """检查本包编码器产物的 alpha 元数据是否与源事实兼容（上游同名函数）。

    libvips/sharp 与 Pillow 的 WebP 编码器都可能省略全不透明的 alpha 平面：
    源带 alpha 而 WebP 输出无 alpha 是唯一允许的差异，其余增删皆不兼容。"""
    return (
        source_has_alpha is None
        or output_has_alpha == source_has_alpha
        or (source_has_alpha and not output_has_alpha and output_media_type == "image/webp")
    )
