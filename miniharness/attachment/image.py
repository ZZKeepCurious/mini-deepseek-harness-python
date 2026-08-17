"""光栅探测：受理时解析头部取得格式/尺寸，读取时仅复验（不完整解码）。

对应 dsh 真实源码：packages/attachment/attachment-local/src/image.ts（已核实）。

上游语义（image.ts）：
  * detectImage 完整解码（sharp）→ {mediaType, width, height}，超过
    maxPixels 抛 IMAGE_TOO_MANY_PIXELS；畸形抛 INVALID_IMAGE；
  * probeImage 仅头部探测（读路径复用，避免历史重放时的逐请求解码放大）；
  * 支持 png / jpeg / webp / gif 四种光栅。

载体简化：上游以 sharp 完整解码像素做权威校验；mini 纯 stdlib（无图像解码
库），以头部结构解析 + 签名校验近似（可检出"缺头/尺寸异常/类型不符"），
无法检出像素级损坏——语义等价于"受理校验存在性 + 尺寸 + 限制"，深度受
stdlib 限制，标注于 WRITING-STYLE §4.1。读取路径与上游同为头部复验。
"""
from __future__ import annotations

import struct

from .error import (
    IMAGE_TOO_MANY_PIXELS,
    INVALID_IMAGE,
    IMAGE_TYPE_MISMATCH,
    AttachmentError,
)

__all__ = ["DetectedImage", "detect_image", "probe_image", "image_media_type"]

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_JPEG_SIG = b"\xff\xd8"
_GIF_SIGS = (b"GIF87a", b"GIF89a")
_WEBP_RIFF = b"RIFF"
_WEBP_WEBP = b"WEBP"

# JPEG SOF 标记（含高度/宽度的帧头）；排除 DHT(C4)/DAC(CC)/DQT(DB)/DRI(DD)/APPn/E0-EF 等
_JPEG_SOF_MARKERS = frozenset(
    {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}
)


class DetectedImage:
    """已解析光栅的元数据：格式 + 固有尺寸。"""

    __slots__ = ("media_type", "width", "height")

    def __init__(self, media_type: str, width: int, height: int):
        self.media_type = media_type
        self.width = width
        self.height = height


def image_media_type(header: bytes) -> str | None:
    """按签名白名单探测媒体类型（上游 MEDIA_TYPES 映射的 stdlib 近似）。"""
    if header.startswith(_PNG_SIG):
        return "image/png"
    if header.startswith(_JPEG_SIG):
        return "image/jpeg"
    if header.startswith(_GIF_SIGS):
        return "image/gif"
    if header.startswith(_WEBP_RIFF) and len(header) >= 12 and header[8:12] == _WEBP_WEBP:
        return "image/webp"
    return None


def _parse_png(data: bytes) -> DetectedImage:
    # PNG：签名 8B + IHDR 长度 4B + "IHDR" 4B 后为 width/height（大端）
    if len(data) < 24:
        raise AttachmentError("Unsupported or malformed image data.", INVALID_IMAGE)
    if data[12:16] != b"IHDR":
        raise AttachmentError("Unsupported or malformed image data.", INVALID_IMAGE)
    width, height = struct.unpack(">II", data[16:24])
    return DetectedImage("image/png", width, height)


def _parse_jpeg(data: bytes) -> DetectedImage:
    # JPEG：FF xx 标记流；SOF 帧头在长度后为 precision(1) + height(2) + width(2)
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker == 0xFF or marker == 0x00:
            i += 1
            continue
        if 0xD0 <= marker <= 0xD9:  # RST/SOI/EOI 无长度
            if marker == 0xD9:
                break
            i += 2
            continue
        if i + 4 > n:
            break
        length = struct.unpack(">H", data[i + 2:i + 4])[0]
        if marker in _JPEG_SOF_MARKERS and length >= 7:
            precision = data[i + 4]
            if precision == 8 and i + 8 <= n:
                height, width = struct.unpack(">HH", data[i + 5:i + 9])
                return DetectedImage("image/jpeg", width, height)
        i += 2 + length
    raise AttachmentError("Unsupported or malformed image data.", INVALID_IMAGE)


def _parse_gif(data: bytes) -> DetectedImage:
    # GIF：签名 6B + 逻辑屏幕宽度/高度（小端）
    if len(data) < 10:
        raise AttachmentError("Unsupported or malformed image data.", INVALID_IMAGE)
    width, height = struct.unpack("<HH", data[6:10])
    return DetectedImage("image/gif", width, height)


def _parse_webp(data: bytes) -> DetectedImage:
    # WebP：RIFF 4B + 大小 4B + WEBP 4B 后按块类型解析
    if len(data) < 30:
        raise AttachmentError("Unsupported or malformed image data.", INVALID_IMAGE)
    kind = data[12:16]
    if kind == b"VP8X":
        # VP8X：flags 1B + 3B 宽-1 + 3B 高-1（小端 24 位）
        if len(data) < 30:
            raise AttachmentError("Unsupported or malformed image data.", INVALID_IMAGE)
        width = int.from_bytes(data[24:27], "little") + 1
        height = int.from_bytes(data[27:30], "little") + 1
        return DetectedImage("image/webp", width, height)
    if kind == b"VP8 ":
        # 有损 VP8：帧头后 3B 同步码 + 14 位宽/14 位高（小端）
        if len(data) < 30:
            raise AttachmentError("Unsupported or malformed image data.", INVALID_IMAGE)
        if data[23:26] != b"\x9d\x01\x2a":
            raise AttachmentError("Unsupported or malformed image data.", INVALID_IMAGE)
        width = int.from_bytes(data[26:28], "little") & 0x3FFF
        height = int.from_bytes(data[28:30], "little") & 0x3FFF
        return DetectedImage("image/webp", width, height)
    if kind == b"VP8L":
        # 无损 VP8L：签名 0x2f + 4B（14 位宽-1 + 14 位高-1，小端）
        if len(data) < 25 or data[20] != 0x2F:
            raise AttachmentError("Unsupported or malformed image data.", INVALID_IMAGE)
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return DetectedImage("image/webp", width, height)
    raise AttachmentError("Unsupported or malformed image data.", INVALID_IMAGE)


def _probe(data: bytes) -> DetectedImage:
    if len(data) < 12:
        raise AttachmentError("Unsupported or malformed image data.", INVALID_IMAGE)
    media_type = image_media_type(data[:12])
    if media_type == "image/png":
        return _parse_png(data)
    if media_type == "image/jpeg":
        return _parse_jpeg(data)
    if media_type == "image/gif":
        return _parse_gif(data)
    if media_type == "image/webp":
        return _parse_webp(data)
    raise AttachmentError("Unsupported or malformed image data.", INVALID_IMAGE)


def probe_image(data: bytes) -> DetectedImage:
    """解析光栅头部，返回格式与固有尺寸（读取路径：不复验像素）。"""
    return _probe(data)


def detect_image(data: bytes, max_pixels: int | None = None) -> DetectedImage:
    """受理路径：解析 + 像素限制校验（上游 detectImage 的 stdlib 近似）。

    简化标注：上游以 sharp 完整解码像素（可拒绝解码中途损坏的光栅）；mini
    纯 stdlib 无解码库，仅做头部结构 + 签名校验，像素级损坏无法检出。
    """
    detected = _probe(data)
    if max_pixels is not None and detected.width * detected.height > max_pixels:
        raise AttachmentError(
            "Image exceeds the configured decoded-pixel limit.", IMAGE_TOO_MANY_PIXELS
        )
    return detected


def assert_media_type_matches(detected: DetectedImage, declared: str) -> None:
    """声明媒体类型与字节解析结果不一致 → IMAGE_TYPE_MISMATCH。"""
    if detected.media_type != declared:
        raise AttachmentError(
            "Declared image type does not match its bytes.", IMAGE_TYPE_MISMATCH
        )
