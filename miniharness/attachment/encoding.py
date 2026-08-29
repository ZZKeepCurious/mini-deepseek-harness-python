"""规范化和请求图编码器共享的质量阶梯与惰性候选执行。

对应 dsh 真实源码：packages/attachment/attachment-local/src/encoding.ts。
alpha.1 重构（2026-08-24，上游 commit 30704dc1df / 4863890535）：共享质量
阶梯 [85,75,60]（间隔真实换取尺寸缩减）与按 alpha 分流的编码阶梯
（写 encoder），废弃 rc.2 的低彩色调色板 PNG 分类。载体：sharp → Pillow
（依赖政策 2026-08-23 修订）。
"""
from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Callable

from PIL import Image

__all__ = [
    "IMAGE_ENCODING_QUALITIES",
    "WEBP_ENCODING_EFFORT",
    "EncodedCandidate",
    "EncodedImage",
    "encode_first_within_limit",
    "encoding_ladder",
    "is_exhausted_encoding",
]

#: 两个编码器共享的阶梯：间隔刻意拉开，每一步都买到真实尺寸缩减（上游同款）
IMAGE_ENCODING_QUALITIES: tuple[int, int, int] = (85, 75, 60)
#: 有损 WebP 的固定 effort；更深搜索花 3-4 倍编码时间只换约 5% 尺寸
WEBP_ENCODING_EFFORT = 0


@dataclass(frozen=True)
class EncodedImage:
    """一个阶梯输出：完整字节与精确事实。"""

    data: bytes
    mediaType: str  # 'image/jpeg' | 'image/webp'
    width: int
    height: int


def _encode_pillow(image: Image.Image, media_type: str, quality: int) -> EncodedImage:
    buffer = io.BytesIO()
    if media_type == "image/webp":
        image.save(buffer, format="WEBP", quality=quality,
                   method=WEBP_ENCODING_EFFORT)
    else:
        image.convert("RGB").save(buffer, format="JPEG", quality=quality)
    with Image.open(io.BytesIO(buffer.getvalue())) as out:
        width, height = out.size
    return EncodedImage(data=buffer.getvalue(), mediaType=media_type,
                        width=width, height=height)


def encoding_ladder(
    prepared: Image.Image,
    has_alpha: bool,
) -> list[Callable[[], EncodedImage]]:
    """为一个已准备（定尺寸 sRGB）管线构建惰性质量阶梯：WebP 保留源 alpha
    通道，其余一律 JPEG（上游 encodingLadder 同款分流）。

    @param prepared: 已定向、sRGB、目标尺寸的小写图像；克隆后逐候选编码。
    @param has_alpha: 解码得到的源 alpha 事实（选择编码器）。
    """
    media_type = "image/webp" if has_alpha else "image/jpeg"
    return [
        (lambda q=q, mt=media_type: _encode_pillow(prepared.copy(), mt, q))
        for q in IMAGE_ENCODING_QUALITIES
    ]


@dataclass(frozen=True)
class EncodedCandidate:
    """携带完整字节的一个编码候选。"""

    data: bytes


@dataclass(frozen=True)
class ExhaustedEncoding:
    """一个尺寸下所有候选都超限的穷尽结果（保留最小者）。"""

    smallest: EncodedCandidate


def encode_first_within_limit(
    attempts: list[Callable[[], EncodedCandidate]],
    max_bytes: int,
) -> EncodedCandidate | ExhaustedEncoding:
    """按偏好序执行编码候选，首个达标即停；否则返回最小完成候选。"""
    if not attempts:
        raise ValueError("image encoding requires at least one candidate")
    smallest = attempts[0]()
    if len(smallest.data) <= max_bytes:
        return smallest
    for attempt in attempts[1:]:
        candidate = attempt()
        if len(candidate.data) <= max_bytes:
            return candidate
        if len(candidate.data) < len(smallest.data):
            smallest = candidate
    return ExhaustedEncoding(smallest=smallest)


def is_exhausted_encoding(result: object) -> bool:
    """一个尺寸下的编码结果是否穷尽了每个候选。"""
    return isinstance(result, ExhaustedEncoding)
