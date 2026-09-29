"""LSP base-protocol 分帧：`Content-Length` 界定的 JSON-RPC over 字节流。

编码器产出一份带帧缓冲；解码器缓冲入站字节并吐出完整消息体，对头部与总消息大小设界，
使敌对或损坏的服务端无法耗尽内存。对齐 packages/lsp/lsp-stdio/src/framing.ts。
"""
from __future__ import annotations

import json

__all__ = ["MAX_HEADER_BYTES", "MessageDecoder", "encode_message"]

#: 头部段上限：服务端永不发分隔符时不能无限增长缓冲。
MAX_HEADER_BYTES = 1 << 16

_HEADER_SEPARATOR = b"\r\n\r\n"


def encode_message(message: object) -> bytes:
    """把一条 JSON-RPC 消息编码为带帧 LSP 缓冲（`Content-Length: N\\r\\n\\r\\n<utf-8 json>`）。

    与上游 `JSON.stringify` + `Buffer.from(utf8)` 等价：紧凑分隔符、非 ASCII 原样 UTF-8
    （不转义成 \\uXXXX，故 Content-Length 与实际字节一致）。
    """
    body = json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
    return header + body


class MessageDecoder:
    """流式解码 `Content-Length` 分帧 JSON-RPC。

    喂 stdout chunk；返回已凑齐的整消息体。只解析 `Content-Length` 头、忽略其它头
    （如 `Content-Type`），与 base protocol 一致。
    """

    def __init__(self, max_message_bytes: int):
        self._buffer = b""
        self._max_message_bytes = max_message_bytes

    def push(self, chunk: bytes) -> list:
        """追加 chunk 并返回当前已完整的全部消息体（按到达序，可能为空）。

        头部畸形或消息体超 `max_message_bytes` → 抛 ValueError。
        """
        self._buffer = chunk if len(self._buffer) == 0 else self._buffer + chunk
        messages: list = []
        while True:
            step = self._next()
            if step is None:
                break
            messages.append(step)
        return messages

    def _next(self):
        """解析并消费下一条完整消息；字节不足 → None。"""
        separator = self._buffer.find(_HEADER_SEPARATOR)
        if separator < 0:
            if len(self._buffer) > MAX_HEADER_BYTES:
                raise ValueError(
                    f"LSP header exceeded {MAX_HEADER_BYTES} bytes without a terminator")
            return None
        if separator > MAX_HEADER_BYTES:
            raise ValueError(f"LSP header exceeded {MAX_HEADER_BYTES} bytes")
        header_text = self._buffer[:separator].decode("ascii")
        content_length = _parse_content_length(header_text)
        if content_length > self._max_message_bytes:
            raise ValueError(
                f"LSP message length {content_length} exceeds the "
                f"{self._max_message_bytes}-byte limit")
        body_start = separator + len(_HEADER_SEPARATOR)
        body_end = body_start + content_length
        if len(self._buffer) < body_end:
            return None
        body = self._buffer[body_start:body_end].decode("utf-8")
        self._buffer = self._buffer[body_end:]
        try:
            return json.loads(body)
        except ValueError as error:
            raise ValueError(f"LSP message body was not valid JSON: {error}") from None


def _parse_content_length(header_text: str) -> int:
    """读 `Content-Length` 头值（大小写不敏感），缺头或非数字即拒。"""
    for line in header_text.split("\r\n"):
        colon = line.find(":")
        if colon < 0:
            continue
        if line[:colon].strip().lower() != "content-length":
            continue
        raw = line[colon + 1:].strip()
        try:
            value = int(raw)
        except ValueError:
            raise ValueError(f"invalid Content-Length header: {line!r}") from None
        if value < 0:
            raise ValueError(f"invalid Content-Length header: {line!r}")
        return value
    raise ValueError(f"LSP header block missing Content-Length: {header_text!r}")
