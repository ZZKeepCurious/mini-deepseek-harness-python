"""time-context：时钟读数、浏览器时区策略与投影 fold。

对齐 packages/context/time-context（request-zone / timestamp / index 的确定性面）。
"""

import os
import re
import unittest

from miniharness.context.time_context import (
    NAME,
    create_timestamp_formatter,
    derive_browser_time_zone_context,
    format_duration,
    format_timestamp,
    install_time_context,
    render_browser_time_zone_context,
)
from miniharness.core.scope import Context
from miniharness.core.session.message import create_message, text_block
from miniharness.core.session_store import SessionStore
from miniharness.session_projection import install_session_projections

STAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}[+-]\d{2}:\d{2}\[[^\]]+\]$")


class TestFormatting(unittest.TestCase):
    def test_format_duration_compact_units(self):
        self.assertEqual(format_duration(0), "0s")
        self.assertEqual(format_duration(999), "0s")
        self.assertEqual(format_duration(1000), "1s")
        self.assertEqual(format_duration(61_000), "1m 1s")
        self.assertEqual(format_duration(3_661_000), "1h 1m 1s")
        self.assertEqual(format_duration(90_061_000), "1d 1h 1m 1s")
        self.assertEqual(format_duration(-5), "0s")

    def test_format_timestamp_is_iso_shaped_with_zone(self):
        tzinfo, zone = create_timestamp_formatter("Asia/Shanghai")
        text = format_timestamp(0, tzinfo, zone)
        self.assertTrue(STAMP.match(text), text)
        self.assertTrue(text.endswith("[Asia/Shanghai]"))
        self.assertIn("+08:00", text)

    def test_utc_offset_is_rendered_with_colon(self):
        tzinfo, zone = create_timestamp_formatter("UTC")
        text = format_timestamp(0, tzinfo, zone)
        self.assertIn("+00:00", text)
        self.assertTrue(text.endswith("[UTC]"))


class TestBrowserZone(unittest.TestCase):
    def _user(self, client_time_zone=None, rpc_id="r1"):
        source = {"kind": "user", "rpcId": rpc_id}
        if client_time_zone is not None:
            source["clientTimeZone"] = client_time_zone
        return create_message("user", [text_block("hi")], source)

    def test_resolved_mixed_and_missing(self):
        self.assertEqual(derive_browser_time_zone_context([self._user()]), {"kind": "missing"})
        self.assertEqual(derive_browser_time_zone_context([self._user("Asia/Shanghai")]),
                         {"kind": "resolved", "timeZone": "Asia/Shanghai"})
        mixed = derive_browser_time_zone_context(
            [self._user("Asia/Shanghai"), self._user("UTC")])
        self.assertEqual(mixed, {"kind": "mixed", "timeZones": ["Asia/Shanghai", "UTC"]})

    def test_rejects_noncanonical_or_invalid_zone(self):
        with self.assertRaises(TypeError):
            derive_browser_time_zone_context([self._user("Not/AZone")])
        with self.assertRaises(TypeError):
            derive_browser_time_zone_context([self._user("Shanghai")])

    def test_render_policy_lines(self):
        self.assertEqual(
            render_browser_time_zone_context({"kind": "resolved", "timeZone": "UTC"}),
            "Browser time zone for this request: UTC. "
            "Interpret otherwise-unqualified dates and times in this zone.")
        self.assertEqual(
            render_browser_time_zone_context({"kind": "mixed", "timeZones": ["UTC", "Asia/Tokyo"]}),
            'Browser time zone for this request: mixed ["UTC","Asia/Tokyo"]. '
            "Ask the user to clarify otherwise-unqualified dates and times.")
        self.assertEqual(
            render_browser_time_zone_context({"kind": "missing"}),
            "Browser time zone for this request: unavailable. "
            "Ask the user to clarify otherwise-unqualified dates and times.")


class TimeContextCase(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="time-context-test")
        self.addCleanup(self.ctx.dispose)
        self.projections = install_session_projections(self.ctx)
        self.store = SessionStore(self.ctx)
        self.session = self.store.create("s1", {"meta": {"cwd": os.getcwd()}})

    def _agent(self):
        return type("Agent", (), {"session": self.session, "id": "s1"})()

    def _pre_step(self, turn=1, step=1, messages=None):
        from miniharness.core.agent_loop.resident_loop import run_on_resident

        payload = {"messages": messages or [], "agent": self._agent(),
                   "turn": turn, "step": step, "signal": None}
        return run_on_resident(self.ctx.awaterfall("agent/pre-step", payload))

    def test_requires_projection_registry(self):
        bare = Context(name="no-projections")
        self.addCleanup(bare.dispose)
        with self.assertRaisesRegex(RuntimeError, "sessionProjections"):
            install_time_context(bare, {})

    def test_injects_three_line_reading_with_browser_zone(self):
        install_time_context(self.ctx, {"timeZone": "Asia/Shanghai"})
        user = create_message("user", [text_block("what time is it")],
                              {"kind": "user", "rpcId": "r1", "clientTimeZone": "Asia/Shanghai"})
        decision = self._pre_step(messages=[user])
        injected = decision["messages"][-1]
        self.assertEqual(injected["source"]["kind"], "plugin")
        self.assertEqual(injected["source"]["plugin"], NAME)
        self.assertEqual(injected["source"]["form"], "snapshot")
        text = injected["content"][0]["text"]
        self.assertIn("Time sampled while preparing turn 1, step 1:", text)
        self.assertIn("Browser time zone for this request: Asia/Shanghai.", text)
        self.assertIn("Elapsed since the preceding model-visible message: unavailable.", text)
        self.assertIn("[Asia/Shanghai]", text)

    def test_later_step_measures_from_preceding_step_context(self):
        install_time_context(self.ctx, {"timeZone": "UTC"})
        self.session.append("user/message", create_message(
            "user", [text_block("q")], {"kind": "user", "rpcId": "r1"})["content"][0],
            surfaceOp="append")
        self.session.append("turn/start", {"turn": 1})
        self.session.append("user/message", create_message(
            "user", [text_block("q")], {"kind": "user", "rpcId": "r1"}),
            surfaceOp="append")
        first = self._pre_step(turn=1, step=1, messages=[])
        self.assertTrue(first["messages"][-1]["content"][0]["text"].count("step 1"))
        # 记录首个读数后，第二步以同 turn 的 time-context 注入为基线
        injected = first["messages"][-1]
        self.session.append("user/message", injected, surfaceOp="append")
        second = self._pre_step(turn=1, step=2, messages=[])
        text = second["messages"][-1]["content"][0]["text"]
        self.assertIn("turn 1, step 2", text)
        self.assertIn("Elapsed since the preceding step context: ", text)
        self.assertNotIn("Elapsed since the preceding step context: unavailable.", text)

    def test_refresh_interval_suppresses_near_duplicate(self):
        install_time_context(self.ctx, {"timeZone": "UTC", "refreshIntervalMs": 3_600_000})
        first = self._pre_step()
        self.assertEqual(len(first["messages"]), 1)
        self.session.append("user/message", first["messages"][-1], surfaceOp="append")
        second = self._pre_step(step=2)
        self.assertEqual(second["messages"], [])

    def test_projection_fold_tracks_message_and_injection_times(self):
        install_time_context(self.ctx, {"timeZone": "UTC"})
        self.session.append("user/message", create_message(
            "user", [text_block("q")], {"kind": "user", "rpcId": "r1"}),
            surfaceOp="append")
        state = self.projections.state_of(self.session, "timeContext")
        self.assertIsNotNone(state["lastMessageTime"])
        self.assertIsNone(state["lastInjectionTime"])
        self.session.append("turn/start", {"turn": 1})
        self.session.append("user/message", create_message(
            "user", [text_block("t")],
            {"kind": "plugin", "plugin": NAME, "form": "snapshot", "sections": []}),
            surfaceOp="append")
        state = self.projections.state_of(self.session, "timeContext")
        self.assertIsNotNone(state["lastInjectionTime"])
        self.assertIsNotNone(state["lastTurnInjectionTime"])
        self.session.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
        state = self.projections.state_of(self.session, "timeContext")
        self.assertIsNone(state["lastTurnInjectionTime"])
        self.assertIsNotNone(state["lastInjectionTime"])


if __name__ == "__main__":
    unittest.main()
