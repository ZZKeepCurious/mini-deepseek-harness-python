"""Lossless range encoding for JSONL `sourceEventSeqs` arrays.

上游对照：packages/core/session/src/seq-ranges.ts（dsh-v0.1.2-alpha.1）。
`sourceEventSeqs` 存储形态从裸 seq 数组升级为「数字或 `[start, end]` 闭区间对」的混合
列表：连续 ≥3 个递增整数被折叠为闭区间对，读侧需展开。本模块提供纯函数 encode / decode。
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "EncodedSeq",
    "decode_seq_ranges",
    "encode_seq_ranges",
]

# 单个存储的源 seq 或闭区间对（inclusive）。
EncodedSeq = int | list[int] | tuple[int, int]


def _is_strictly_increasing(values: list[int]) -> bool:
    return all(values[i] > values[i - 1] for i in range(1, len(values)))


def encode_seq_ranges(values: list[int]) -> list[EncodedSeq]:
    """将有价值（连续）的游程替换为闭区间对（上游 encodeSeqRanges）。

    @param values: 已校验的内存态源 seq（合法输入为严格递增 + 非负安全整数）。
    @returns: 无损的 JSON 存储形态。
    """
    if not _is_strictly_increasing(values):
        return list(values)
    encoded: list[EncodedSeq] = []
    start = 0
    while start < len(values):
        end = start
        while end + 1 < len(values) and values[end + 1] == values[end] + 1:
            end += 1
        if end - start >= 2:
            encoded.append([values[start], values[end]])
        else:
            for index in range(start, end + 1):
                encoded.append(values[index])
        start = end + 1
    return encoded


def _assert_seq(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise TypeError("sourceEventSeqs must contain non-negative safe integers")
    return value


def decode_seq_ranges(value: Any, max_entries: int = 2 ** 53 - 1) -> list[int]:
    """展开 JSON 存储形态的源 seq 数组（上游 decodeSeqRanges）。

    @param value: 解析后的存储值。
    @param max_entries: 所属事件允许的最大表项数。
    @returns: 内存态源 seq（严格递增）。
    """
    if not isinstance(value, (list, tuple)):
        raise TypeError("sourceEventSeqs must be an array")
    decoded: list[int] = []
    has_range = False
    for entry in value:
        if isinstance(entry, int) and not isinstance(entry, bool):
            _assert_seq(entry)
            if len(decoded) >= max_entries:
                raise TypeError("sourceEventSeqs exceeds its event sequence")
            decoded.append(entry)
            continue
        if not (isinstance(entry, (list, tuple)) and len(entry) == 2):
            raise TypeError("sourceEventSeqs range entries must be [start, end] pairs")
        start = _assert_seq(entry[0])
        end = _assert_seq(entry[1])
        if end < start:
            raise TypeError("sourceEventSeqs ranges require start <= end")
        length = end - start + 1
        if length > max_entries - len(decoded):
            raise TypeError("sourceEventSeqs range exceeds its event sequence")
        for seq in range(start, end + 1):
            decoded.append(seq)
        has_range = True
    if has_range and not _is_strictly_increasing(decoded):
        raise TypeError("sourceEventSeqs ranges must be strictly increasing")
    return decoded
