"""actionCards 会话投影（对齐 packages/experimental/action-cards/src/projection.ts）。"""
from __future__ import annotations

import json
from typing import Any

from ...core.session.json import thaw
from ...core.session.plugin_record import plugin_record_of
from ...session_projection import ProjectionDefinition

PROPOSE_ACTION = "propose_action"
PLUGIN_DECISION = "plugin:action-cards/decision"

STATUS_PREAMBLE = (
    "Latest status of action cards from user actions and background work. "
    "These are status facts, not a request to start or retry work. Use these receipts "
    "instead of the earlier proposal when describing progress."
)
STATUS_CLOSING = "Continue with the user’s current request."


def _init_state(_header: dict, inherited_event_count: int) -> dict:
    return {
        "inheritedEventCount": inherited_event_count,
        "view": {"proposals": {}, "decisions": {}},
        "calls": {},
        "admittedTurn": None,
        "pendingStatus": {},
        "queued": 0,
    }


def _apply(state: dict, event: dict) -> dict:
    seq = event.get("seq", -1)
    if seq < state["inheritedEventCount"]:
        return state
    event_type = event.get("type")
    data = event.get("data") or {}
    if event_type == "turn/start":
        if not state["calls"]:
            return state
        return {**state, "calls": {}}
    if event_type == "agent/inbox/spliced":
        if data.get("target") != "next-turn":
            return state
        removed = data.get("removedCount") or 0
        inserted = data.get("inserted") or []
        queued = max(0, state["queued"] - removed + len(inserted))
        return {**state, "queued": queued}
    if event_type == "user/message":
        source = data.get("source") if isinstance(data, dict) else None
        if not isinstance(source, dict) or source.get("kind") != "action-cards":
            return state
        through = source.get("through")
        pending = {
            key: value for key, value in state["pendingStatus"].items()
            if value > through
        }
        return {**state, "pendingStatus": pending}
    if event_type == "tool/call":
        if data.get("name") != PROPOSE_ACTION:
            return state
        try:
            raw = json.loads(data.get("arguments") or "{}")
        except json.JSONDecodeError:
            return state
        for key in ("action", "title", "brief", "details"):
            if key not in raw:
                return state
        if not str(raw.get("title", "")).strip() or not str(raw.get("brief", "")).strip():
            return state
        call_id = data.get("callId")
        if not call_id:
            return state
        call = {
            "turn": data.get("turn"),
            "action": raw["action"],
            "title": raw["title"],
            "brief": raw["brief"],
            "details": raw["details"],
        }
        return {**state, "calls": {**state["calls"], call_id: call}}
    if event_type == "tool/result":
        message = data.get("message") or {}
        call_id = message.get("toolCallId")
        if call_id not in state["calls"]:
            return state
        call = state["calls"][call_id]
        calls = {key: value for key, value in state["calls"].items() if key != call_id}
        if message.get("isError") is True or state["admittedTurn"] == call["turn"]:
            return {**state, "calls": calls}
        decisions = dict(state["view"]["decisions"])
        for earlier in state["view"]["proposals"].values():
            if earlier["callId"] not in decisions:
                decisions[earlier["callId"]] = {
                    "callId": earlier["callId"],
                    "action": earlier["action"],
                    "phase": "replaced",
                }
        proposal = {"callId": call_id, **call}
        return {
            **state,
            "calls": calls,
            "admittedTurn": call["turn"],
            "view": {
                "proposals": {**state["view"]["proposals"], call_id: proposal},
                "decisions": decisions,
            },
        }
    record = plugin_record_of(event)
    if record is None or record["type"] != PLUGIN_DECISION:
        return state
    decision = thaw(record["data"])
    if not isinstance(decision, dict):
        return state
    for key in ("callId", "action", "phase"):
        if not isinstance(decision.get(key), str) or not decision[key]:
            return state
    call_id = decision["callId"]
    view = {
        "proposals": state["view"]["proposals"],
        "decisions": {**state["view"]["decisions"], call_id: decision},
    }
    pending = {**state["pendingStatus"], call_id: seq}
    return {**state, "view": view, "pendingStatus": pending}


def action_cards_view(state: dict) -> dict:
    return state["view"]


action_card_projection = ProjectionDefinition(
    "actionCards",
    init=_init_state,
    apply=_apply,
    state_version=2,
    view=action_cards_view,
)
