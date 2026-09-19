"""Include 子树：文件装载 / initial 写回 / 运行期持久化 / __jsExpr 原样回写。

对应上游：vendor/include/src/index.ts（applyEntryPatches / writeBack /
allowUpsert）、vendor/loader/src/config/tree.ts。boot 是装载面（include
条目 module: cordis:include，载波不进 activations）。
"""
import os
import tempfile
import unittest
from pathlib import Path

import yaml

from miniharness.boot import boot
from miniharness.loader.include import dump_js_expr_yaml

SUB = "plugins:\n  - id: greeter\n    module: miniharness.example_plugins\n    config:\n      greeting: 你好\n"


def _write(p: Path, text: str) -> None:
    p.write_text(text, encoding="utf-8")


class TestIncludeBoot(unittest.TestCase):
    def test_rows_active_and_carrier_not_listed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "sub.yml", SUB)
            _write(root / "cordis.yml",
                   "plugins:\n  - id: inc\n    module: cordis:include\n    config:\n"
                   "      path: sub.yml\n")
            ctx, activations = boot(root / "cordis.yml")
            self.assertEqual([n for n, _ in activations], ["greeter"])
            self.assertEqual(ctx.get("greeter")("名"), "你好, 名!")

    def test_missing_file_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "cordis.yml",
                   "plugins:\n  - id: inc\n    module: cordis:include\n    config:\n"
                   "      path: nope.yml\n")
            with self.assertRaisesRegex(RuntimeError, "config file not found"):
                boot(root / "cordis.yml")

    def test_initial_writes_file_on_first_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write(root / "cordis.yml",
                   "plugins:\n  - id: inc\n    module: cordis:include\n    config:\n"
                   "      path: fresh.yml\n"
                   "      initial:\n"
                   "        - id: g2\n"
                   "          module: miniharness.example_plugins\n"
                   "          config:\n"
                   "            greeting: bonjour\n"
                   "            service_name: g2_svc\n")
            ctx, activations = boot(root / "cordis.yml")
            self.assertEqual([n for n, _ in activations], ["g2"])
            self.assertEqual(ctx.get("g2_svc")("名"), "bonjour, 名!")
            self.assertIn("bonjour", (root / "fresh.yml").read_text(encoding="utf-8"))


class TestIncludeRuntime(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _write(self.root / "sub.yml", SUB)
        _write(self.root / "cordis.yml",
               "plugins:\n  - id: inc\n    module: cordis:include\n    config:\n"
               "      path: sub.yml\n")
        self.ctx, _ = boot(self.root / "cordis.yml")
        root_include = self.ctx.get("loader").resolve("include").subtree
        self.sub_include = root_include.resolve("inc").subtree

    def tearDown(self):
        self._tmp.cleanup()

    def test_runtime_update_persists_to_file(self):
        row = self.sub_include.resolve("greeter")
        new = dict(row.fiber.config)
        new["greeting"] = "ola"
        row.fiber.update(new, False)
        self.assertIn("ola", (self.root / "sub.yml").read_text(encoding="utf-8"))

    def test_include_write_back_keeps_js_expr_literal(self):
        _write(self.root / "sub.yml",
               "plugins:\n  - id: greeter\n    module: miniharness.example_plugins\n"
               "    config:\n      greeting: !!js process.env.MINI_INC_GREETING\n")
        self.sub_include.refresh()
        self.sub_include.write()
        text = (self.root / "sub.yml").read_text(encoding="utf-8")
        self.assertIn("!!js", text)
        self.assertIn("process.env.MINI_INC_GREETING", text)

    def test_refresh_applies_file_changes(self):
        _write(self.root / "sub.yml",
               SUB + "  - id: second\n"
                     "    module: miniharness.example_plugins\n"
                     "    config:\n"
                     "      greeting: wave\n"
                     "      service_name: second_svc\n")
        self.sub_include.refresh()
        self.assertIn("second", self.sub_include.store)
        self.assertEqual(self.ctx.get("second_svc")("名"), "wave, 名!")


class TestDumpJsExprYaml(unittest.TestCase):
    def test_js_expr_roundtrip(self):
        data = [{"greeting": {"__jsExpr": "process.env.X"}}]
        text = dump_js_expr_yaml(data)
        self.assertIn("!!js", text)
        self.assertEqual(yaml.safe_load(text), data)

    def test_flattens_plugins_wrapper(self):
        data = [{"id": "a", "module": "m"}]
        text = dump_js_expr_yaml(data)
        self.assertIn("id: a", text)


if __name__ == "__main__":
    unittest.main()