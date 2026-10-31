"""被动事件订阅 helpers（对齐 session-controller history/listener.ts eventItem）。"""
from __future__ import annotations

from typing import Any, Mapping

from .api import _Reject, thaw

__all__ = ["expand_created_backfill", "emit_event_frame"]


def emit_event_frame(event: Mapping) -> dict:
    return {"type": "event", "event": thaw(event)}


def expand_created_backfill(session: Any, next_seq: int | None) -> tuple[list[dict], int | None]:
    """session/created 通知：回放 [start, end) 区间内尚未推送的事件。"""
    events = list(session.events)
    end = session.seq
    if next_seq is None:
        start = events[0]["seq"] if events else 0
    else:
        start = next_seq
    if start >= end:
        return [], next_seq
    frames: list[dict] = []
    expected = next_seq
    for event in events:
        seq = event["seq"]
        if seq < start:
            continue
        if seq >= end:
            break
        if expected is not None and seq < expected:
            continue
        if expected is not None and seq != expected:
            raise _Reject(
                "gateway/internal",
                f"session event stream skipped seq {expected}",
                {})
        frames.append(emit_event_frame(event))
        expected = seq + 1
    return frames, expected
