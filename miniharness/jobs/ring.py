"""一条作业背后的有界输出 ring（对齐 jobs-local/src/ring.ts）。

绝对字节偏移的 chunk 序列、绝不移动已分配偏移的头部驱逐、从任意偏移非消耗读。
容量以 UTF-8 字节计：追加超限丢最老保留字节；单块超限只留其 UTF-8 安全尾部并打
`gapBefore`。读者低于保留窗口得到 lossy 读，而不是报错。
"""
from __future__ import annotations

from .view import chunk_from_ring
from .types import JOB_CHANNELS

__all__ = ["OutputRing"]


def _utf8_tail(text: str, max_bytes: int) -> tuple[str, int]:
    """`text` 的 UTF-8 安全尾部，字节长不超过 `max_bytes`。

    字节切点向后越过续字节（0b10xxxxxx），幸存文本永不从码点中途开始。
    """
    raw = text.encode("utf-8")
    start = len(raw) - max_bytes
    while start < len(raw) and (raw[start] & 0xC0) == 0x80:
        start += 1
    tail = raw[start:]
    return tail.decode("utf-8"), len(tail)


class OutputRing:
    """有界 ring：偏移跨驱逐保持绝对，`earliest` 只会前进。"""

    __slots__ = ("_chunks", "retained_bytes", "total", "earliest")

    def __init__(self) -> None:
        self._chunks: list[dict] = []
        self.retained_bytes = 0
        #: 曾经追加过的 UTF-8 总字节 —— 下一块 chunk 的起始偏移。
        self.total = 0
        #: 最老保留字节的偏移（无保留时等于 total）。
        self.earliest = 0

    def append(self, text: str, options: dict | None, cap: int) -> bool:
        """追加一块并按 `cap` 裁剪头部；空 chunk 返回 False（无变化）。"""
        if len(text) == 0:
            return False
        channel = (options or {}).get("channel")
        if channel is not None and channel not in JOB_CHANNELS:
            channel = None
        chunk: dict = {"at": self.total, "text": text,
                       "bytes": len(text.encode("utf-8"))}
        if channel is not None:
            chunk["channel"] = channel
        if (options or {}).get("gapBefore"):
            chunk["gapBefore"] = True
        self._chunks.append(chunk)
        self.total += chunk["bytes"]
        self.retained_bytes += chunk["bytes"]
        self.trim(cap)
        return True

    def trim(self, cap: int) -> None:
        """丢弃头部保留块直到落入 `cap`；单块超限只留 UTF-8 安全尾部。"""
        while self.retained_bytes > cap and len(self._chunks) > 1:
            dropped = self._chunks.pop(0)
            self.retained_bytes -= dropped["bytes"]
        if len(self._chunks) == 1 and self._chunks[0]["bytes"] > cap:
            single = self._chunks[0]
            tail_text, tail_bytes = _utf8_tail(single["text"], cap)
            single["at"] += single["bytes"] - tail_bytes
            single["text"] = tail_text
            single["bytes"] = tail_bytes
            single["gapBefore"] = True
            self.retained_bytes = tail_bytes
        self.earliest = self._chunks[0]["at"] if self._chunks else self.total

    def read_from(self, from_byte: int) -> dict:
        """返回与 `[from, total)` 相交的保留块（对外 JobChunk）、续读偏移与 lossy。"""
        chunks = []
        for chunk in self._chunks:
            if chunk["at"] + chunk["bytes"] <= from_byte:
                continue
            chunks.append(chunk_from_ring(chunk))
        return {"chunks": chunks, "next": self.total, "lossy": from_byte < self.earliest}
