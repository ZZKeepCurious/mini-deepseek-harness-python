"""规范化和请求图编码器共享的惰性候选执行。

对应 dsh 真实源码：packages/attachment/attachment-local/src/encoding.ts
（rc.2 新增，已核实）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

__all__ = ["EncodedCandidate", "encode_first_within_limit", "is_exhausted_encoding"]


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
