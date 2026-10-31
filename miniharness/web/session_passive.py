"""Session 被动读：events / state / wait（对齐 session-controller alpha.2）。"""
from __future__ import annotations

import time
from typing import Any

DEFAULT_MAX_MESSAGES = 50
DEFAULT_WAIT_TIMEOUT_MS = 30_000
_MAX_TIMER_DELAY_MS = 2_147_483_647


def _reject():
    from .api import _Reject
    return _Reject


def events_page(api, payload: dict) -> dict:
    return api.page(payload)


def read_session_state(api, session_id: str, *, retried: bool = False) -> dict | None:
    """被动 state 采样；live attach 竞态时首次返回 None，调用方应 retried=True 重读。"""
    from ..session_query.config import SessionQueryError
    from ..session_query.observation import observe_session

    Reject = _reject()
    agent = api._agents.get(session_id)
    if agent is not None:
        session = agent.session
        header = api._wire_header(session)
        if header.get("cwd") is None:
            raise Reject("session/not-found", f'session "{session_id}" not found',
                         {"sessionId": session_id, "kind": "session"})
        return {
            "cursor": session.seq - 1,
            "active": True,
            "running": agent.status == "running",
            "queue": _queue_items(agent),
            "pendingInput": _pending_input(api, session_id),
        }
    try:
        with observe_session(api.ctx, session_id, projection=False) as observation:
            if not retried and observation.source == "live":
                return None
            if observation.header.get("cwd") is None:
                raise Reject("session/not-found", f'session "{session_id}" not found',
                             {"sessionId": session_id, "kind": "session"})
            return {
                "cursor": observation.cursor,
                "active": False,
                "running": False,
                "queue": None,
                "pendingInput": _pending_input(api, session_id),
            }
    except SessionQueryError as error:
        if error.code == "SESSION_QUERY_SESSION_NOT_FOUND":
            raise Reject("session/not-found", f'session "{session_id}" not found',
                         {"sessionId": session_id}) from error
        raise


def read_session_state_resolved(api, session_id: str) -> dict:
    state = read_session_state(api, session_id)
    if state is None:
        state = read_session_state(api, session_id, retried=True)
    if state is None:
        Reject = _reject()
        raise Reject("session/not-found", f'session "{session_id}" not found',
                     {"sessionId": session_id})
    return state


def assistant_output(api, session_id: str, after_seq: int, through_seq: int,
                     max_messages: int) -> dict:
    from .api import thaw
    from ..session_query.config import SessionQueryError
    from ..session_query.observation import observe_session

    Reject = _reject()
    try:
        with observe_session(api.ctx, session_id, projection=False) as observation:
            if through_seq > observation.cursor:
                raise Reject("gateway/internal",
                             "Session history no longer covers the wait cursor", {})
            events = observation.events
    except SessionQueryError as error:
        if error.code == "SESSION_QUERY_SESSION_NOT_FOUND":
            raise Reject("session/not-found", f'session "{session_id}" not found',
                         {"sessionId": session_id}) from error
        raise
    records = []
    index = through_seq
    while index > after_seq:
        if index < 0 or index >= len(events):
            break
        event = events[index]
        if event.get("type") == "assistant/message" and event.get("surfaceOp") == "append":
            if len(records) == max_messages:
                return {"records": records, "truncated": True}
            records.append({"type": "event", "event": thaw(event)})
        index -= 1
    return {"records": records, "truncated": False}


def wait_session(api, payload: dict) -> dict:
    from .api import _is_safe_int
    from .state_listener import StateListenerSync

    Reject = _reject()
    session_id = payload.get("sessionId")
    if not isinstance(session_id, str) or session_id == "":
        raise Reject("gateway/bad-request", "sessionId must not be empty", {})
    timeout_ms = payload.get("timeoutMs")
    if timeout_ms is None:
        timeout_ms = DEFAULT_WAIT_TIMEOUT_MS
    after_seq = payload.get("afterSeq")
    if after_seq is None:
        after_seq = -1
    max_messages = payload.get("maxMessages")
    if max_messages is None:
        max_messages = DEFAULT_MAX_MESSAGES
    if not _is_safe_int(after_seq) or after_seq < -1:
        raise Reject("gateway/bad-request",
                     "afterSeq must be an integer greater than or equal to -1", {})
    if not _is_safe_int(max_messages) or max_messages <= 0:
        raise Reject("gateway/bad-request", "maxMessages must be a positive safe integer", {})
    if (not _is_safe_int(timeout_ms) or timeout_ms <= 0
            or timeout_ms > _MAX_TIMER_DELAY_MS):
        raise Reject("gateway/bad-request",
                     "timeoutMs must be a positive supported timer delay", {})

    def _read() -> dict:
        return read_session_state_resolved(api, session_id)

    listener = StateListenerSync(api, session_id, _read, refresh_ms=1_000)
    listener.start()
    deadline = time.monotonic() + timeout_ms / 1000.0
    current_state: dict | None = None
    reason: str = "timeout"
    states = listener.iter_versioned_states()
    try:
        for versioned in states:
            if time.monotonic() >= deadline:
                break
            if not listener.is_current(versioned["revision"]):
                continue
            current_state = versioned["state"]
            action = _wait_action(current_state)
            if action == "continue":
                continue
            if action in ("input-required", "idle"):
                reason = action
                break
            agent = api._agents.get(session_id)
            session = api.store.get(session_id)
            if agent is None or session is None or agent.session is not session:
                continue
            while time.monotonic() < deadline and not agent.when_idle():
                time.sleep(0.05)
            if not listener.is_current(versioned["revision"]):
                continue
            if api._agents.get(session_id) is not agent or api.store.get(session_id) is not session:
                continue
            fresh = _read()
            current_state = fresh
            reason = ("input-required" if _wait_action(fresh) == "input-required"
                      else "idle")
            break
        else:
            if current_state is None and not listener.closed:
                raise Reject(
                    "session/observation-ended",
                    "Session observation ended before a stopping state", {})
    finally:
        listener.close()

    if current_state is None:
        raise Reject(
            "session/wait-timeout",
            "No Session state arrived before the wait deadline", {})
    output = assistant_output(
        api, session_id, after_seq, current_state["cursor"], max_messages)
    return {"reason": reason, "state": current_state, "output": output}


def _wait_action(state: dict) -> str:
    if state["pendingInput"]:
        return "input-required"
    if state["running"]:
        return "continue"
    if not state["active"]:
        return "idle"
    return "check-idle"


def _pending_input(api, session_id: str) -> list[str]:
    kinds: set[str] = set()
    for item in api.gateway.get_pending_remote_events():
        if str(item["agentId"]) != session_id:
            continue
        event = item["event"]
        if event == "approval/request":
            kinds.add("approval")
        elif event == "user-questions/request":
            kinds.add("question")
        else:
            kinds.add("other")
    return sorted(kinds)


def _queue_items(agent) -> list[dict]:
    rows = []

    def item(message: dict, placement: str) -> dict:
        source = message.get("source") or {}
        row = {
            "id": message["id"],
            "placement": placement,
            "content": message.get("content") or [],
        }
        if source.get("kind") == "user" and isinstance(source.get("rpcId"), str):
            row["rpcId"] = source["rpcId"]
        return row

    for message in getattr(agent.inbox, "next_turn", []):
        rows.append(item(message, "queued"))
    for message in getattr(agent.inbox, "next_step", []):
        source = message.get("source") or {}
        placement = "steering" if source.get("kind") == "user" else "context"
        rows.append(item(message, placement))
    return rows
