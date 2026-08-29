"""第 8 章测试：preset roster —— 组合选择、投影与挂载。

覆盖 dsh-v0.1.2-alpha.1 对齐增量：多 root 分层（shipped system / user 根，
first-root-wins）、project_preset 投影（trust/source/is_default）、会话锁
（PresetLockedError，turn/start 后拒绝 select）、会话投影 fold
（project_session_agent_preset）、非 user trust 只读（delete →
PresetNotWritableError）以及 agent.cordis.yml → Preset 的 YAML 翻译。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

from miniharness.core.scope import Context
from miniharness.core.tools import Tool, ToolRegistry
from miniharness.preset.presets import (
    Preset,
    PresetLockedError,
    PresetMountError,
    PresetNotWritableError,
    PresetRoster,
    SHIPPED_PRESET_ROOT,
    UnknownPresetError,
    builtin_roster,
    delete_preset,
    default_roster,
    project_preset,
    project_session_agent_preset,
    select_preset,
    session_has_started,
    translate_cordis_composition,
)


def make_tool(name: str) -> Tool:
    return Tool(name=name, description=f"工具 {name}", execute=lambda args, exec: "ok")


def write_preset(root: Path, preset_id: str, manifest: dict) -> None:
    d = root / preset_id
    d.mkdir(parents=True)
    (d / "preset.json").write_text(json.dumps(manifest), encoding="utf-8")


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
        with self.assertRaises(UnknownPresetError) as cm:
            roster.resolve("nope")
        self.assertIn("standard", cm.exception.available)

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


class TestShippedRootAndProjection(unittest.TestCase):
    def test_shipped_preset_trust_is_system(self):
        # shipped root 永远在分层最前，且 trust=system；用户根同名同址也无法覆盖
        with tempfile.TemporaryDirectory() as tmp:
            user_root = Path(tmp)
            write_preset(user_root, "standard", {"id": "standard", "name": "偷梁", "order": 0})
            roster = PresetRoster([user_root], default="standard", include_shipped_root=True)
            proj = project_preset(roster, "standard")
            self.assertEqual(proj.trust, "system")           # first-root-wins：shipped 胜
            self.assertEqual(proj.preset.name, "标准模式")     # 不是用户根那份
            self.assertTrue(proj.is_default)
            self.assertEqual(proj.source_root, SHIPPED_PRESET_ROOT)

    def test_user_root_preset_projects_with_user_trust(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_root = Path(tmp).resolve()
            write_preset(user_root, "custom", {"id": "custom", "name": "自定义", "order": 5})
            roster = PresetRoster([user_root], default="standard", include_shipped_root=True)
            proj = project_preset(roster, "custom")
            self.assertEqual(proj.trust, "user")
            self.assertEqual(proj.source_root, user_root)
            self.assertFalse(proj.is_default)
            self.assertEqual(proj.preset.tools, [])

    def test_projection_uses_roster_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_root = Path(tmp)
            write_preset(user_root, "mine", {"id": "mine", "name": "我的", "order": 0})
            roster = PresetRoster([user_root], default="mine", include_shipped_root=True)
            proj = project_preset(roster)
            self.assertEqual(proj.id, "mine")
            self.assertTrue(proj.is_default)
            # 缺省未配置 → ValueError（对齐上游 defaultId 不明的顿挫）
            with self.assertRaises(ValueError):
                project_preset(PresetRoster([user_root]))

    def test_rows_shape(self):
        roster = builtin_roster()
        rows = roster.rows()
        standard = next(r for r in rows if r["id"] == "standard")
        self.assertEqual(standard["trust"], "system")
        self.assertTrue(standard["isDefault"])


class TestSessionProjectionAndLock(unittest.TestCase):
    def _lock_free(self):
        return []

    def test_select_after_start_locked(self):
        events = [{"type": "turn/start", "seq": 0, "time": 1, "data": {}}]
        self.assertTrue(session_has_started(events))
        with self.assertRaises(PresetLockedError) as cm:
            select_preset(builtin_roster(), events, "standard", session_id="s-1")
        self.assertEqual(cm.exception.session_id, "s-1")
        self.assertIn("already started", str(cm.exception))

    def test_select_before_start_ok(self):
        proj = select_preset(builtin_roster(), [], "minimal", session_id="s-2")
        self.assertEqual(proj.id, "minimal")

    def test_select_empty_id_rejected_like_upstream(self):
        with self.assertRaises(ValueError):
            select_preset(builtin_roster(), [], "", session_id="s-3")

    def test_session_projection_folds_selected(self):
        events = [
            {"type": "agent-preset/selected", "seq": 0, "time": 1,
             "data": {"agentPreset": "minimal"}},
            {"type": "agent-preset/selected", "seq": 1, "time": 2,
             "data": {"agentPreset": "standard"}},
        ]
        self.assertEqual(project_session_agent_preset(events), "standard")
        self.assertEqual(project_session_agent_preset([], header="minimal"), "minimal")
        self.assertEqual(project_session_agent_preset([]), None)


class TestAuthoringReadOnly(unittest.TestCase):
    def test_shipped_delete_read_only(self):
        with self.assertRaises(PresetNotWritableError) as cm:
            delete_preset(builtin_roster(), "standard")
        self.assertIn("ships with the deployment", str(cm.exception))

    def test_user_delete_removes_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            user_root = Path(tmp)
            write_preset(user_root, "custom", {"id": "custom", "name": "自定义", "order": 5})
            roster = PresetRoster([user_root], include_shipped_root=True)
            self.assertIn("custom", roster.ids())
            delete_preset(roster, "custom")
            self.assertNotIn("custom", roster.ids())
            self.assertFalse((user_root / "custom").exists())


class TestYamlTranslation(unittest.TestCase):
    def test_translates_agent_cordis_yml(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "port"
            d.mkdir()
            (d / "agent.cordis.yml").write_text(
                "plugins:\n"  # 顶层对象形态（mini 组合惯例）也能读
                "- name: '@deepseek-ai/dsh-persona'\n"
                "  config:\n"
                "    text: '你是翻译后的助手。'\n"
                "    complete: true\n"
                "- name: '@deepseek-ai/dsh-tool-bash-persistent'\n"
                "- name: '@deepseek-ai/dsh-tool-pwsh'\n"
                "  disabled: !!js process.platform === 'win32'\n"
                "- name: '@deepseek-ai/dsh-plan-mode'\n"
                "- name: '@deepseek-ai/dsh-tool-web'\n"
                "- name: 'cordis:group'\n"
                "  group: true\n"
                "  config:\n"
                "    - name: '@deepseek-ai/dsh-tool-fs'\n"
                "    - name: '@deepseek-ai/dsh-tool-skill'\n",
                encoding="utf-8",
            )
            (d / "preset.yml").write_text(
                "name: 移植\n"
                "description: 从上游 agent.cordis.yml 翻译\n"
                "order: 7\n",
                encoding="utf-8",
            )
            p = translate_cordis_composition(d, "user")
            self.assertEqual(p.id, "port")
            self.assertEqual(p.name, "移植")
            self.assertEqual(p.order, 7)
            self.assertTrue(p.persona.complete)
            # 平台门静态求值：pwsh === 'win32' 被禁 → win32 上不含 pwsh
            expected = ["bash", "fs_read", "fs_write", "plan", "skills", "web_search"]
            if sys.platform == "win32":
                self.assertNotIn("pwsh", p.tools)
            else:
                expected.append("pwsh")
            self.assertEqual(sorted(p.tools), sorted(expected))


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
        with self.assertRaises(PresetMountError) as cm:
            preset.mount(self.host, agent, self.registry)
        self.assertIn("does_not_exist", str(cm.exception))

    def test_mount_process_global_conflict_rejected(self):
        agent = self.host.create_scope("agent:3")
        self.host.provide("session-persistence", object())
        preset = Preset(id="rogue", name="越权", description="", order=9,
                        tools=["bash"], provides=["session-persistence"])
        with self.assertRaises(PresetMountError) as cm:
            preset.mount(self.host, agent, self.registry)
        self.assertIn("session-persistence", str(cm.exception))

    def test_mount_refuses_unscoped_context(self):
        # 无作用域上下文 = 注册进 root 层，漏进每个 agent → 拒绝
        preset = Preset(id="leak", name="泄漏", description="", order=9, tools=["bash"])
        with self.assertRaises(PresetMountError) as cm:
            preset.mount(self.host, Context(name="agent-bare"), self.registry)
        self.assertIn("non-scoped", str(cm.exception))

    def test_mount_ok_when_provides_key_absent_in_host(self):
        agent = self.host.create_scope("agent:4")
        preset = Preset(id="ok", name="正常", description="", order=9,
                        tools=["bash"], provides=["agent-local-service"])
        view = preset.mount(self.host, agent, self.registry)
        self.assertEqual(view.names(agent), ["bash"])

    def test_custom_roster_directory(self):
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


class TestDefaultRoster(unittest.TestCase):
    def test_default_roster_includes_user_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            user_root = home / ".agent-presets"
            write_preset(user_root, "mine", {"id": "mine", "name": "我的", "order": 0})
            roster = default_roster({"MINIHARNESS_HOME": str(home)}, default="mine")
            self.assertEqual(roster.resolve("mine").name, "我的")
            self.assertIn("standard", roster.ids())  # shipped 仍在
            # user root 未创建 → 空（ENOENT → []）
            empty = default_roster({"MINIHARNESS_HOME": str(home)})
            self.assertIn("standard", empty.ids())


if __name__ == "__main__":
    unittest.main()