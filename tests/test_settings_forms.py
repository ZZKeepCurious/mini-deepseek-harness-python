"""settings SettingsForms 核心（rc.1：读 config-editor 条目 + 写经 config-editor）。

对齐 packages/settings/settings/src/index.ts（SettingsForms）。测试在 boot 过的
上下文（有 loader + config-editor）验证：命名空间 = profile entry id、三层值、
revision 冲突、写经 config-editor.edit 落 cordis.patch.yml、configure 页面策略。
"""
import os
import tempfile
import unittest

from miniharness.boot import boot
from miniharness.boot.config_editor import install_config_editor
from miniharness.boot.profile import (
    PROFILE_PATCH_FILENAME,
    init_profile,
    load_profile_directory,
    resolve_profile_dir,
)
from miniharness.settings import SettingsConflictError
from miniharness.settings.forms import (
    LEGACY_SECTION_ENTRIES,
    SettingsForms,
    install_settings_forms,
)


class TestSettingsForms(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.abspath(self._tmp.name)
        self.config_file = os.path.join(self.home, "cordis.yml")
        with open(self.config_file, "w", encoding="utf-8") as h:
            h.write(
                "plugins:\n"
                "  - id: greeter\n"
                "    module: miniharness.example_plugins\n"
                "    config:\n"
                "      greeting: hi\n"
                "      service_name: greeter\n")
        self.ctx, _ = boot(self.config_file)
        self.addCleanup(self.ctx.dispose)
        profile_dir = resolve_profile_dir("web", self.home)
        os.makedirs(profile_dir, exist_ok=True)
        init_profile(profile_dir, [])
        self.patch_path = os.path.join(profile_dir, PROFILE_PATCH_FILENAME)
        self.editor = install_config_editor(
            self.ctx, profile_dir=profile_dir, patch_path=self.patch_path,
            home=self.home)
        self.forms = install_settings_forms(self.ctx, config_editor=self.editor,
                                            profile_home=self.home)
        self.events = []
        self.ctx.on("settings/document-updated",
                    lambda p: self.events.append(p))

    def test_namespace_is_profile_entry_id(self):
        desc = self.forms.describe()
        self.assertEqual(desc["writable"], True)
        self.assertEqual(desc["hasDocument"], True)
        self.assertEqual([ns["ns"] for ns in desc["namespaces"]], ["greeter"])

    def test_describe_three_layers(self):
        ns = self.forms.describe()["namespaces"][0]
        self.assertEqual(ns["value"], {"greeting": "hi", "service_name": "greeter"})
        self.assertEqual(ns["applies"], "live")
        self.assertEqual(ns["autoGenerate"], True)
        # base = inherited 层（无补丁时即全量配置）；user 无 override 层时缺省
        self.assertEqual(ns["base"], {"greeting": "hi", "service_name": "greeter"})
        self.assertNotIn("user", ns)

    async def test_update_persists_via_config_editor(self):
        await self.forms.update("greeter", {"greeting": "yo"})
        self.assertEqual(self.ctx.get("greeter")("x"), "yo, x!")
        profile = load_profile_directory("miniharness",
                                         os.path.dirname(self.patch_path))
        row = next(p for p in profile.patches if p.get("id") == "greeter")
        # mergeLayers 语义：patch 只覆盖声明字段，其余配置保留
        self.assertEqual(row["config"],
                         {"greeting": "yo", "service_name": "greeter"})
        # describe 反映新值
        ns = self.forms.describe()["namespaces"][0]
        self.assertEqual(ns["value"]["greeting"], "yo")

    async def test_conflict_on_stale_revision(self):
        with self.assertRaises(SettingsConflictError) as cm:
            await self.forms.update("greeter", {"greeting": "x"}, expected_revision=99)
        self.assertEqual(cm.exception.code, "SETTINGS_CONFLICT")

    async def test_replace_section(self):
        await self.forms.replace("greeter", {"greeting": "yo", "service_name": "greeter"})
        profile = load_profile_directory("miniharness",
                                         os.path.dirname(self.patch_path))
        row = next(p for p in profile.patches if p.get("id") == "greeter")
        self.assertEqual(row["config"], {"greeting": "yo", "service_name": "greeter"})

    async def test_mutate_unset(self):
        await self.forms.mutate("greeter", [{"op": "set", "path": ["greeting"], "value": "yo"}])
        await self.forms.mutate("greeter", [{"op": "unset", "path": ["service_name"]}])
        profile = load_profile_directory("miniharness",
                                         os.path.dirname(self.patch_path))
        row = next(p for p in profile.patches if p.get("id") == "greeter")
        self.assertEqual(row["config"], {"greeting": "yo"})

    async def test_update_unknown_namespace_rejected(self):
        with self.assertRaises(KeyError):
            await self.forms.update("nope", {"a": 1})

    def test_document_path_is_profile_patch(self):
        self.assertEqual(self.forms.document_path, self.patch_path)
        self.assertEqual(self.forms.prepare_document(), self.patch_path)

    def test_configure_policy(self):
        entry = next(e for e in self.editor.entries()
                     if e.options.get("id") == "greeter")
        disposer = self.forms.configure({"auto": False}, owner=entry.fiber)
        ns = self.forms.describe()["namespaces"][0]
        self.assertEqual(ns["autoGenerate"], False)
        disposer()
        ns = self.forms.describe()["namespaces"][0]
        self.assertEqual(ns["autoGenerate"], True)

    def test_install_idempotent(self):
        self.assertIs(
            install_settings_forms(self.ctx, config_editor=self.editor),
            self.forms)

    def test_legacy_section_mapping(self):
        # shell → 平台 executor；win32 走 pwsh-sandbox（对齐 index.ts:201-206）。
        self.assertEqual(LEGACY_SECTION_ENTRIES["ui-developer-tools"], "ui-settings")
        self.assertIn(LEGACY_SECTION_ENTRIES["shell"],
                      ("bash-sandbox", "pwsh-sandbox"))


class TestSettingsFormsLegacyImport(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.abspath(self._tmp.name)
        self.config_file = os.path.join(self.home, "cordis.yml")
        with open(self.config_file, "w", encoding="utf-8") as h:
            h.write(
                "plugins:\n"
                "  - id: greeter\n"
                "    module: miniharness.example_plugins\n"
                "    config:\n"
                "      greeting: hi\n")
        self.ctx, _ = boot(self.config_file)
        self.addCleanup(self.ctx.dispose)
        profile_dir = resolve_profile_dir("web", self.home)
        os.makedirs(profile_dir, exist_ok=True)
        init_profile(profile_dir, [])
        self.patch_path = os.path.join(profile_dir, PROFILE_PATCH_FILENAME)
        self.editor = install_config_editor(
            self.ctx, profile_dir=profile_dir, patch_path=self.patch_path,
            home=self.home)

    def test_import_moves_and_applies(self):
        legacy = os.path.join(self.home, "settings.yaml")
        with open(legacy, "w", encoding="utf-8") as h:
            h.write("greeter:\n  greeting: yo\n")
        forms = install_settings_forms(self.ctx, config_editor=self.editor,
                                       profile_home=self.home)
        # 文档在首次写前改名 .imported
        self.assertFalse(os.path.exists(legacy))
        self.assertTrue(os.path.exists(legacy + ".imported"))
        # 已导入活动 profile
        ns = forms.describe()["namespaces"][0]
        self.assertEqual(ns["value"]["greeting"], "yo")
        self.assertEqual(self.ctx.get("greeter")("x"), "yo, x!")

    def test_import_idempotent_without_repeat(self):
        legacy = os.path.join(self.home, "settings.yaml")
        with open(legacy, "w", encoding="utf-8") as h:
            h.write("greeter:\n  greeting: yo\n")
        install_settings_forms(self.ctx, config_editor=self.editor,
                               profile_home=self.home)
        # 再次装配（幂等）：改名文件已存在 → 不重复导入
        forms2 = install_settings_forms(self.ctx, config_editor=self.editor,
                                        profile_home=self.home)
        self.assertIs(forms2, self.ctx.get("settings"))
        ns = forms2.describe()["namespaces"][0]
        self.assertEqual(ns["value"]["greeting"], "yo")


if __name__ == "__main__":
    unittest.main()