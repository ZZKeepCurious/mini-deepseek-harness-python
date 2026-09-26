"""boot config-editor（对齐 packages/boot/config-editor/src/index.ts）。"""
import os
import tempfile
import unittest

from miniharness.boot import boot
from miniharness.boot.config_editor import (
    ConfigEditor,
    install_config_editor,
    _round_trip_dump,
)
from miniharness.boot.profile import (
    PROFILE_PATCH_FILENAME,
    init_profile,
    load_profile_directory,
    resolve_profile_dir,
)


class TestConfigEditorDump(unittest.TestCase):
    def test_round_trip_preserves_structure(self):
        doc = [{"id": "a", "name": "x", "config": {"k": 1, "nested": {"z": 2}}}]
        text = _round_trip_dump(doc)
        from ruamel.yaml import YAML
        loaded = YAML().load(text)
        self.assertEqual(loaded, doc)


class TestConfigEditor(unittest.TestCase):
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

    def _entry(self, entry_id="greeter"):
        return next(e for e in self.editor.entries() if e.options.get("id") == entry_id)

    def test_document_path(self):
        self.assertEqual(self.editor.document_path, self.patch_path)

    def test_entries_finds_boot_plugin(self):
        self.assertEqual(self._entry().options.get("id"), "greeter")

    def test_configuration_reads_inherited(self):
        rows = self.editor.configuration()
        row = next(r for r in rows if r["entry"].options.get("id") == "greeter")
        self.assertEqual(row["inherited"], {"greeting": "hi"})
        self.assertEqual(row["override"], {})

    def test_edit_persists_patch_and_applies(self):
        self.editor.edit(self._entry(),
                         lambda current, inherited: {**current, "greeting": "yo"})
        self.assertEqual(self.ctx.get("greeter")("x"), "yo, x!")
        # patch 文件已写
        profile = load_profile_directory("miniharness",
                                         os.path.dirname(self.patch_path))
        row = next(p for p in profile.patches if p.get("id") == "greeter")
        self.assertEqual(row["config"], {"greeting": "yo"})

    def test_edit_back_to_inherited_removes_row(self):
        self.editor.edit(self._entry(),
                         lambda c, i: {**c, "greeting": "yo"})
        self.editor.edit(self._entry(),
                         lambda c, i: {"greeting": "hi"})
        profile = load_profile_directory("miniharness",
                                         os.path.dirname(self.patch_path))
        rows = [p for p in profile.patches if p.get("id") == "greeter"]
        self.assertEqual(rows, [])
        self.assertEqual(self.ctx.get("greeter")("x"), "hi, x!")

    def test_edit_unknown_entry_rejected(self):
        fake = type("Fake", (), {"options": {"id": "nope"}, "fiber": None,
                                 "__eq__": lambda s, o: False})()
        with self.assertRaisesRegex(RuntimeError, "no longer available"):
            self.editor.edit(fake, lambda c, i: c)

    def test_install_idempotent(self):
        self.assertIs(
            install_config_editor(self.ctx, profile_dir="x", patch_path="y"),
            self.editor)


if __name__ == "__main__":
    unittest.main()