"""确定性的 provider 无关图片规范化。

对应 dsh 真实源码：packages/attachment/attachment-local/src/normalization.ts。
alpha.1 重构（2026-08-24，上游 commit 30704dc1df）：规范化规制从 rc.2 的
2048 长边规则改为总像素预算（normalizedImageMaxPixels，缺省 2048×2048）+ 
8192 长边封顶，极端纵横比保住短边分辨率；编码走与 request-image 共享的
质量阶梯（encoding_ladder），每个阶梯质量都超字节目标时保留最小阶梯输出
（不再抛 IMAGE_TOO_LARGE）。载体：sharp → Pillow（依赖政策 2026-08-23 修订）。

上游语义：
  * 源已是干净单帧 8-bit sRGB/sRGBA 且在全部规范化限度内 → 字节原样直通；
  * 尺寸 = requestImageDimensions（总像素预算，纵横保持内收）再按长边封顶；
  * 否则按 alpha 分流重编码：带 alpha 走 WebP 阶梯、不透明走 JPEG 阶梯；
  * 重编码永不移除透明度（WebP 允许省略全不透明平面，见
    encoded_alpha_is_compatible）；产物回读验证事实一致后才发布。
"""
from __future__ import annotations

import io
from dataclasses import dataclass

from PIL import Image, ImageOps

from .encoding import EncodedImage, encode_first_within_limit, encoding_ladder, is_exhausted_encoding
from .error import ATTACHMENT_WRITE_FAILED, AttachmentError
from .image import DetectedImage, detect_image, encoded_alpha_is_compatible
from .projection import request_image_dimensions

__all__ = [
    "NormalizedImage",
    "NormalizationPolicy",
    "can_pass_through_normalization",
    "normalize_image",
    "prepared_source",
    "resized",
]


@dataclass(frozen=True)
class NormalizationPolicy:
    """持久化规范化附件的部署解析策略。"""

    maxPixels: int = 2048 * 2048
    maxDimension: int = 8192
    maxBytes: int = 4 * 1024 * 1024


@dataclass(frozen=True)
class NormalizedImage:
    """规范化字节与其持久引用所记录的事实。"""

    data: bytes
    mediaType: str
    width: int
    height: int


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
        and detected.width * detected.height <= policy.maxPixels
        and max(detected.width, detected.height) <= policy.maxDimension
    )


def prepared_source(data: bytes, has_alpha: bool) -> Image.Image:
    """提交字节 → 定向 sRGB 源（EXIF 定向 + 色彩空间归一 + 去除元数据）。

    编码前主动剥掉源 info（Pillow 会把 comment/ICC 等写进编码输出，与上游
    sharp 编码即去元数据的契约不符；协议要求规范化产物零元数据）。
    """
    image = Image.open(io.BytesIO(data))
    image.load()
    oriented = ImageOps.exif_transpose(image)
    if oriented is None:
        oriented = image
    result = oriented.convert("RGBA" if has_alpha else "RGB")
    result.info = {}
    return result


def resized(source: Image.Image, width: int, height: int) -> Image.Image:
    """纵横不变的目标尺寸（不放大；上游 resize inside 同款）。"""
    scale = min(1.0, width / source.width, height / source.height)
    target = (max(1, round(source.width * scale)), max(1, round(source.height * scale)))
    return source.resize(target, Image.Resampling.LANCZOS)


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


def _initial_dimensions(detected: DetectedImage, policy: NormalizationPolicy) -> tuple[int, int]:
    """总像素预算内的尺寸，再按长边封顶，纵横不变（上游 initialDimensions）。"""
    budgeted_width, budgeted_height = request_image_dimensions(
        detected.width, detected.height, policy.maxPixels
    )
    long_edge = max(budgeted_width, budgeted_height)
    if long_edge <= policy.maxDimension:
        return budgeted_width, budgeted_height
    scale = policy.maxDimension / long_edge
    return (
        max(1, int(budgeted_width * scale)),
        max(1, int(budgeted_height * scale)),
    )


def normalize_image(
    data: bytes,
    detected: DetectedImage,
    policy: NormalizationPolicy,
) -> NormalizedImage:
    """产出一个完整解码源的持久 provider 无关规范化版本（上游同名函数）。

    当每个阶梯质量都超字节目标时保留最小阶梯输出；provider 字节上限由传输
    路径在发包时执行。
    """
    if can_pass_through_normalization(detected, len(data), policy):
        return NormalizedImage(
            data=data, mediaType=detected.media_type,
            width=detected.width, height=detected.height,
        )
    try:
        width, height = _initial_dimensions(detected, policy)
        prepared = resized(prepared_source(data, detected.has_alpha), width, height)
        ladder = encoding_ladder(prepared, detected.has_alpha)
        encoded = encode_first_within_limit(ladder, policy.maxBytes)
        chosen: EncodedImage = (
            encoded.smallest if is_exhausted_encoding(encoded) else encoded
        )
        return _verify_normalized_image(
            NormalizedImage(
                data=chosen.data,
                mediaType=chosen.mediaType,
                width=chosen.width,
                height=chosen.height,
            ),
            None if detected.media_type == "image/gif" else detected.has_alpha,
        )
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
