"""AgentLoop 拥有的会话投影单元（turnBoundary + inbox）验收。

对齐上游 `packages/core/agent-loop/src/{index,inbox}.ts`：turnBoundary
（host-only，stateVersion 2）、inbox（wire，stateVersion 1）；AgentLoop
构造在有 `sessionProjections` 时注册两单元。
"""
import unittest

from miniharness.core.agent_loop.projections import (
    TURN_BOUNDARY_STATE_VERSION,
    inbox_projection,
    turn_boundary_projection,
)
from miniharness.core.scope import Context
from miniharness.core.session_store import install_sessions
from miniharness.session_projection import install_session_projections


class TestTurnBoundaryProjection(unittest.TestCase):
    def setUp(self):
        self.proj = turn_boundary_projection()
        self.state = self.proj.init(None, 0)

    def test_contract(self):
        self.assertEqual(self.proj.key, "turnBoundary")
        self.assertEqual(self.proj.state_version, TURN_BOUNDARY_STATE_VERSION)
        self.assertIsNone(self.proj.view)

    def test_init_shape(self):
        self.assertEqual(self.state, {
            "openTurnStartSeq": None,
            "lastStepStartSeq": None,
            "lastStepBoundary": None,
            "lastTurn": 0,
        })

    def test_turn_and_step_boundaries(self):
        self.state = self.proj.apply(self.state, {"type": "turn/start", "seq": 3,
                                                  "data": {"turn": 1}})
        self.assertEqual(self.state["openTurnStartSeq"], 3)
        self.assertEqual(self.state["lastTurn"], 1)
        self.state = self.proj.apply(self.state, {"type": "step/start", "seq": 4})
        self.assertEqual(self.state["lastStepStartSeq"], 4)
        self.assertEqual(self.state["lastStepBoundary"], {"kind": "start", "seq": 4})
        self.state = self.proj.apply(self.state, {"type": "step/end", "seq": 5})
        self.assertEqual(self.state["lastStepBoundary"], {"kind": "end", "seq": 5})
        self.state = self.proj.apply(self.state, {"type": "turn/end", "seq": 6,
                                                 "data": {"turn": 1}})
        self.assertIsNone(self.state["openTurnStartSeq"])

    def test_unrelated_event_returns_same_reference(self):
        self.assertIs(self.state,
                      self.proj.apply(self.state, {"type": "user/message", "seq": 1}))


class TestInboxProjection(unittest.TestCase):
    def setUp(self):
        self.proj = inbox_projection()
        self.state = self.proj.init(None, 0)

    def test_contract(self):
        self.assertEqual(self.proj.key, "inbox")
        self.assertEqual(self.proj.state_version, 1)
        self.assertEqual(self.proj.view(self.state), self.state)

    def test_splice_append_and_claim(self):
        message = {"id": "m1", "role": "user", "content": [], "source": {"kind": "user"}}
        self.state = self.proj.apply(self.state, {
            "type": "agent/inbox/spliced", "seq": 1,
            "data": {"target": "next-turn", "start": 0, "inserted": [message]}})
        self.assertEqual(self.state["next-turn"], [message])
        self.state = self.proj.apply(self.state, {
            "type": "agent/inbox/spliced", "seq": 2,
            "data": {"target": "next-turn", "start": 0, "removedCount": 1,
                     "inserted": []}})
        self.assertEqual(self.state["next-turn"], [])

    def test_next_step_keeps_other_queue(self):
        turn_msg = {"id": "t", "role": "user", "content": [], "source": {"kind": "user"}}
        step_msg = {"id": "s", "role": "user", "content": [], "source": {"kind": "user"}}
        self.state = self.proj.apply(self.state, {
            "type": "agent/inbox/spliced", "seq": 1,
            "data": {"target": "next-turn", "start": 0, "inserted": [turn_msg]}})
        self.state = self.proj.apply(self.state, {
            "type": "agent/inbox/spliced", "seq": 2,
            "data": {"target": "next-step", "start": 0, "inserted": [step_msg]}})
        self.assertEqual(self.state["next-turn"], [turn_msg])
        self.assertEqual(self.state["next-step"], [step_msg])

    def test_duplicate_id_rejected(self):
        message = {"id": "m1", "role": "user", "content": [], "source": {"kind": "user"}}
        self.state = self.proj.apply(self.state, {
            "type": "agent/inbox/spliced", "seq": 1,
            "data": {"target": "next-turn", "start": 0, "inserted": [message]}})
        with self.assertRaises(ValueError):
            self.proj.apply(self.state, {
                "type": "agent/inbox/spliced", "seq": 2,
                "data": {"target": "next-step", "start": 0, "inserted": [message]}})

    def test_out_of_bounds_splice_rejected(self):
        with self.assertRaises(ValueError):
            self.proj.apply(self.state, {
                "type": "agent/inbox/spliced", "seq": 1,
                "data": {"target": "next-turn", "start": 5, "inserted": []}})

    def test_unknown_target_rejected(self):
        with self.assertRaises(ValueError):
            self.proj.apply(self.state, {
                "type": "agent/inbox/spliced", "seq": 1,
                "data": {"target": "nope", "start": 0, "inserted": []}})

    def test_unrelated_event_returns_same_reference(self):
        self.assertIs(self.state,
                      self.proj.apply(self.state, {"type": "user/message", "seq": 1}))


class TestAgentLoopRegistersProjections(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        self.store = install_sessions(self.ctx)
        self.registry = install_session_projections(self.ctx)
        self.session = self.store.create("s1", {"meta": {}})

    def tearDown(self):
        self.ctx.dispose()

    def _loop(self):
        from miniharness.core.agent_loop import AgentLoop
        from miniharness.llm import FakeLlmAdapter
        from miniharness.core.tools import ToolRegistry
        return AgentLoop(self.session, FakeLlmAdapter(),
                         ToolRegistry(self.ctx), self.ctx)

    def test_constructor_registers_both_units(self):
        loop = self._loop()
        self.assertIsNotNone(self.registry.state_of(self.session, "turnBoundary"))
        self.assertIsNotNone(self.registry.state_of(self.session, "inbox"))
        self.assertEqual(
            self.registry.state_of(self.session, "turnBoundary")["lastTurn"], 0)
        self.assertEqual(
            self.registry.state_of(self.session, "inbox"), {"next-turn": [], "next-step": []})
        loop.dispose()

    def test_registration_removed_on_dispose(self):
        loop = self._loop()
        loop.dispose()
        self.assertIsNone(self.registry.state_of(self.session, "turnBoundary"))
        self.assertIsNone(self.registry.state_of(self.session, "inbox"))

    def test_inbox_wire_unit_visible_in_snapshot(self):
        loop = self._loop()
        values = self.registry.snapshot(self.session)["values"]
        self.assertIn("inbox", values)
        self.assertNotIn("turnBoundary", values)
        loop.dispose()


if __name__ == "__main__":
    unittest.main()