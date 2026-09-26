"""boot profile 目录机制（对齐 packages/boot/app-boot/src/{profile,profile-context}.ts）。"""
import os
import tempfile
import unittest

from miniharness.boot.profile import (
    PROFILES_DIR,
    PROFILE_PATCH_FILENAME,
    bundle_patch_files,
    compose_entries,
    init_profile,
    load_profile_directory,
    read_profile_patches,
    resolve_profile_dir,
)


class TestResolveProfileDir(unittest.TestCase):
    def test_resolves_under_profiles_dir(self):
        home = tempfile.mkdtemp()
        self.assertEqual(
            resolve_profile_dir("web", home),
            os.path.join(home, PROFILES_DIR, "web"))

    def test_invalid_names_rejected(self):
        for name in ("", "a/b", "a\\b", ".", "..", "node_modules"):
            with self.assertRaises(ValueError):
                resolve_profile_dir(name, tempfile.mkdtemp())


class TestInitProfile(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def test_creates_manifest_and_patch(self):
        init_profile(self.dir, ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"])
        self.assertTrue(os.path.exists(os.path.join(self.dir, "package.json")))
        self.assertTrue(os.path.exists(
            os.path.join(self.dir, PROFILE_PATCH_FILENAME)))
        import json
        with open(os.path.join(self.dir, "package.json"), encoding="utf-8") as h:
            manifest = json.load(h)
        self.assertEqual(manifest["dsh"]["profile"]["bundles"],
                         ["@deepseek-ai/dsh-base", "@deepseek-ai/dsh-headless"])

    def test_idempotent(self):
        init_profile(self.dir, ["@deepseek-ai/dsh-base"])
        first = open(os.path.join(self.dir, PROFILE_PATCH_FILENAME),
                     encoding="utf-8").read()
        init_profile(self.dir, ["@deepseek-ai/dsh-base"])
        second = open(os.path.join(self.dir, PROFILE_PATCH_FILENAME),
                      encoding="utf-8").read()
        self.assertEqual(first, second)


class TestBundlePatchFiles(unittest.TestCase):
    def test_string_and_list(self):
        self.assertEqual(bundle_patch_files({"patch": "./a.yml"}), ["./a.yml"])
        self.assertEqual(bundle_patch_files({"patch": ["a.yml", "b.yml"]}),
                         ["a.yml", "b.yml"])

    def test_invalid_rejected(self):
        with self.assertRaises(ValueError):
            bundle_patch_files({"patch": 42})
        with self.assertRaises(ValueError):
            bundle_patch_files({"patch": [1, 2]})


class TestLoadProfileDirectory(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _write_patch(self, text):
        with open(os.path.join(self.dir, PROFILE_PATCH_FILENAME),
                  "w", encoding="utf-8") as h:
            h.write(text)

    def test_empty_profile(self):
        init_profile(self.dir, [])
        profile = load_profile_directory("miniharness", self.dir)
        self.assertEqual(profile.name, os.path.basename(self.dir))
        self.assertEqual(profile.patches, [])
        self.assertEqual(profile.layers, [])

    def test_user_patch_loaded(self):
        init_profile(self.dir, [])
        self._write_patch(
            "- replace:\n"
            "    id: greeter\n"
            "    config:\n"
            "      greeting: yo\n")
        profile = load_profile_directory("miniharness", self.dir)
        self.assertEqual(len(profile.patches), 1)
        self.assertEqual(profile.patches[0]["replace"]["id"], "greeter")

    def test_user_layer_skipped(self):
        init_profile(self.dir, [])
        self._write_patch("{broken")
        profile = load_profile_directory("miniharness", self.dir,
                                         user_layer=False)
        self.assertEqual(profile.patches, [])

    def test_bundles_recorded_but_not_resolved(self):
        init_profile(self.dir, ["@deepseek-ai/dsh-base"])
        profile = load_profile_directory("miniharness", self.dir)
        self.assertEqual([layer.package_name for layer in profile.layers],
                         ["@deepseek-ai/dsh-base"])
        self.assertEqual(profile.layers[0].patches, [])

    def test_missing_manifest_fails_loud(self):
        with self.assertRaises(FileNotFoundError):
            load_profile_directory("miniharness", self.dir)


class TestReadProfilePatches(unittest.TestCase):
    def test_layer_ordering(self):
        home = tempfile.mkdtemp()
        profile_dir = os.path.join(home, PROFILES_DIR, "web")
        os.makedirs(profile_dir)
        init_profile(profile_dir, [])
        with open(os.path.join(profile_dir, PROFILE_PATCH_FILENAME),
                  "w", encoding="utf-8") as h:
            h.write("- insert:\n    - id: from-profile\n")
        with open(os.path.join(home, PROFILE_PATCH_FILENAME),
                  "w", encoding="utf-8") as h:
            h.write("- insert:\n    - id: from-home\n")
        profile = load_profile_directory("miniharness", profile_dir)
        patches = read_profile_patches(
            "miniharness", profile, home=home,
            overlays=[{"insert": [{"id": "from-overlay"}]}])
        ids = [p.get("insert", [{}])[0].get("id") for p in patches]
        self.assertEqual(ids, ["from-profile", "from-home", "from-overlay"])

    def test_no_home_patch(self):
        home = tempfile.mkdtemp()
        profile_dir = os.path.join(home, PROFILES_DIR, "web")
        os.makedirs(profile_dir)
        init_profile(profile_dir, [])
        profile = load_profile_directory("miniharness", profile_dir)
        patches = read_profile_patches("miniharness", profile, home=home)
        self.assertEqual(patches, [])


class TestComposeEntries(unittest.TestCase):
    def test_applies_patches_over_empty_root(self):
        entries = compose_entries([[
            {"insert": [{"id": "a", "module": "x"}]},
        ]])
        self.assertEqual([e.get("id") for e in entries], ["a"])
        self.assertEqual(entries[0]["module"], "x")

    def test_insert_then_replace_flat_layers(self):
        # 层序：先 insert 建条目，后续 replace 命中（同上游 applyEntryPatches
        # 空根 + layers.flat 的逐补丁叠一源语义）。
        entries = compose_entries([[
            {"insert": [{"id": "a", "module": "x", "config": {"k": 1}}]},
            {"id": "a", "config": {"k": 2}},
        ]])
        self.assertEqual(entries[0]["id"], "a")
        self.assertEqual(entries[0]["config"], {"k": 2})

    def test_bare_replace_on_empty_root_is_noop(self):
        # 空根 + 裸 replace（无 insert 打底）→ target 缺失跳过（同上游 warn
        # 跳过语义），不抛。
        entries = compose_entries([[{"id": "a", "config": {"k": 2}}]])
        self.assertEqual(entries, [])


if __name__ == "__main__":
    unittest.main()