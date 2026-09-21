"""bounded 文本缓冲 + 翻页 + UTF-8 尾截断（对齐 upstream terminal-bash/src/session.ts）。

* BoundedTextBuffer：scrollback 双限（字节 + 行数）链式 chunk 缓冲，字节账目按
  UTF-8 字节计（对齐 Buffer.byteLength），追加端做代理项拼接修正，淘汰端按
  code point 逐字符出队；首块永不增长保证淘汰 O(chunk) 均摊。
  （session.ts:44-156 —— 半身材拷贝优化在 Python 切片天然拷贝下无意义，省略。）
* read_scrollback：LocalPtySession.read 的等价面（session.ts:429-452），按行
  翻页 + `maxReadBytes` 尾截断，保 UTF-8 边界。
* utf8_tail：referenceTail（session-buffer.spec.ts:55-67）的尾截断算法。

载体差异：上游淘汰按 UTF-16 code unit 读取、代理对配对扣 4 字节；本实现按
Python code point 读取、4 档字节表（<0x80/0x800/0x10000/其余），仅在人工喂入
孤立代理项时走同款配对修正 —— 对真实解码文本两种记账完全一致
（已登记 design-terminal-domain.md §3 / verified-diffs）。
"""

from __future__ import annotations

from .types import _utf8_bytes

__all__ = ["BoundedTextBuffer", "read_scrollback", "utf8_tail"]

#: 碎片节点合并上限（对齐 session.ts:45 COALESCED_CHUNK_UNITS；语义为字符数，
#: 仅影响节点粒度，不影响内容与字节账目）。
COALESCED_CHUNK_UNITS = 4096


class _Chunk:
    __slots__ = ("text", "start", "next")

    def __init__(self, text: str, start: int, next=None):
        self.text = text
        self.start = start
        self.next = next


class BoundedTextBuffer:
    """maxBytes(+可选 maxLines) 双限链式文本缓冲，snapshot/consume 提供只读面。"""

    def __init__(self, max_bytes: int, max_lines: int | None = None):
        self.max_bytes = max_bytes
        self.max_lines = max_lines
        self._head: _Chunk | None = None
        self._tail: _Chunk | None = None
        self._bytes = 0
        self._newlines = 0
        self._last_code_unit = 0
        self._dropped = False

    @property
    def truncated(self) -> bool:
        return self._dropped

    @property
    def is_empty(self) -> bool:
        return self._head is None

    def append(self, text: str) -> None:
        if len(text) == 0:
            return
        self._bytes += _utf8_bytes(text)
        tail = self._tail
        last = self._last_code_unit
        first = ord(text[0])
        # 拼接可把两个三字节孤立代理项并成一个四字节码点；仅当前次末尾真实留在
        # 缓冲里（前一块未整块淘汰）才修正（session.ts:81-86 `tail !== undefined`）。
        if tail is not None and 0xD800 <= last <= 0xDBFF and 0xDC00 <= first <= 0xDFFF:
            self._bytes -= 2
        for index in range(len(text)):
            if text[index] == "\n":
                self._newlines += 1
        self._last_code_unit = ord(text[-1])
        # 首块永不增长（淘汰不重扫增长串）：只向后块合并。
        if tail is not None and tail is not self._head and len(tail.text) + len(text) <= COALESCED_CHUNK_UNITS:
            tail.text += text
        else:
            chunk = _Chunk(text, 0)
            if tail is None:
                self._head = chunk
            else:
                tail.next = chunk
            self._tail = chunk

        while (self._head is not None
                and (self._bytes > self.max_bytes
                     or (self.max_lines is not None and self._newlines >= self.max_lines))):
            head = self._head
            first = ord(head.text[head.start])
            second = ord(head.text[head.start + 1]) if head.start + 1 < len(head.text) else (
                ord(head.next.text[0]) if head.next is not None else None)
            paired = 0xD800 <= first <= 0xDBFF and second is not None and 0xDC00 <= second <= 0xDFFF
            if paired:
                self._bytes -= 4
            else:
                self._bytes -= 1 if first < 0x80 else (2 if first < 0x800 else 3 if first < 0x10000 else 4)
            if first == 10:
                self._newlines -= 1
            self._advance(2 if paired else 1)
            self._dropped = True

    def _advance(self, units: int) -> None:
        while units > 0 and self._head is not None:
            head = self._head
            count = min(units, len(head.text) - head.start)
            head.start += count
            units -= count
            if head.start == len(head.text):
                self._head = head.next
        if self._head is None:
            self._tail = None

    def snapshot(self) -> dict:
        chunks: list[str] = []
        chunk = self._head
        while chunk is not None:
            chunks.append(chunk.text[chunk.start:])
            chunk = chunk.next
        return {"text": "".join(chunks), "truncated": self._dropped}

    def consume(self) -> dict:
        """消费全部保留文本并复位（对齐 session.ts:139-147，返回 delta/truncated）。"""
        snapshot = self.snapshot()
        self._head = None
        self._tail = None
        self._bytes = 0
        self._newlines = 0
        self._dropped = False
        return {"delta": snapshot["text"], "truncated": snapshot["truncated"]}


def _utf8_units(text: str):
    """按 UTF-16 语义切分字节单位：代理对为 1 单位 4 字节（对齐 Buffer.byteLength 逐单位面）。"""
    index = 0
    size = len(text)
    while index < size:
        code = ord(text[index])
        if 0xD800 <= code <= 0xDBFF and index + 1 < size and 0xDC00 <= ord(text[index + 1]) <= 0xDFFF:
            yield text[index:index + 2], 4
            index += 2
        else:
            yield text[index], 1 if code < 0x80 else 2 if code < 0x800 else 3 if code < 0x10000 else 4
            index += 1


def utf8_tail(text: str, max_bytes: int) -> dict:
    """UTF-8 字节上限的尾截断（对齐 referenceTail，保字符 / 代理对边界）。"""
    if _utf8_bytes(text) <= max_bytes:
        return {"text": text, "truncated": False}
    units = list(_utf8_units(text))
    bytes_count = 0
    start = len(units)
    while start > 0:
        size = units[start - 1][1]
        if bytes_count + size > max_bytes:
            break
        bytes_count += size
        start -= 1
    return {"text": "".join(unit for unit, _ in units[start:]), "truncated": True}


def read_scrollback(snapshot: dict, max_read_bytes: int, request: dict | None = None) -> dict:
    """scrollback 快照按行翻页 + 尾截断（对齐 session.ts LocalPtySession.read）。

    参数 request {offset?, count?}，缺省 offset=0 / count=500；非法值 fail loud。
    """
    request = dict(request or {})
    text = snapshot["text"]
    lines = text.split("\n")
    total_lines = 0 if len(text) == 0 else len(lines)
    offset = request.get("offset", 0)
    count = request.get("count", 500)
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        raise RuntimeError("PTY read offset must be a non-negative safe integer")
    if not isinstance(count, int) or isinstance(count, bool) or count <= 0:
        raise RuntimeError("PTY read count must be a positive safe integer")
    if offset >= total_lines:
        return {
            "text": "", "totalLines": total_lines,
            "lineBegin": offset, "lineEnd": offset, "truncated": snapshot["truncated"],
        }
    end = total_lines - offset
    start = max(0, end - count)
    requested = "\n".join(lines[start:end])
    bounded = utf8_tail(requested, max_read_bytes)
    returned_lines = 0 if len(bounded["text"]) == 0 else len(bounded["text"].split("\n"))
    return {
        "text": bounded["text"], "totalLines": total_lines,
        "lineBegin": offset, "lineEnd": offset + returned_lines,
        "truncated": snapshot["truncated"] or bounded["truncated"],
    }