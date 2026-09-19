"""pluginInventory 投影测试（对齐 host/plugin-inventory + agent-presets composition-inventory）。

- entries 四字段 + fiberPhase（active / 缺 fiber → None / group 跳过）
- 组合行 flatten（组行跳过、组 disabled 继承、`!!js` conditional、condition 原文）
- entryListProblem 逐字形状诊断
- composition_inventory / build_inventory / PluginInventoryService / web 端点
"""
import tempfile
import unittest
from pathlib import Path

from miniharness.core.scope import Context
from miniharness.core.tools import ToolRegistry
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.loader import Group, Loader
from miniharness.preset.presets import PresetRoster
from miniharness.web.api import WebApi
from miniharness.web.inventory import (
    PluginInventoryService,
    build_inventory,
    composition_inventory,
    entries_snapshot,
    entry_list_problem,
    file_composition,
    install_plugin_inventory,
)


def _loader_root(tmp: Path):
    root = Context(name="root")
    root.plugin(Loader, {"baseUrl": str(tmp)})
    loader = root.get("loader")
    loader.builtins["group"] = Group
    return root, loader


class TestEntriesSnapshot(unittest.TestCase):
    def test_four_fields_group_skipped_and_phases(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, loader = _loader_root(Path(tmp))
            loader.create({"id": "ok", "name": "miniharness.example_plugins",
                           "config": {"service_name": "greeter"}})
            loader.create({"id": "bad", "name": "miniharness.no_such_module"})
            loader.create({"id": "grp", "name": "cordis:group", "group": True,
                           "config": []})
            snapshot = entries_snapshot(root)
            self.assertEqual([e["entryId"] for e in snapshot], ["ok", "bad"])
            ok, bad = snapshot
            self.assertEqual(ok["moduleName"], "miniharness.example_plugins")
            self.assertTrue(ok["enabled"])
            self.assertEqual(ok["fiberPhase"], "active")
            self.assertEqual(bad["fiberPhase"], None)

    def test_missing_loader_service_yields_empty(self):
        root = Context(name="root")
        self.assertEqual(entries_snapshot(root), [])


class TestEntryListProblem(unittest.TestCase):
    def test_wording(self):
        self.assertEqual(entry_list_problem({"a": 1}),
                         "the composition must be a top-level list of plugin rows")
        self.assertEqual(entry_list_problem([42]),
                         'row 1 is not a plugin row (expected a map with a "name")')
        self.assertEqual(entry_list_problem([{"id": "x"}]),
                         'row 1 names no plugin (a "name" string is required)')
        self.assertEqual(entry_list_problem([{"name": "g", "group": True, "config": 5}]),
                         "group row 1 must hold a list of plugin rows")
        self.assertIsNone(entry_list_problem([{"name": "ok"}]))


class TestFileComposition(unittest.TestCase):
    def test_flattens_rows_and_inherits_group_disable(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "agent.cordis.yml"
            path.write_text(
                "- id: a\n"
                "  name: plain\n"
                "- id: g\n"
                "  name: cordis:group\n"
                "  group: true\n"
                "  disabled: true\n"
                "  config:\n"
                "    - id: child\n"
                "      name: nested\n"
                "- id: cond\n"
                "  name: cond\n"
                "  disabled: !!js process.env.MINI_INV\n"
                "- id: off\n"
                "  name: off\n"
                "  disabled: true\n",
                encoding="utf-8",
            )
            result = file_composition(path)
            rows = result["rows"]
            self.assertEqual([r["entryId"] for r in rows], ["a", "child", "cond", "off"])
            self.assertEqual(rows[0], {"entryId": "a", "moduleName": "plain", "enabled": True})
            self.assertEqual(rows[1]["enabled"], False, "组 disabled 应被子女继承")
            self.assertEqual(rows[2]["enabled"], True, "env 未设置 → 表达式为假 → 启用")
            self.assertEqual(rows[2]["condition"], "process.env.MINI_INV")
            self.assertEqual(rows[3]["enabled"], False)

    def test_broken_and_non_composition_carriers(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "agent.cordis.yml"
            bad.write_text("not-a-list\n", encoding="utf-8")
            self.assertEqual(file_composition(bad),
                             {"broken": "the composition must be a top-level list of plugin rows"})
            json_path = Path(tmp) / "preset.json"
            json_path.write_text("{}", encoding="utf-8")
            self.assertEqual(file_composition(json_path), {"rows": []})


class TestCompositionInventory(unittest.TestCase):
    def test_group_fields_and_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / "p2"
            p.mkdir()
            (p / "preset.json").write_text(
                '{"id": "p2", "name": "two"}', encoding="utf-8")
            roster = PresetRoster([root], default="p2")
            groups = composition_inventory(roster)
            self.assertEqual(len(groups), 1)
            group = groups[0]
            self.assertEqual(group["id"], "p2")
            self.assertEqual(group["trust"], "user")
            self.assertTrue(group["isDefault"])
            self.assertEqual(group["rows"], [], "preset.json 载体无插件行")

    def test_broken_preset_reports_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = root / "p1"
            p.mkdir()
            (p / "agent.cordis.yml").write_text("plugins: [", encoding="utf-8")
            roster = PresetRoster([root])
            groups = composition_inventory(roster)
            self.assertEqual(groups[0]["id"], "p1")
            self.assertIn("broken", groups[0])
            self.assertEqual(groups[0]["rows"], [])


class TestBuildInventoryAndService(unittest.TestCase):
    def test_build_without_roster_omits_agent_presets(self):
        root = Context(name="root")
        snapshot = build_inventory(root)
        self.assertEqual(snapshot, {"entries": []})
        self.assertNotIn("agentPresets", snapshot)

    def test_service_install_is_idempotent_and_lists(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, loader = _loader_root(Path(tmp))
            loader.create({"id": "ok", "name": "miniharness.example_plugins",
                           "config": {"service_name": "greeter"}})
            service = install_plugin_inventory(root)
            self.assertIsInstance(service, PluginInventoryService)
            self.assertIs(install_plugin_inventory(root), service)
            value = service.list()
            self.assertEqual([e["entryId"] for e in value["entries"]], ["ok"])


class TestWebEndpoint(unittest.TestCase):
    def test_dispatch_plugin_inventory_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, loader = _loader_root(Path(tmp))
            loader.create({"id": "ok", "name": "miniharness.example_plugins",
                           "config": {"service_name": "greeter"}})
            api = WebApi(root, FakeLlmAdapter(), ToolRegistry(root))
            response = api.dispatch("pluginInventory/list", "rid", {})
            self.assertTrue(response["result"]["ok"])
            entries = response["result"]["value"]["entries"]
            self.assertEqual([e["entryId"] for e in entries], ["ok"])


if __name__ == "__main__":
    unittest.main()
