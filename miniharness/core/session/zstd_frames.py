"""JSONL 持久化后端的 Zstandard 帧原语。

对应 dsh 真实源码：packages/session/session-persistence-jsonl/src/zstd.ts。

后端拥有一个**拼接帧容器**：每个耐久批次（header 行或事件批）压成一条
独立可解码、带校验和的 zstd 帧，按序追加进同一文件 —— 因此可以只扫结构
定位完整帧（不解压块）、对 torn 尾帧做前缀恢复，而不把压缩机制泄漏到
持久化接缝之外。

载体：Node 侧用 node:zlib；Python 侧用 `zstandard` 库（依赖政策：
成熟开源库优先）。压缩参数对齐上游：写校验和；内容尺寸随库默认携带
（与 libzstd contentSizeFlag 缺省一致），扫描器两种头形都认。
"""
from __future__ import annotations

from dataclasses import dataclass

import zstandard

# Zstandard 帧魔数（小端 0xFD2FB528）。
ZSTD_MAGIC = 0xFD2FB528


@dataclass(frozen=True)
class ZstdFrameRange:
    """一条结构完整帧占用的字节区间（start 含，end 不含）。"""

    start: int
    end: int


@dataclass(frozen=True)
class ZstdFrameScan:
    """拼接 zstd 流的结构扫描结果。"""

    #: 按文件序排列的完整帧。
    frames: list[ZstdFrameRange]
    #: EOF 打断最后一帧时，该残帧的起始偏移；无残帧为 None。
    torn_start: int | None = None


def scan_zstd_frames(
    buffer: bytes, max_frames: int | None = None
) -> ZstdFrameScan:
    """不解压块、只凭帧结构定位完整帧。

    完整结构非法即拒绝（响亮报错）；EOF 落在末帧内部时返回其起点供修复。
    头部长度按描述符逐字段推算（内容尺寸标志 / 单段标志 / 校验和位 /
    字典 ID 位），块循环按 3 字节块头推进，RLE 块只占 1 字节载荷。
    """
    limit = max_frames if max_frames is not None else float("inf")
    frames: list[ZstdFrameRange] = []
    offset = 0
    n = len(buffer)

    while offset < n:
        start = offset
        if n - offset < 4:
            return ZstdFrameScan(frames, start)
        if int.from_bytes(buffer[offset : offset + 4], "little") != ZSTD_MAGIC:
            raise ValueError(
                f"corrupt Zstandard session log: invalid frame magic at byte {offset}"
            )
        offset += 4

        if offset == n:
            return ZstdFrameScan(frames, start)
        descriptor = buffer[offset]
        offset += 1
        if descriptor & 0x18:
            raise ValueError(
                "corrupt Zstandard session log: reserved frame-header bit at byte "
                f"{offset - 1}"
            )

        content_size_flag = descriptor >> 6
        single_segment = bool(descriptor & 0x20)
        checksum = bool(descriptor & 0x04)
        dictionary_flag = descriptor & 0x03
        dictionary_bytes = 4 if dictionary_flag == 3 else dictionary_flag
        content_size_bytes = (
            (1 if single_segment else 0) if content_size_flag == 0 else 1 << content_size_flag
        )
        remaining_header = (0 if single_segment else 1) + dictionary_bytes + content_size_bytes
        if n - offset < remaining_header:
            return ZstdFrameScan(frames, start)
        offset += remaining_header

        while True:
            if n - offset < 3:
                return ZstdFrameScan(frames, start)
            block_header = int.from_bytes(buffer[offset : offset + 3], "little")
            offset += 3
            last_block = bool(block_header & 1)
            block_type = (block_header >> 1) & 0x03
            block_size = block_header >> 3
            if block_type == 0x03:
                raise ValueError(
                    f"corrupt Zstandard session log: reserved block type at byte {offset - 3}"
                )
            payload_bytes = 1 if block_type == 0x01 else block_size
            if n - offset < payload_bytes:
                return ZstdFrameScan(frames, start)
            offset += payload_bytes
            if last_block:
                break

        if checksum:
            if n - offset < 4:
                return ZstdFrameScan(frames, start)
            offset += 4
        frames.append(ZstdFrameRange(start, offset))
        if len(frames) >= limit:
            return ZstdFrameScan(frames)

    return ZstdFrameScan(frames)


def compress_zstd_frame(data: bytes | str) -> bytes:
    """把一段 JSONL 明文（header 行或耐久事件批）压成一条独立可解码、
    带校验和的完整 zstd 帧。"""
    payload = data.encode("utf-8") if isinstance(data, str) else data
    return zstandard.ZstdCompressor(write_checksum=True).compress(payload)


def decompress_zstd_frame(frame: bytes) -> bytes:
    """解压一条结构完整的帧并验证其校验和。

    走流式解码器而非一次性 decompress()：上游写入侧是流式压缩，帧头不带
    内容大小字段（无 single_segment），one-shot API 会以 "could not determine
    content size in frame header" 拒绝——跨实现互读必须容忍两种帧头形态。
    """
    return zstandard.ZstdDecompressor().decompressobj().decompress(frame)


def decompress_zstd_prefix(torn: bytes) -> bytes:
    """从结构不完整的末帧恢复可得明文。

    有意不做末帧收尾与校验和完成（等价上游 finishFlush=ZSTD_e_flush）：
    截断输入只产出已可得的明文前缀而不报错；调用方必须先用扫描器确立
    torn 帧边界再使用本结果。极短残帧可能产不出任何明文。
    """
    return zstandard.ZstdDecompressor().decompressobj().decompress(torn)


def decode_frames(buffer: bytes, frames: list[ZstdFrameRange]):
    """按源序解出每条完整帧的明文并验证校验和（惰性生成器）。

    与 decompress_zstd_frame 同理走流式解码，兼容无内容大小字段的帧头。
    """
    decompressor = zstandard.ZstdDecompressor()
    for frame in frames:
        obj = decompressor.decompressobj()
        yield obj.decompress(buffer[frame.start : frame.end])


def read_first_frame(read_chunk: callable, chunk_size: int = 8192):
    """增量读取流中第一条完整帧并解压（header-only 列举用）。

    ``read_chunk()`` 返回一次读取到的字节块（EOF 返回 b''）。找不到任何
    完整帧返回 None；首帧解压/校验失败按损坏报错。
    """
    content = b""
    while True:
        chunk = read_chunk()
        if not chunk:
            return None
        content += chunk
        scan = scan_zstd_frames(content, max_frames=1)
        if not scan.frames:
            continue
        frame = scan.frames[0]
        try:
            # 流式解码：兼容无内容大小字段的帧头（见 decompress_zstd_frame）。
            return zstandard.ZstdDecompressor().decompressobj().decompress(
                content[frame.start : frame.end]
            )
        except zstandard.ZstdError as error:
            raise ValueError(
                "corrupt Zstandard session log: header frame failed validation"
            ) from error
