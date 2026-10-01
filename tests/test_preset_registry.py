"""preset 声明式载体（对齐 packages/preset/agent-preset-registry）。"""
import tempfile
import unittest
from pathlib import Path

from miniharness.core.scope import Context
from miniharness.preset.registry import (
    AgentPresetRegistry,
    PresetDefinition,
    definition_composition,
    entry_list_problem,
    flatten_rows,
    install_agent_preset_registry,
)


class TestEntryListProblem(unittest.TestCase):
    def test_valid_rows(self):
        self.assertIsNone(entry_list_problem(
            [{"name": "dsh-tool-bash"},
             {"name": "g", "group": True, "config": [{"name": "x"}]}]))

    def test_rejects_non_list(self):
        self.assertEqual(entry_list_problem("nope"),
                         "the composition must be a top-level list of plugin rows")

    def test_rejects_non_map_row(self):
        self.assertEqual(entry_list_problem([42]),
                         'row 1 is not a plugin row (expected a map with a "name")')

    def test_rejects_unnamed(self):
        self.assertEqual(entry_list_problem([{"config": {}}]),
                         'row 1 names no plugin (a "name" string is required)')

    def test_rejects_bad_group_config(self):
        self.assertEqual(
            entry_list_problem([{"name": "g", "group": True, "config": "x"}]),
            "group row 1 must hold a list of plugin rows")


class TestDefinitionComposition(unittest.TestCase):
    def test_flat_rows(self):
        result = definition_composition([
            {"id": "a", "name": "dsh-tool-bash"},
            {"name": "dsh-tool-fs"},
        ])
        self.assertEqual(result, {"rows": [
            {"entryId": "a", "moduleName": "dsh-tool-bash", "enabled": True},
            {"entryId": None, "moduleName": "dsh-tool-fs", "enabled": True},
        ]})

    def test_group_inherits_disabled(self):
        result = definition_composition([
            {"name": "g", "group": True, "disabled": True,
             "config": [{"name": "child"}]},
        ])
        self.assertEqual(result, {"rows": [
            {"entryId": None, "moduleName": "child", "enabled": False},
        ]})

    def test_conditional_js_expr(self):
        result = definition_composition([
            {"name": "x", "disabled": {"__jsExpr": "process.platform === 'win32'"}},
        ])
        row = result["rows"][0]
        self.assertEqual(row["enabled"], "conditional")
        self.assertEqual(row["condition"], "process.platform === 'win32'")

    def test_broken_shape(self):
        result = definition_composition("nope")
        self.assertIn("broken", result)


class TestAgentPresetRegistry(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        self.addCleanup(self.ctx.dispose)

    def test_register_and_unregister(self):
        registry = AgentPresetRegistry(self.ctx)
        unregister = registry.register(PresetDefinition(
            id="standard", name="Standard",
            plugins=[{"id": "a", "name": "dsh-tool-bash"}]))
        self.assertEqual(registry.ids(), ["standard"])
        unregister()
        self.assertEqual(registry.ids(), [])
        # 幂等
        unregister()

    def test_duplicate_rejected(self):
        registry = AgentPresetRegistry(self.ctx)
        registry.register(PresetDefinition(id="x", plugins=[]))
        with self.assertRaisesRegex(ValueError, "Duplicate agent preset: x"):
            registry.register(PresetDefinition(id="x", plugins=[]))

    def test_empty_id_rejected(self):
        registry = AgentPresetRegistry(self.ctx)
        with self.assertRaisesRegex(ValueError, "must not be empty"):
            registry.register(PresetDefinition(id="", plugins=[]))

    def test_composition_inventory(self):
        registry = AgentPresetRegistry(self.ctx)
        registry.register(PresetDefinition(
            id="standard", name="Standard",
            plugins=[{"id": "a", "name": "dsh-tool-bash"}]))
        groups = registry.composition_inventory()
        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group["id"], "standard")
        self.assertEqual(group["name"], "Standard")
        self.assertEqual(group["isDefault"], True)
        self.assertEqual(group["rows"], [
            {"entryId": "a", "moduleName": "dsh-tool-bash", "enabled": True},
        ])

    def test_composition_inventory_broken(self):
        registry = AgentPresetRegistry(self.ctx)
        registry.register(PresetDefinition(id="bad", plugins="nope"))
        group = registry.composition_inventory()[0]
        self.assertIn("broken", group)
        self.assertEqual(group["rows"], [])

    def test_roster_shape_has_no_mode_selection(self):
        registry = AgentPresetRegistry(self.ctx)
        registry.register(PresetDefinition(id="standard", name="Standard",
                                           plugins=[]))
        roster = registry.roster()
        self.assertEqual(roster, {"presets": [
            {"id": "standard", "name": "Standard", "isDefault": True},
        ]})
        self.assertNotIn("modeSelectionEnabled", roster)

    def test_default_id_selected_over_deployment_default(self):
        registry = AgentPresetRegistry(
            self.ctx, default="standard", selected_default="minimal")
        registry.register(PresetDefinition(id="standard", plugins=[]))
        registry.register(PresetDefinition(id="minimal", plugins=[]))
        self.assertEqual(registry.default_id, "minimal")
        roster = registry.roster()
        minimal = next(r for r in roster["presets"] if r["id"] == "minimal")
        self.assertTrue(minimal["isDefault"])

    def test_default_id_falls_back_to_deployment_default(self):
        registry = AgentPresetRegistry(self.ctx, default="standard")
        registry.register(PresetDefinition(id="standard", plugins=[]))
        registry.register(PresetDefinition(id="minimal", plugins=[]))
        self.assertEqual(registry.default_id, "standard")

    def test_default_id_falls_back_to_first_registered(self):
        registry = AgentPresetRegistry(self.ctx)
        registry.register(PresetDefinition(id="standard", plugins=[]))
        registry.register(PresetDefinition(id="minimal", plugins=[]))
        self.assertEqual(registry.default_id, "standard")

    def test_roster_reports_broken_rows(self):
        registry = AgentPresetRegistry(self.ctx)
        registry.register(PresetDefinition(id="bad", plugins="nope"))
        roster = registry.roster()
        self.assertIn("broken", roster["presets"][0])
        self.assertTrue(roster["presets"][0]["isDefault"])

    def test_install_accepts_defaults(self):
        bare = Context(name="with-defaults")
        self.addCleanup(bare.dispose)
        registry = install_agent_preset_registry(
            bare, default="standard", selected_default="minimal")
        self.assertEqual(registry.default_id, "minimal")

    def test_install_idempotent(self):
        first = install_agent_preset_registry(self.ctx)
        self.assertIs(self.ctx.get("agentPresets"), first)
        self.assertIs(install_agent_preset_registry(self.ctx), first)


class TestLoadDefinitionRows(unittest.TestCase):
    def test_load_yaml_rows(self):
        from miniharness.preset.registry import load_definition_rows
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.cordis.yml"
            path.write_text(
                "plugins:\n"
                "  - id: a\n"
                "    name: dsh-tool-bash\n",
                encoding="utf-8")
            self.assertEqual(load_definition_rows(path),
                             [{"id": "a", "name": "dsh-tool-bash"}])


if __name__ == "__main__":
    unittest.main()