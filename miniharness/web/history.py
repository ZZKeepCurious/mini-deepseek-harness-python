"""Session 历史分页与地址校验（对齐 session-controller history.ts + controller history）。"""
from __future__ import annotations

from typing import Any

from ..core.session import thaw
from ..session_query.observation import SessionObservation
from .api import DEFAULT_MAX_MESSAGES, _Reject, _is_safe_int

__all__ = [
    "address_id",
    "history_page",
    "resolve_history_request",
    "validate_observation_address",
]


def address_id(address: dict) -> str:
    if address.get("kind") == "session":
        return address["sessionId"]
    return address["childSessionId"]


def _validate_history_window(max_messages: Any, turn_window: Any) -> None:
    if max_messages is not None and (not _is_safe_int(max_messages) or max_messages <= 0):
        raise _Reject("gateway/bad-request", "maxMessages must be a positive safe integer", {})
    if turn_window is None:
        return
    if not isinstance(turn_window, dict):
        raise _Reject("gateway/bad-request", "turnWindow must be an object", {})
    min_messages = turn_window.get("minMessages")
    min_turns = turn_window.get("minTurns")
    if (not _is_safe_int(min_messages) or min_messages <= 0
            or min_messages > (max_messages or DEFAULT_MAX_MESSAGES)):
        raise _Reject(
            "gateway/bad-request",
            "turnWindow.minMessages must be a positive safe integer no greater than maxMessages",
            {})
    if not _is_safe_int(min_turns) or min_turns <= 0:
        raise _Reject("gateway/bad-request",
                      "turnWindow.minTurns must be a positive safe integer", {})


def resolve_history_request(request: dict) -> dict:
    through_seq = request.get("throughSeq")
    if through_seq is not None and (
            not _is_safe_int(through_seq) or through_seq < -1):
        raise _Reject("gateway/bad-request",
                      "throughSeq must be an integer greater than or equal to -1", {})
    before_seq = request.get("beforeSeq")
    if before_seq is not None and (not _is_safe_int(before_seq) or before_seq < 0):
        raise _Reject("gateway/bad-request",
                      "beforeSeq must be a non-negative safe integer", {})
    max_messages = request.get("maxMessages")
    turn_window = request.get("turnWindow")
    _validate_history_window(max_messages, turn_window)
    return {
        "throughSeq": through_seq,
        "beforeSeq": before_seq,
        "maxMessages": max_messages or DEFAULT_MAX_MESSAGES,
        "turnWindow": turn_window,
    }


def validate_observation_address(address: dict, observation: SessionObservation) -> None:
    if observation.header.get("cwd") is None:
        _reject_not_found(address)
    header = observation.header
    inherited = observation.inherited_event_count
    projections = observation.projections
    if address.get("kind") == "session":
        if header.get("origin") == "subagent":
            raise _Reject(
                "session/agent-busy",
                "subagent Sessions require their durable parent address",
                {"reason": "use subagent delivery for this child session"})
        return
    parent_session_id = address.get("parentSessionId")
    child_session_id = address.get("childSessionId")
    if header.get("origin") != "subagent" or header.get("parentSession") != parent_session_id:
        raise _Reject(
            "subagent/unauthorized",
            "subagent does not belong to the supplied parent",
            {"childSessionId": child_session_id})
    identity = None
    if projections is not None:
        identity = (projections.get("values") or {}).get("subagent")
    if identity is None:
        raise _Reject(
            "subagent/catalog-diagnostic",
            "subagent descriptor is unavailable",
            {"parentSessionId": parent_session_id,
             "childSessionId": child_session_id,
             "reason": "unsupported"})
    if isinstance(identity, dict):
        seq = identity.get("seq")
        if seq is not None and seq < inherited:
            raise _Reject(
                "subagent/catalog-diagnostic",
                "subagent descriptor is unavailable",
                {"parentSessionId": parent_session_id,
                 "childSessionId": child_session_id,
                 "reason": "unsupported"})
    mode = address.get("mode")
    if mode not in (None, "unknown") and isinstance(identity, dict):
        if identity.get("mode") != mode:
            raise _Reject(
                "subagent/unauthorized",
                "subagent mode does not match the supplied address",
                {"childSessionId": child_session_id})


def _reject_not_found(address: dict) -> None:
    if address.get("kind") == "session":
        session_id = address.get("sessionId", "")
        raise _Reject("session/not-found", f'session "{session_id}" not found',
                      {"sessionId": session_id})
    raise _Reject("subagent/not-found", "subagent is unavailable", {
        "parentSessionId": address.get("parentSessionId"),
        "childSessionId": address.get("childSessionId"),
    })


def history_page(observation: SessionObservation, spec: dict, paginate) -> dict:
    through_seq = spec["throughSeq"]
    if through_seq is None:
        cursor = observation.cursor
    elif through_seq == -1:
        cursor = -1
    else:
        cursor = through_seq
    if cursor > observation.cursor:
        raise _Reject(
            "gateway/bad-request",
            f"session history cut {cursor} exceeds {observation.cursor}",
            {})
    page_events, has_more = paginate(
        observation.events,
        spec["beforeSeq"],
        spec["maxMessages"],
        cursor,
        spec["turnWindow"],
    )
    records = [{"type": "event", "event": thaw(event)} for event in page_events]
    return {"cursor": cursor, "records": records, "hasMore": has_more}
