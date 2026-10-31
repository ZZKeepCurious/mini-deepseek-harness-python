"""action-cards Host 面（experimental，对齐 packages/experimental/action-cards 子集）。"""
from __future__ import annotations

import json
from typing import Any

from ...core.scope import Context, Service
from ...core.session.message import create_message, text_block
from ...core.session.plugin_record import append_plugin_record
from .projection import (
    PLUGIN_DECISION,
    STATUS_CLOSING,
    STATUS_PREAMBLE,
    action_card_projection,
)

__all__ = [
    "ActionCards",
    "install_action_cards",
]


class ActionCards(Service):
    provide = "actionCards"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "actionCards")
        self._unregister_projection = None

    def start(self) -> None:
        registry = self.ctx.get("sessionProjections")
        if registry is None:
            raise RuntimeError("action-cards requires sessionProjections on ctx")
        self._unregister_projection = registry.register(action_card_projection)
        self.ctx.on("agent/pre-step", self._on_pre_step, prepend=True)

    def record(self, agent: Any, decision: dict) -> None:
        """记录卡片决策（plugin:action-cards/decision）。"""
        for key in ("callId", "action", "phase"):
            if not isinstance(decision.get(key), str) or not decision[key]:
                raise ValueError(f"action card decision missing {key}")
        if decision.get("phase") == "replaced":
            raise ValueError('action-cards: phase "replaced" is derived from the log')
        append_plugin_record(agent.session, PLUGIN_DECISION, dict(decision))

    def view(self, session: Any) -> dict:
        registry = self.ctx.get("sessionProjections")
        if registry is None:
            return {"proposals": {}, "decisions": {}}
        state = registry.state_of(session, "actionCards")
        if state is None:
            return {"proposals": {}, "decisions": {}}
        return state["view"]

    async def _on_pre_step(self, payload: dict, next_fn) -> dict:
        decision = await next_fn()
        if not isinstance(decision, dict) or decision.get("kind") == "reject":
            return decision
        signal = payload.get("signal")
        if signal is not None and getattr(signal, "aborted", False):
            return decision
        agent = payload.get("agent")
        if agent is None:
            return decision
        registry = self.ctx.get("sessionProjections")
        if registry is None:
            return decision
        state = registry.state_of(agent.session, "actionCards")
        if state is None:
            return decision
        pending = state.get("pendingStatus") or {}
        if not pending:
            return decision
        receipts = [
            state["view"]["decisions"][call_id]
            for call_id in pending
            if call_id in state["view"]["decisions"]
        ]
        if not receipts:
            return decision
        through = max(pending.values())
        updates = []
        for receipt in receipts:
            proposal = state["view"]["proposals"].get(receipt["callId"])
            item = {
                "action": receipt["action"],
                "callId": receipt["callId"],
                "phase": receipt["phase"],
                "title": proposal.get("title") if proposal else None,
            }
            if "data" in receipt:
                item["data"] = receipt["data"]
            updates.append(item)
        text = " ".join([STATUS_PREAMBLE, STATUS_CLOSING]) + "\n" + json.dumps(updates, ensure_ascii=False)
        notice = create_message(
            "user",
            [text_block(text)],
            {
                "kind": "action-cards",
                "through": through,
                "form": "notice",
                "summary": "Action card status updated",
            },
        )
        messages = list(decision.get("messages") or [])
        return {**decision, "messages": [*messages, notice]}


def install_action_cards(ctx: Context) -> ActionCards:
    existing = ctx.get("actionCards")
    if existing is not None:
        return existing
    service = ActionCards(ctx)
    service.start()
    return service
