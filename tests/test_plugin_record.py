import unittest

from miniharness.core.session import Session
from miniharness.core.session.plugin_record import (
    append_plugin_record,
    is_plugin_record_type,
    plugin_record_of,
)
from miniharness.session_projection import install_session_projections


class PluginRecordTest(unittest.TestCase):
    def test_grammar_and_round_trip(self):
        self.assertTrue(is_plugin_record_type("plugin:action-cards/decision"))
        self.assertFalse(is_plugin_record_type("action-cards/decision"))
        session = Session(session_id="s1", meta={"id": "s1"})
        seq = append_plugin_record(session, "plugin:action-cards/decision", {
            "callId": "c1",
            "action": "demo",
            "phase": "dismissed",
        })
        self.assertEqual(seq, 0)
        record = plugin_record_of(session.events[0])
        self.assertIsNotNone(record)
        self.assertEqual(record["type"], "plugin:action-cards/decision")
        self.assertEqual(record["data"]["phase"], "dismissed")

    def test_projection_sees_record(self):
        from miniharness.core.scope import Context
        from miniharness.experimental.action_cards.projection import action_card_projection

        root = Context()
        registry = install_session_projections(root)
        registry.register(action_card_projection)
        session = Session(session_id="s2", meta={"id": "s2"})
        root.emit("session/created", {"session": session})
        append_plugin_record(session, "plugin:action-cards/decision", {
            "callId": "call-1",
            "action": "install",
            "phase": "dismissed",
        })
        for event in session.events:
            root.emit("session/event", {"session": session, "event": event})
        state = registry.state_of(session, "actionCards")
        self.assertIn("call-1", state["view"]["decisions"])
        self.assertEqual(state["pendingStatus"]["call-1"], 0)
