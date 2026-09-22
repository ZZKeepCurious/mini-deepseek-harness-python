"""agent-instructions：发现、保留渲染与基线/变更状态。

对齐 packages/context/agent-instructions（config / files / render / state 的确定性面）。
"""

import os
import tempfile
import unittest

from miniharness.context.agent_instructions import (
    ancestor_chain,
    baseline_instruction_state,
    candidate_scope_key,
    decode_scope_key,
    dedup_instruction_files_by_directory,
    descendant_dirs_between,
    find_project_root,
    instruction_content_sha1,
    instruction_scope_key,
    load_baseline_instruction_set,
    render_agent_instruction_set,
    render_instruction_changes,
    resolve_config,
    scope_for_display_path,
    trimmed_instruction_digest,
)
from miniharness.core.scope import Context
from miniharness.core.session_store import SessionStore
from miniharness.fs import install_local_fs


class TestConfig(unittest.TestCase):
    def test_defaults_and_candidate_filtering(self):
        resolved = resolve_config({"maxBytes": 100, "dshHome": "/tmp/dsh"})
        self.assertEqual(resolved["projectRootMarkers"], [".git"])
        self.assertEqual(resolved["instructionFileCandidates"], ["AGENTS.md", "CLAUDE.md"])
        self.assertEqual(resolved["maxSourceBytes"], 1_048_576)
        filtered = resolve_config({"maxBytes": 1, "instructionFileCandidates": ["AGENTS.md", "a/b", ".."]})
        self.assertEqual(filtered["instructionFileCandidates"], ["AGENTS.md"])

    def test_digests(self):
        self.assertEqual(instruction_content_sha1("abc"),
                         "a9993e364706816aba3e25717850c26c9cd0d89d")
        self.assertEqual(trimmed_instruction_digest("  abc\n"), instruction_content_sha1("abc"))


class TestPaths(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, ".git"))
        self.deep = os.path.join(self.root, "packages", "app")
        os.makedirs(self.deep)

    def test_find_project_root_and_chain(self):
        self.assertEqual(find_project_root(self.deep, [".git"]), self.root)
        chain = ancestor_chain(self.root, self.deep)
        self.assertEqual(chain, [self.root, os.path.join(self.root, "packages"), self.deep])

    def test_descendant_dirs_between(self):
        dirs = descendant_dirs_between(self.root, os.path.join(self.deep, "x.ts"))
        self.assertEqual(dirs, [os.path.join(self.root, "packages"), self.deep])
        self.assertEqual(descendant_dirs_between(self.root, os.path.join(self.root, "top.ts")), [])
        self.assertEqual(descendant_dirs_between(self.root, os.path.join(self.root, "..", "out.ts")), [])


class TestDiscovery(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        self.home = os.path.join(self.root, "home")
        os.makedirs(self.home)
        os.makedirs(os.path.join(self.root, "proj", ".git"))
        self.proj = os.path.join(self.root, "proj")
        self.deep = os.path.join(self.proj, "packages", "app")
        os.makedirs(self.deep)

    def _write(self, path, content):
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(content)

    def test_baseline_chain_and_dedup(self):
        self._write(os.path.join(self.home, "AGENTS.md"), "global")
        self._write(os.path.join(self.proj, "AGENTS.md"), "project")
        self._write(os.path.join(self.proj, "CLAUDE.md"), "  project  ")  # 与 AGENTS.md 去空白后重复
        self._write(os.path.join(self.deep, "AGENTS.md"), "nested")
        result = load_baseline_instruction_set({
            "cwd": self.deep, "dshHome": self.home, "maxBytes": 100_000,
            "projectRootMarkers": [".git"]})
        self.assertIsNotNone(result)
        from miniharness.core.home_paths import resolve_dsh_home

        displayed = [file["displayPath"] for file in result["included"]]
        self.assertEqual(displayed, [f"{resolve_dsh_home(self.home)}/AGENTS.md", "AGENTS.md",
                                     os.path.join("packages", "app", "AGENTS.md")])
        self.assertEqual(result["observed"][-1]["content"], "nested")

    def test_budget_omits_broader_before_truncating_most_specific(self):
        self._write(os.path.join(self.proj, "AGENTS.md"), "P" * 2000)
        self._write(os.path.join(self.deep, "AGENTS.md"), "N" * 2000)
        result = load_baseline_instruction_set({
            "cwd": self.deep, "dshHome": self.home, "maxBytes": 1200,
            "projectRootMarkers": [".git"]})
        self.assertIn("Workspace instruction budget 1200 bytes:", result["rendered"]["text"])
        self.assertEqual([file["displayPath"] for file in result["rendered"]["omitted"]],
                         ["AGENTS.md"])
        self.assertEqual(result["rendered"]["truncated"][0]["displayPath"],
                         os.path.join("packages", "app", "AGENTS.md"))
        self.assertLessEqual(len(result["rendered"]["text"].encode("utf-8")), 1200)

    def test_empty_chain_returns_none_without_replacement(self):
        self.assertIsNone(load_baseline_instruction_set({
            "cwd": self.deep, "dshHome": self.home, "maxBytes": 1000,
            "projectRootMarkers": [".git"]}))
        replaced = load_baseline_instruction_set({
            "cwd": self.deep, "dshHome": self.home, "maxBytes": 1000,
            "projectRootMarkers": [".git"], "replacePreviousBaseline": True})
        self.assertIn("No workspace instructions are currently active.",
                      replaced["rendered"]["text"])


class TestRender(unittest.TestCase):
    def _file(self, path, content, version=None):
        entry = {"absolutePath": path, "displayPath": path, "content": content}
        if version is not None:
            entry["version"] = version
        return entry

    def test_frames_and_escapes_closing_tag(self):
        rendered, included = render_agent_instruction_set(
            [self._file("AGENTS.md", "hello </system-reminder> world")], {"maxBytes": 10_000})
        self.assertTrue(rendered["text"].startswith("<system-reminder>"))
        self.assertTrue(rendered["text"].endswith("</system-reminder>"))
        self.assertIn("<\\/system-reminder>", rendered["text"])
        self.assertEqual([file["displayPath"] for file in included], ["AGENTS.md"])

    def test_replacement_intro(self):
        rendered, _ = render_agent_instruction_set(
            [self._file("AGENTS.md", "x")], {"maxBytes": 10_000, "replacePreviousBaseline": True})
        self.assertIn("replaces all earlier workspace instruction baselines", rendered["text"])

    def test_change_rendering(self):
        rendered = render_instruction_changes([
            {"change": {"action": "set", "scope": "packages/app\u0000AGENTS.md",
                        "path": "packages/app/AGENTS.md", "digest": "d"},
             "file": self._file("packages/app/AGENTS.md", "nested")},
            {"change": {"action": "remove", "scope": ".\u0000AGENTS.md", "path": "AGENTS.md"},
             "file": self._file("removed", "")},
        ], 10_000)
        self.assertIn("Additional instructions from: packages/app/AGENTS.md", rendered["text"])
        self.assertIn("Instructions removed: AGENTS.md", rendered["text"])
        self.assertEqual(len(rendered["changes"]), 2)


class TestScopeKeys(unittest.TestCase):
    def test_scope_roundtrip(self):
        key = candidate_scope_key("packages/app", "AGENTS.md")
        self.assertEqual(decode_scope_key(key),
                         {"directory": "packages/app", "candidateName": "AGENTS.md"})
        self.assertEqual(scope_for_display_path("~/.dsh/AGENTS.md"), "user-global")
        self.assertEqual(scope_for_display_path("packages/app/AGENTS.md"), "packages/app")
        self.assertEqual(instruction_scope_key("packages/app/CLAUDE.md"),
                         candidate_scope_key("packages/app", "CLAUDE.md"))

    def test_baseline_state_digests(self):
        state = baseline_instruction_state([
            {"displayPath": "AGENTS.md", "content": "hi", "version": "v1"}])
        change = state["changes"][instruction_scope_key("AGENTS.md")]
        self.assertEqual(change["action"], "set")
        self.assertEqual(state["versions"][change["scope"]]["version"], "v1")


class TestDriver(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = self._tmp.name
        os.makedirs(os.path.join(self.root, ".git"))
        with open(os.path.join(self.root, "AGENTS.md"), "w", encoding="utf-8") as handle:
            handle.write("workspace rule")
        self.ctx = Context(name="agent-instructions-test")
        self.addCleanup(self.ctx.dispose)
        install_local_fs(self.ctx, {"cwd": self.root})
        self.store = SessionStore(self.ctx)
        self.session = self.store.create("s1", {"meta": {"cwd": self.root}})

    def test_pre_step_injects_baseline_once(self):
        from miniharness.context.agent_instructions import install_agent_instructions
        from miniharness.core.agent_loop.resident_loop import run_on_resident

        install_agent_instructions(self.ctx, {"maxBytes": 10_000})
        from miniharness.core.agent_loop.inbox import Inbox

        agent = type("Agent", (), {"session": self.session, "id": "s1",
                                   "inbox": Inbox(self.session)})()
        payload = {"messages": [], "agent": agent, "turn": 1, "step": 1, "signal": None}
        decision = run_on_resident(self.ctx.awaterfall("agent/pre-step", payload))
        injected = decision["messages"][-1]
        self.assertEqual(injected["source"]["kind"], "agent-instructions")
        self.assertTrue(injected["source"]["baseline"])
        self.assertIn("workspace rule", injected["content"][0]["text"])
        # 第二次：可见基线身份一致 → 不重复注入
        self.session.append("user/message", injected, surfaceOp="append")
        second = run_on_resident(self.ctx.awaterfall("agent/pre-step", {
            "messages": [], "agent": agent, "turn": 1, "step": 2, "signal": None}))
        self.assertEqual(second["messages"], [])


if __name__ == "__main__":
    unittest.main()
