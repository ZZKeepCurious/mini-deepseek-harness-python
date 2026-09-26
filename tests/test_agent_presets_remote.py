"""agentPresets Remote 面（list/read/select）验收。

对照上游 `packages/preset/agent-preset-registry/src/{index,types}.ts`：
`agentPresets` 命名空间的声明式 roster（`AgentPresetRoster`）、可读文档
（`AgentPresetDocument`）与首回合前选择（turnBoundary 锁 + durable
`agent-preset/selected`）。
"""
import os
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.preset import builtin_roster
from miniharness.web.api import WebApi


class AgentPresetsRemoteTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="agent-presets")
        self.addCleanup(self.ctx.dispose)
        self.work = tempfile.mkdtemp()
        self.api = WebApi(self.ctx, FakeLlmAdapter(), roster=builtin_roster())
        self.session_id = self._value(
            self.api.dispatch("session/create", "r0",
                              {"cwd": os.path.join(self.work, "work")}))["sessionId"]

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def _error(self, response):
        self.assertFalse(response["result"]["ok"])
        return response["result"]["error"]

    def _session(self):
        return self.api.store.get(self.session_id)

    # ---------- list ----------

    def test_list_returns_roster_with_is_default(self):
        value = self._value(self.api.dispatch("agentPresets/list", "l1", {}))
        self.assertTrue(value["modeSelectionEnabled"])
        ids = [row["id"] for row in value["presets"]]
        self.assertIn("standard", ids)
        self.assertIn("minimal", ids)
        standard = next(row for row in value["presets"] if row["id"] == "standard")
        self.assertTrue(standard["isDefault"])
        minimal = next(row for row in value["presets"] if row["id"] == "minimal")
        self.assertFalse(minimal["isDefault"])

    def test_list_unmounted_namespace(self):
        bare = Context(name="bare")
        self.addCleanup(bare.dispose)
        api = WebApi(bare, FakeLlmAdapter())
        error = self._error(api.dispatch("agentPresets/list", "l2", {}))
        self.assertEqual(error["code"], "gateway/invocation-unavailable")

    # ---------- read ----------

    def test_read_known_preset_document(self):
        value = self._value(self.api.dispatch(
            "agentPresets/read", "r1", {"agentPreset": "standard"}))
        self.assertEqual(value["agentPreset"], "standard")
        self.assertIsInstance(value["content"], str)

    def test_read_unknown_preset_not_found(self):
        error = self._error(self.api.dispatch(
            "agentPresets/read", "r2", {"agentPreset": "nope"}))
        self.assertEqual(error["code"], "agent-preset/not-found")
        self.assertEqual(error["details"]["agentPreset"], "nope")
        self.assertIn("standard", error["details"]["available"])

    # ---------- select ----------

    def test_select_before_first_turn_commits(self):
        value = self._value(self.api.dispatch(
            "agentPresets/select", "s1",
            {"sessionId": self.session_id, "agentPreset": "minimal"}))
        self.assertEqual(value, "minimal")
        # durable agent-preset/selected 落盘
        events = [ev["type"] for ev in self._session().events]
        self.assertIn("agent-preset/selected", events)

    def test_select_unknown_preset_not_found(self):
        error = self._error(self.api.dispatch(
            "agentPresets/select", "s2",
            {"sessionId": self.session_id, "agentPreset": "nope"}))
        self.assertEqual(error["code"], "agent-preset/not-found")

    def test_select_after_first_turn_locked(self):
        # 驱动一回合（落 turn/start）→ select 被锁
        self._value(self.api.dispatch(
            "session/prompt", "p1",
            {"requestId": "q1", "sessionId": self.session_id,
             "mode": "queue", "content": [{"type": "text", "text": "hi"}]}))
        error = self._error(self.api.dispatch(
            "agentPresets/select", "s3",
            {"sessionId": self.session_id, "agentPreset": "minimal"}))
        self.assertEqual(error["code"], "agent-preset/locked")
        self.assertEqual(error["details"]["sessionId"], self.session_id)
        self.assertEqual(error["details"]["agentPreset"], "minimal")

    def test_select_missing_session(self):
        error = self._error(self.api.dispatch(
            "agentPresets/select", "s4",
            {"sessionId": "nope", "agentPreset": "minimal"}))
        self.assertEqual(error["code"], "session/not-found")


class AgentPresetProjectionTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="preset-projection")
        self.addCleanup(self.ctx.dispose)
        from miniharness.core.session_store import install_sessions
        from miniharness.session_projection import install_session_projections
        install_sessions(self.ctx)
        self.registry = install_session_projections(self.ctx)
        from miniharness.preset import register_agent_preset_projection
        self.dispose = register_agent_preset_projection(self.registry)
        self.addCleanup(lambda: self.dispose() if self.dispose else None)
        self.session = self.ctx.get("sessions").create(
            "p1", {"meta": {"agentPreset": "standard"}})

    def test_init_from_header(self):
        value = self.registry.state_of(self.session, "agentPreset")
        self.assertEqual(value, "standard")

    def test_folds_selected_event(self):
        self.session.append("agent-preset/selected", {"agentPreset": "minimal"})
        value = self.registry.state_of(self.session, "agentPreset")
        self.assertEqual(value, "minimal")

    def test_wire_unit_visible_in_snapshot(self):
        values = self.registry.snapshot(self.session)["values"]
        self.assertIn("agentPreset", values)
        self.assertEqual(values["agentPreset"], "standard")


if __name__ == "__main__":
    unittest.main()