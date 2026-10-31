import asyncio
import unittest

from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.experimental.action_cards import install_action_cards
from miniharness.session_projection import install_session_projections


class _Agent:
    def __init__(self, session: Session):
        self.session = session


class ActionCardsTest(unittest.IsolatedAsyncioTestCase):
    async def test_pre_step_emits_action_cards_notice(self):
        ctx = Context()
        install_session_projections(ctx)
        cards = install_action_cards(ctx)
        self.addCleanup(lambda: cards._unregister_projection() if cards._unregister_projection else None)
        session = Session(session_id="ac1", meta={"id": "ac1"})
        ctx.emit("session/created", {"session": session})
        agent = _Agent(session)
        cards.record(agent, {
            "callId": "tool-call-1",
            "action": "demo-action",
            "phase": "dismissed",
        })
        for event in session.events:
            ctx.emit("session/event", {"session": session, "event": event})

        async def next_fn():
            return {"kind": "enter", "messages": []}

        class _Signal:
            aborted = False

        result = await cards._on_pre_step({"agent": agent, "signal": _Signal()}, next_fn)
        self.assertEqual(len(result["messages"]), 1)
        source = result["messages"][0]["source"]
        self.assertEqual(source["kind"], "action-cards")
        self.assertEqual(source["form"], "notice")
        self.assertEqual(source["through"], 0)
