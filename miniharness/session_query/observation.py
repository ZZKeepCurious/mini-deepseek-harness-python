"""Session 精确读租约（对齐 upstream `ctx.sessionQuery.observeSession`）。

live store 优先；否则经 `ctx.sessionPersistence.inspect` 读 prepared 日志。
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator

from ..core.session import SESSION_FORMAT_VERSION
from .config import SessionQueryError

__all__ = ["SessionObservation", "observe_session"]


def _inherited_event_count(events: list) -> int:
    for index, event in enumerate(events):
        if event.get("type") == "session/end-seed":
            return index + 1
    return 0


def _cursor_of(events: list) -> int:
    return events[-1]["seq"] if events else -1


def _wire_header_live(session: Any) -> dict:
    meta = dict(getattr(session, "meta", {}) or {})
    header: dict[str, Any] = {
        "version": SESSION_FORMAT_VERSION,
        "id": session.session_id,
        "createdAt": getattr(session, "created_at", None),
        "isSeeded": bool(getattr(session, "is_seeded", False)),
    }
    if meta.get("cwd") is not None:
        header["cwd"] = meta["cwd"]
    if meta.get("parentSession") is not None:
        header["parentSession"] = meta["parentSession"]
    if meta.get("origin") is not None:
        header["origin"] = meta["origin"]
    if meta.get("delegationDepth") is not None:
        header["delegationDepth"] = meta["delegationDepth"]
    if meta.get("agentPreset") is not None:
        header["agentPreset"] = meta["agentPreset"]
    return header


def _wire_header_prepared(session_id: str, meta: dict | None, created_at: Any) -> dict:
    meta = dict(meta or {})
    header: dict[str, Any] = {
        "version": SESSION_FORMAT_VERSION,
        "id": session_id,
        "createdAt": created_at,
        "isSeeded": bool(meta.get("isSeeded")),
    }
    if meta.get("cwd") is not None:
        header["cwd"] = meta["cwd"]
    if meta.get("parentSession") is not None:
        header["parentSession"] = meta["parentSession"]
    if meta.get("origin") is not None:
        header["origin"] = meta["origin"]
    if meta.get("delegationDepth") is not None:
        header["delegationDepth"] = meta["delegationDepth"]
    if meta.get("agentPreset") is not None:
        header["agentPreset"] = meta["agentPreset"]
    return header


@dataclass
class SessionObservation:
    header: dict
    events: list
    cursor: int
    inherited_event_count: int
    source: str
    projections: dict | None
    _live_session: Any | None = None
    _retain_handle: Any | None = None

    def retain(self) -> "SessionObservation":
        if self.source != "prepared":
            raise SessionQueryError(
                "only prepared observations can be retained for promotion",
                "SESSION_QUERY_INVALID_LEASE")
        clone = SessionObservation(
            header=dict(self.header),
            events=list(self.events),
            cursor=self.cursor,
            inherited_event_count=self.inherited_event_count,
            source=self.source,
            projections=self.projections,
            _live_session=self._live_session,
            _retain_handle=self,
        )
        self._retain_handle = clone
        return clone

    def close(self) -> None:
        self._retain_handle = None
        self._live_session = None


@contextmanager
def observe_session(ctx, session_id: str, *, projection: bool = False) -> Iterator[SessionObservation]:
    if not isinstance(session_id, str) or session_id == "":
        raise SessionQueryError("sessionId must not be empty", "SESSION_QUERY_SESSION_NOT_FOUND")

    store = ctx.get("sessions")
    session = store.get(session_id) if store is not None else None
    if session is not None:
        events = list(session.events)
        observation = SessionObservation(
            header=_wire_header_live(session),
            events=events,
            cursor=_cursor_of(events),
            inherited_event_count=_inherited_event_count(events),
            source="live",
            projections=None,
            _live_session=session,
        )
        try:
            yield observation
        finally:
            observation.close()
        return

    persistence = ctx.get("sessionPersistence")
    if persistence is None:
        raise SessionQueryError(
            f"session {session_id} not found", "SESSION_QUERY_SESSION_NOT_FOUND")

    status = None
    if hasattr(persistence, "format_status"):
        status = persistence.format_status(session_id)
    if status is None and not hasattr(persistence, "inspect"):
        raise SessionQueryError(
            f"session {session_id} not found", "SESSION_QUERY_SESSION_NOT_FOUND")

    try:
        inspected = persistence.inspect(session_id)
    except FileNotFoundError as error:
        raise SessionQueryError(
            f"session {session_id} not found", "SESSION_QUERY_SESSION_NOT_FOUND") from error

    events = list(inspected.get("events") or [])
    if not events:
        raise SessionQueryError(
            f"session {session_id} not found", "SESSION_QUERY_SESSION_NOT_FOUND")

    meta = inspected.get("meta") if isinstance(inspected.get("meta"), dict) else {}
    created_at = meta.get("createdAt")
    observation = SessionObservation(
        header=_wire_header_prepared(session_id, meta, created_at),
        events=events,
        cursor=_cursor_of(events),
        inherited_event_count=_inherited_event_count(events),
        source="prepared",
        projections=None,
    )
    try:
        yield observation
    finally:
        observation.close()

