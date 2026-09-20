"""事件记录与语义文档投影（对齐 session-query/src/documents.ts）。

surface 分类：`current`（当前 model surface 节点）、`shadowed`（被 replace 遮蔽的
surface 事件）、`log-only`（其余）。mini 经 `core.session.surface._surface_nodes` 取当前
节点（不建上游 foldSurface 的完整节点/替换结构——语义等价，登记）。
"""
from __future__ import annotations

from ..core.session.surface import _surface_nodes, derive_event_message
from .extraction import extract_event_text

__all__ = ["build_event_records", "build_search_documents", "classify_surface", "is_surface_event"]


def is_surface_event(event: dict) -> bool:
    try:
        return derive_event_message(event) is not None
    except Exception:  # noqa: BLE001 - 无法派生即非 surface
        return False


def classify_surface(events: list) -> dict:
    """返回 `{seq: 'current'|'shadowed'|'log-only'}`（对齐 classifySurface）。"""
    current = {event.get("seq") for event in _surface_nodes(events)}
    result: dict = {}
    for event in events:
        seq = event.get("seq")
        if seq in current:
            result[seq] = "current"
        elif is_surface_event(event):
            result[seq] = "shadowed"
        else:
            result[seq] = "log-only"
    return result


def build_event_records(session_id: str, events: list) -> list:
    surface = classify_surface(events)
    return [{
        "sessionId": session_id,
        "seq": event.get("seq"),
        "type": event.get("type"),
        "time": event.get("time"),
        "surface": surface.get(event.get("seq"), "log-only"),
    } for event in events]


def build_search_documents(session_id: str, events: list) -> list:
    surface = classify_surface(events)
    documents: list = []
    for event in events:
        text = extract_event_text(event)
        if text == "":
            continue
        documents.append({
            "sessionId": session_id,
            "seq": event.get("seq"),
            "type": event.get("type"),
            "time": event.get("time"),
            "surface": surface.get(event.get("seq"), "log-only"),
            "text": text,
        })
    return documents
