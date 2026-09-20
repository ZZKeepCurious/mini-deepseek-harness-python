"""一方事件语义文本抽取（对齐 session-query/src/extraction.ts）。

结构边界、内嵌原始流、请求信封与未知/声明合并事件不贡献文本；只有一方语义事件产生
可检索文本。mini 事件数据形态与上游一致（`user/message`/`assistant/message`/`tool/call`/
`tool/result`/`turn/end`）。
"""
from __future__ import annotations

from collections.abc import Mapping

__all__ = ["extract_event_text"]


def _join(parts: list) -> str:
    return "\n".join(str(part).strip() for part in parts if part and str(part).strip())


def _block_text(block: object) -> list:
    if not isinstance(block, Mapping):
        return []
    kind = block.get("type")
    if kind == "text":
        return [block.get("text", "")]
    if kind == "reasoning":
        return []
    if kind == "tool-call":
        return [block.get("name", ""), block.get("arguments", "")]
    if kind == "tool-result":
        content = block.get("content") or []
        out: list = []
        for inner in content:
            out.extend(_block_text(inner))
        return out
    return []


def _content_text(content: object) -> str:
    if not isinstance(content, (list, tuple)):
        return ""
    parts: list = []
    for block in content:
        parts.extend(_block_text(block))
    return _join(parts)


def _turn_end_text(reason: object) -> str:
    if not isinstance(reason, Mapping):
        return ""
    kind = reason.get("kind")
    if kind == "error":
        error = reason.get("error") or {}
        message = error.get("message") if isinstance(error, Mapping) else str(error)
        return _join(["error", message])
    if kind == "aborted":
        return "aborted"
    if kind in ("max-tokens", "interrupted"):
        return kind
    return ""


def extract_event_text(event: dict) -> str:
    """从一条一方事件抽取可检索语义文本；不可检索 → 空串。"""
    kind = event.get("type")
    data = event.get("data")
    if not isinstance(data, Mapping):
        return ""
    if kind == "user/message":
        return _content_text(data.get("content"))
    if kind == "assistant/message":
        message = data.get("message") or {}
        return _content_text(message.get("content") if isinstance(message, Mapping) else None)
    if kind == "tool/call":
        return _join([data.get("name", ""), data.get("arguments", "")])
    if kind == "tool/result":
        message = data.get("message") or {}
        error = data.get("error") or {}
        return _join([
            _content_text(message.get("content") if isinstance(message, Mapping) else None),
            error.get("name", "") if isinstance(error, Mapping) else "",
            error.get("code", "") if isinstance(error, Mapping) else "",
        ])
    if kind == "turn/end":
        return _turn_end_text(data.get("reason"))
    return ""
