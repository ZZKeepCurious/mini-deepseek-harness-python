"""第 8 章测试：preset roster —— 组合选择与挂载。"""
import json
import tempfile
import unittest
from pathlib import Path

from miniharness.core.scope import Context
from miniharness.preset.presets import Preset, PresetRoster, builtin_roster
from miniharness.core.tools import Tool, ToolRegistry


def make_tool(name: str) -> Tool:
    return Tool(name=name, description=f"工具 {name}", execute=lambda args, exec: "ok")


class TestRosterDiscovery(unittest.TestCase):
    def test_builtin_roster_discovers_by_directory(self):
        roster = builtin_roster()
        ids = roster.ids()
        self.assertIn("standard", ids)
        self.assertIn("minimal", ids)
        # 按 order 排序：standard(1) 在 minimal(3) 前
        self.assertEqual(ids.index("standard"), 0)
        self.assertEqual(ids.index("minimal"), 1)

    def test_resolve_unknown_fails_loud(self):
        roster = builtin_roster()
        with self.assertRaises(KeyError):
            roster.resolve("nope")

    def test_minimal_is_fixed_prompt_two_tools(self):
        p = builtin_roster().resolve("minimal")
        self.assertTrue(p.persona.complete)
        self.assertFalse(p.persona.include_runtime_context)
        self.assertEqual(sorted(p.tools), ["bash", "str_replace_editor"])
        self.assertIsNotNone(p.persona.system_prompt)

    def test_standard_has_runtime_context_and_more_tools(self):
        p = builtin_roster().resolve("standard")
        self.assertFalse(p.persona.complete)
        self.assertTrue(p.persona.include_runtime_context)
        self.assertGreater(len(p.tools), 2)

    def test_missing_root_yields_empty_roster(self):
        # 根缺失 → 空名单（上游 scanRoot ENOENT → []）
        with tempfile.TemporaryDirectory() as tmp:
            roster = PresetRoster(Path(tmp) / "nope")
            self.assertEqual(roster.ids(), [])

    def test_broken_preset_occupies_roster_row(self):
        # 名字合法但缺 preset.json → 占位 broken 行；残渣目录名跳过（上游 discovery.ts:139-163）
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "ghost").mkdir()
            (root / ".DS_Store").mkdir()
            (root / "valid").mkdir()
            (root / "valid" / "preset.json").write_text(json.dumps(
                {"id": "valid", "name": "Valid", "description": "", "order": 2}), encoding="utf-8")
            roster = PresetRoster(root)
            ids = roster.ids()
            self.assertIn("ghost", ids)
            self.assertIn("valid", ids)
            self.assertNotIn(".DS_Store", ids)
            ghost = roster.resolve("ghost")
            self.assertIsNotNone(ghost.broken)
            # broken 可 resolve（展示/删除需要行），但挂载期拒绝
            with self.assertRaisesRegex(RuntimeError, "is broken"):
                ghost.mount(Context(name="host"), Context(name="agent"),
                            ToolRegistry(Context(name="host2")))

    def test_unloadable_manifest_occupies_broken_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "corrupt").mkdir()
            (root / "corrupt" / "preset.json").write_text("{ not json", encoding="utf-8")
            roster = PresetRoster(root)
            corrupt = roster.resolve("corrupt")
            self.assertIsNotNone(corrupt.broken)
            self.assertIn("unloadable", corrupt.broken)


class TestMount(unittest.TestCase):
    def setUp(self):
        self.host = Context(name="host")
        self.registry = ToolRegistry(self.host)
        for name in ["bash", "fs_read", "fs_write", "str_replace_editor", "web_search",
                     "skills", "goal", "plan"]:
            self.registry.register(make_tool(name), scope=None)

    def test_mount_registers_view_in_agent_scope(self):
        agent = self.host.create_scope("agent:1")
        roster = builtin_roster()
        view = roster.resolve("minimal").mount(self.host, agent, self.registry)
        # agent 作用域视图只看到 preset 声明的工具
        self.assertEqual(sorted(view.names(agent)),
                         ["bash", "str_replace_editor"])
        # host 注册表不受影响（仍是全部工具）
        self.assertIn("fs_write", self.registry.names())
        self.assertIsNone(view.resolve("fs_write", agent))

    def test_mount_missing_tool_fails_loud(self):
        agent = self.host.create_scope("agent:2")
        preset = Preset(id="broken", name="缺工具", description="", order=9,
                        tools=["does_not_exist"])
        with self.assertRaises(RuntimeError) as cm:
            preset.mount(self.host, agent, self.registry)
        self.assertIn("does_not_exist", str(cm.exception))

    def test_mount_process_global_conflict_rejected(self):
        agent = self.host.create_scope("agent:3")
        self.host.provide("session-persistence", object())
        preset = Preset(id="rogue", name="越权", description="", order=9,
                        tools=["bash"], provides=["session-persistence"])
        with self.assertRaises(RuntimeError) as cm:
            preset.mount(self.host, agent, self.registry)
        self.assertIn("session-persistence", str(cm.exception))

    def test_mount_ok_when_provides_key_absent_in_host(self):
        agent = self.host.create_scope("agent:4")
        preset = Preset(id="ok", name="正常", description="", order=9,
                        tools=["bash"], provides=["agent-local-service"])
        view = preset.mount(self.host, agent, self.registry)
        self.assertEqual(view.names(agent), ["bash"])

    def test_custom_roster_directory(self):
        import json
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d = root / "custom"
            d.mkdir()
            with open(d / "preset.json", "w", encoding="utf-8") as f:
                json.dump({"id": "custom", "name": "自定义", "order": 5,
                           "tools": ["bash"]}, f)
            roster = PresetRoster(root)
            self.assertEqual(roster.ids(), ["custom"])
            self.assertEqual(roster.resolve("custom").tools, ["bash"])


if __name__ == "__main__":
    unittest.main()