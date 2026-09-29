"""production web 装配接 profile boot：SettingsForms/config-editor 生产接线。

任务：tasks.md「production web 装配接 profile boot」（migration-log 步骤 184）。

验证 `--profile web` 装配现在是 boot/profile 驱动：默认 web 组合
（cli/web_profile.yml 条目树，cli/plugins/* 插件）经 Loader 装载、config-editor
+ SettingsForms 在 boot 过的上下文装配、命名空间 = 组合条目 id、写经
config-editor 持久化 profile `cordis.patch.yml`、用户补丁层覆盖条目 config。
"""
import os
import sys
import tempfile
import unittest
from unittest.mock import patch as mock_patch

import miniharness.cli
from miniharness.boot import boot
from miniharness.boot.config_editor import install_config_editor
from miniharness.boot.profile import (
    PROFILE_PATCH_FILENAME,
    PROFILE_TEMPLATES,
    init_profile,
    load_profile_directory,
    read_profile_patches,
    resolve_profile_dir,
)
from miniharness.llm import FakeLlmAdapter
from miniharness.preset.presets import default_roster
from miniharness.settings.forms import SettingsForms, install_settings_forms

EXPECTED_ENTRY_IDS = [
    "system-prompt", "web", "user-questions", "sandbox-policy",
    "terminal-controller", "fs", "credentials", "workspaces",
    "workspace-controller", "workspace-files", "session-projections",
    "usage-stats", "turn-outline", "command-feedback", "message-feedback",
    "approval", "permission-presets", "session-title",
    "session-title-first-prompt-llm", "deepseek-account",
]


def _web_profile_config_path() -> str:
    return os.path.join(os.path.dirname(miniharness.cli.__file__), "web_profile.yml")


def _seed_web_include(profile_dir: str) -> str:
    """首次 boot：把 shipped 模板种子进 profile include（与 _web_main 同逻辑）。"""
    include_path = os.path.join(profile_dir, "cordis.yml")
    if not os.path.exists(include_path):
        import shutil

        shutil.copyfile(_web_profile_config_path(), include_path)
    return include_path


class TestWebProfileBoot(unittest.TestCase):
    """直接 boot 默认 web 组合（与 _web_main 相同的装配序）。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.abspath(self._tmp.name)
        self.profile_dir = resolve_profile_dir("web", self.home)
        init_profile(self.profile_dir, PROFILE_TEMPLATES["web"])
        self.include_path = _seed_web_include(self.profile_dir)
        self.profile = load_profile_directory("miniharness", self.profile_dir)
        self.patches = read_profile_patches("miniharness", self.profile, home=self.home)
        self.ctx, _ = boot(
            self.include_path, patches=self.patches,
            env={"adapter": FakeLlmAdapter(), "roster": default_roster()})
        self.addCleanup(self.ctx.dispose)
        self.editor = install_config_editor(
            self.ctx, profile_dir=self.profile_dir, patch_path=self.profile.patch_path,
            home=self.home)
        self.forms = install_settings_forms(self.ctx, config_editor=self.editor,
                                            profile_home=self.home)

    def test_all_web_services_installed(self):
        for key in (
            "systemPrompt", "web", "userQuestions", "sandbox", "sandboxPolicy",
            "terminalController", "fs", "credentials", "workspaces",
            "workspaceController", "workspaceFiles", "sessionProjections",
            "usageStats", "sessionFeedback", "messageFeedback",
            "approval", "permissionPresets", "sessionTitle", "deepseekAccount",
        ):
            self.assertIsNotNone(self.ctx.get(key), key)
        # turn-outline 不提供独立服务：把投影单元注册进 sessionProjections（M18）。
        self.assertIn("turnOutline", self.ctx.get("sessionProjections")._registrations)

    def test_settings_is_settings_forms(self):
        self.assertIsInstance(self.ctx.get("settings"), SettingsForms)

    def test_config_editor_entries_match_composition(self):
        entry_ids = [e.options.get("id") for e in self.editor.entries()]
        self.assertEqual(entry_ids, EXPECTED_ENTRY_IDS)

    def test_settings_describe_namespaces_match_composition(self):
        ns = [n["ns"] for n in self.forms.describe()["namespaces"]]
        self.assertEqual(ns, EXPECTED_ENTRY_IDS)

    def test_settings_update_persists_to_profile_patch(self):
        import asyncio

        asyncio.run(self.forms.update("message-feedback", {"maxNoteBytes": 4096}))
        profile = load_profile_directory("miniharness", self.profile_dir)
        row = next(p for p in profile.patches if p.get("id") == "message-feedback")
        self.assertEqual(row["config"], {"maxNoteBytes": 4096})
        self.assertEqual(self.ctx.get("messageFeedback").max_note_bytes, 4096)

    def test_profile_patch_overrides_entry_config(self):
        # 预写用户补丁层 → 重 boot：组合条目 config 反映 override（SettingsForms
        # 读面由此生效）。
        patch_path = os.path.join(self.profile_dir, PROFILE_PATCH_FILENAME)
        with open(patch_path, "w", encoding="utf-8") as h:
            h.write(
                "- id: message-feedback\n"
                "  config:\n"
                "    maxNoteBytes: 4096\n"
                "- id: sandbox-policy\n"
                "  config:\n"
                "    mode: read-only\n")
        profile = load_profile_directory("miniharness", self.profile_dir)
        patches = read_profile_patches("miniharness", profile, home=self.home)
        with mock_patch.dict(os.environ):
            os.environ.pop("DSH_PERMISSION_MODE", None)
            ctx, _ = boot(self.include_path, patches=patches,
                          env={"adapter": FakeLlmAdapter(), "roster": default_roster()})
        self.addCleanup(ctx.dispose)
        self.assertEqual(ctx.get("messageFeedback").max_note_bytes, 4096)
        self.assertEqual(ctx.get("sandboxPolicy").default_mode, "read-only")

    def test_shipped_template_is_not_rewritten(self):
        # Loader 的 unload 标记会写 include 文件，但绝不写 shipped 模板资产。
        with open(_web_profile_config_path(), encoding="utf-8") as handle:
            shipped = handle.read()
        self.assertNotIn("disabled: true", shipped)
        self.assertIn("session-title-first-prompt-llm", shipped)


class TestWebMainWiresSettingsForms(unittest.TestCase):
    """经 cli.main 端到端：--profile web → boot + SettingsForms 生产接线。"""

    def test_web_main_installs_settings_forms(self):
        from miniharness.web import launcher as web_launcher

        captured: dict = {}

        def fake_run_web(adapter, tools, ctx, **kwargs):
            captured["settings"] = ctx.get("settings")
            captured["editor_ids"] = [
                e.options.get("id") for e in ctx.get("configEditor").entries()]
            captured["ns"] = [n["ns"] for n in ctx.get("settings").describe()["namespaces"]]
            captured["tools"] = tools
            ctx.dispose()

        class _Stream:
            def __init__(self, lines):
                self._lines = lines

            def write(self, text):
                self._lines.append(text)

        with tempfile.TemporaryDirectory() as home:
            out, err, code = [], [], [None]

            def fake_exit(n):
                code[0] = n
                raise SystemExit(n)

            with mock_patch.dict(os.environ, {"MINIHARNESS_HOME": home}, clear=False):
                os.environ.pop("DEEPSEEK_API_KEY", None)
                with mock_patch.object(web_launcher, "run_web", fake_run_web), \
                     mock_patch.object(sys, "stdout", _Stream(out)), \
                     mock_patch.object(sys, "stderr", _Stream(err)), \
                     mock_patch.object(sys, "exit", fake_exit):
                    try:
                        miniharness.cli.main(["--profile", "web"])
                    except SystemExit:
                        pass
        self.assertEqual(code[0], None)
        self.assertIsInstance(captured["settings"], SettingsForms)
        self.assertEqual(captured["editor_ids"], EXPECTED_ENTRY_IDS)
        self.assertEqual(captured["ns"], EXPECTED_ENTRY_IDS)
        self.assertIsNotNone(captured["tools"])


if __name__ == "__main__":
    unittest.main()