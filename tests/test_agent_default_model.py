"""agent-default-model：部署默认模型选择服务（对齐 packages/core/agent-default-model）。"""
import os
import tempfile
import unittest
from types import SimpleNamespace

from miniharness.boot import boot
from miniharness.boot.config_editor import install_config_editor
from miniharness.boot.profile import (
    PROFILE_PATCH_FILENAME,
    init_profile,
    load_profile_directory,
    resolve_profile_dir,
)
from miniharness.core.agent_default_model import (
    AgentDefaultModel,
    install_agent_default_model,
)
from miniharness.core.scope import Context


class _FakeEditor:
    def __init__(self, entry):
        self.entry = entry
        self.calls = 0

    def edit(self, entry, change):
        self.calls += 1
        current = dict(entry.options.get("config") or {})
        entry.options["config"] = change(current, {})


class AgentDefaultModelTest(unittest.TestCase):
    def test_current_selection_omits_absent_effort(self):
        ctx = Context(name="test")
        service = AgentDefaultModel(ctx, provider="p", model="m")
        self.assertEqual(service.current_selection(),
                         {"provider": "p", "model": "m"})

    def test_current_selection_includes_effort_and_is_detached(self):
        ctx = Context(name="test")
        service = AgentDefaultModel(ctx, provider="p", model="m",
                                    reasoning_effort="high")
        selection = service.current_selection()
        self.assertEqual(selection,
                         {"provider": "p", "model": "m",
                          "reasoningEffort": "high"})
        selection["model"] = "changed"
        self.assertEqual(service.current_selection()["model"], "m")

    def test_config_required_fields_fail_loud(self):
        ctx = Context(name="test")
        with self.assertRaisesRegex(ValueError, "provider"):
            AgentDefaultModel(ctx, provider="", model="m")
        with self.assertRaisesRegex(ValueError, "model"):
            AgentDefaultModel(ctx, provider="p", model=None)
        with self.assertRaisesRegex(ValueError, "reasoningEffort"):
            AgentDefaultModel(ctx, provider="p", model="m",
                              reasoning_effort="")

    def test_save_without_editor_is_noop(self):
        ctx = Context(name="test")
        service = AgentDefaultModel(ctx, provider="p", model="m")
        service.save_selection({"provider": "p", "model": "next"})
        self.assertEqual(service.current_selection(),
                         {"provider": "p", "model": "m"})

    def test_save_updates_entry_and_selection(self):
        ctx = Context(name="test")
        entry = SimpleNamespace(options={"id": "agent-default-model",
                                         "config": {"provider": "p",
                                                    "model": "m"}})
        ctx.fiber.entry = entry
        editor = _FakeEditor(entry)
        ctx.provide("configEditor", editor)
        service = AgentDefaultModel(ctx, provider="p", model="m")
        service.save_selection({"provider": "p", "model": "next",
                                "reasoningEffort": "low"})
        self.assertEqual(entry.options["config"],
                         {"provider": "p", "model": "next",
                          "reasoningEffort": "low"})
        self.assertEqual(service.current_selection(),
                         {"provider": "p", "model": "next",
                          "reasoningEffort": "low"})

    def test_save_without_effort_removes_field(self):
        ctx = Context(name="test")
        entry = SimpleNamespace(options={"id": "agent-default-model",
                                         "config": {"provider": "p",
                                                    "model": "m",
                                                    "reasoningEffort": "high"}})
        ctx.fiber.entry = entry
        ctx.provide("configEditor", _FakeEditor(entry))
        service = AgentDefaultModel(ctx, provider="p", model="m",
                                    reasoning_effort="high")
        service.save_selection({"provider": "p", "model": "final"})
        self.assertEqual(entry.options["config"],
                         {"provider": "p", "model": "final"})
        self.assertNotIn("reasoningEffort", service.current_selection())

    def test_failed_save_does_not_block_later_saves(self):
        ctx = Context(name="test")
        entry = SimpleNamespace(options={"id": "agent-default-model",
                                         "config": {"provider": "p",
                                                    "model": "m"}})
        ctx.fiber.entry = entry
        failures = {"left": 1}

        class _FlakyEditor:
            def edit(self, entry, change):
                if failures["left"]:
                    failures["left"] -= 1
                    raise RuntimeError("write rejected")
                entry.options["config"] = change(
                    dict(entry.options.get("config") or {}), {})

        ctx.provide("configEditor", _FlakyEditor())
        service = AgentDefaultModel(ctx, provider="p", model="m")
        with self.assertRaisesRegex(RuntimeError, "write rejected"):
            service.save_selection({"provider": "p", "model": "rejected"})
        self.assertEqual(service.current_selection()["model"], "m")
        service.save_selection({"provider": "p", "model": "final"})
        self.assertEqual(service.current_selection()["model"], "final")

    def test_install_idempotent(self):
        ctx = Context(name="test")
        first = install_agent_default_model(ctx, provider="p", model="m")
        second = install_agent_default_model(ctx, provider="q", model="n")
        self.assertIs(first, second)
        self.assertEqual(second.current_selection(),
                         {"provider": "p", "model": "m"})


class AgentDefaultModelPersistenceTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.abspath(self._tmp.name)
        self.config_file = os.path.join(self.home, "cordis.yml")
        with open(self.config_file, "w", encoding="utf-8") as handle:
            handle.write(
                "plugins:\n"
                "  - id: agent-default-model\n"
                "    module: miniharness.cli.plugins.agent_default_model\n"
                "    config:\n"
                "      provider: test-provider\n"
                "      model: first\n")
        self.ctx, _ = boot(self.config_file)
        self.addCleanup(self.ctx.dispose)
        profile_dir = resolve_profile_dir("web", self.home)
        os.makedirs(profile_dir, exist_ok=True)
        init_profile(profile_dir, [])
        self.patch_path = os.path.join(profile_dir, PROFILE_PATCH_FILENAME)
        self.editor = install_config_editor(
            self.ctx, profile_dir=profile_dir, patch_path=self.patch_path,
            home=self.home)

    def _row(self):
        profile = load_profile_directory(
            "miniharness", os.path.dirname(self.patch_path))
        return next(p for p in profile.patches
                    if p.get("id") == "agent-default-model")

    def test_save_persists_profile_patch_and_reloads(self):
        service = self.ctx.get("agentDefaultModel")
        service.save_selection({"provider": "test-provider", "model": "second",
                                "reasoningEffort": "high"})
        self.assertEqual(self._row()["config"],
                         {"provider": "test-provider", "model": "second",
                          "reasoningEffort": "high"})
        self.assertEqual(self.ctx.get("agentDefaultModel").current_selection(),
                         {"provider": "test-provider", "model": "second",
                          "reasoningEffort": "high"})

    def test_save_without_effort_removes_patch_field(self):
        service = self.ctx.get("agentDefaultModel")
        service.save_selection({"provider": "test-provider", "model": "second",
                                "reasoningEffort": "high"})
        service.save_selection({"provider": "test-provider", "model": "third"})
        self.assertEqual(self._row()["config"],
                         {"provider": "test-provider", "model": "third"})
        self.assertNotIn(
            "reasoningEffort",
            self.ctx.get("agentDefaultModel").current_selection())


if __name__ == "__main__":
    unittest.main()
