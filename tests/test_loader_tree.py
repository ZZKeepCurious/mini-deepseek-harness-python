"""loader 活树：组载体 / 补丁 / !!js 激活期求值 / 依赖缺失审计。

对应上游：vendor/loader/src/config/{entry,group,tree}.ts、
vendor/include/src/index.ts（applyEntryPatches）。boot 是装载面（配合
test_persistence_boot 的组合契约）。
"""
import os
import tempfile
import unittest
from pathlib import Path

from miniharness.boot import boot
from miniharness.loader.patch import apply_entry_patches
from miniharness.loader.utils import is_js_expr, resolve_js_exprs

WARNED: list[str] = []


def _warn(message: str, *args) -> None:
    WARNED.append(message)


class TestApplyEntryPatches(unittest.TestCase):
    def setUp(self):
        WARNED.clear()

    def test_insert_top_level_appends(self):
        data = [{"id": "a", "module": "m"}]
        out = apply_entry_patches(data, [{"insert": [{"id": "b", "module": "m2"}]}])
        self.assertEqual([e["id"] for e in out], ["a", "b"])

    def test_insert_into_group_carries_rows(self):
        data = [{"id": "g", "group": True, "config": [{"id": "x", "module": "m"}]}]
        out = apply_entry_patches(data, [
            {"id": "g", "insert": [{"id": "y", "module": "m2"}]}])
        group = out[0]
        self.assertEqual([e["id"] for e in group["config"]], ["x", "y"])
        self.assertEqual([e["id"] for e in data[0]["config"]], ["x"], "输入必须只读（热重载可反复应用）")

    def test_insert_into_non_group_warns(self):
        data = [{"id": "a", "module": "m"}]
        out = apply_entry_patches(data, [{"id": "a", "insert": [{"id": "b"}]}], _warn)
        self.assertEqual(out, data)
        self.assertTrue(any("is not a group" in w for w in WARNED))

    def test_named_replace_with_config(self):
        data = [{"id": "a", "module": "m", "config": {"x": 1}}]
        out = apply_entry_patches(data, [{"id": "a", "config": {"x": 2}}], _warn)
        self.assertEqual(out[0]["config"], {"x": 2})
        self.assertEqual(data[0]["config"], {"x": 1}, "输入必须只读")

    def test_name_mismatch_skips(self):
        data = [{"id": "a", "name": "n1", "module": "m"}]
        out = apply_entry_patches(data, [{"id": "a", "name": "n2", "module": "m2"}], _warn)
        self.assertEqual(out[0]["module"], "m")
        self.assertTrue(any("name mismatch" in w for w in WARNED))

    def test_missing_target_and_missing_id_warn(self):
        data = [{"id": "a", "module": "m"}]
        apply_entry_patches(data, [{"id": "not-here", "config": {}}], _warn)
        apply_entry_patches(data, [{"module": "m2"}], _warn)
        self.assertEqual(len(WARNED), 2)

    def test_subsequent_patches_see_inserted_rows(self):
        data = [{"id": "a", "module": "m"}]
        out = apply_entry_patches(data, [
            {"insert": [{"id": "b", "module": "m2"}]},
            {"id": "b", "config": {"k": "v"}},
        ])
        self.assertEqual(out[1]["config"], {"k": "v"})


class TestGroupCarrierBoot(unittest.TestCase):
    def test_group_rows_active_and_ordered(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cordis.yml"
            config.write_text(
                "plugins:\n"
                "  - id: grp\n"
                "    name: cordis:group\n"
                "    group: true\n"
                "    config:\n"
                "      - id: inner\n"
                "        module: miniharness.example_plugins\n"
                "        config:\n"
                "          greeting: inner\n"
                "          service_name: inner_svc\n"
                "      - id: spare\n"
                "        module: miniharness.example_plugins\n"
                "        config:\n"
                "          greeting: spare\n"
                "          service_name: spare_svc\n",
                encoding="utf-8",
            )
            ctx, activations = boot(config)
            self.assertEqual([n for n, _ in activations], ["inner", "spare"])
            self.assertEqual(ctx.get("inner_svc")("名"), "inner, 名!")
            self.assertEqual(ctx.get("spare_svc")("名"), "spare, 名!")

    def test_group_insert_via_boot_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "base.yml"
            config.write_text(
                "plugins:\n"
                "  - id: grp\n"
                "    name: cordis:group\n"
                "    group: true\n"
                "    config:\n"
                "      - id: inner\n"
                "        module: miniharness.example_plugins\n"
                "        config:\n"
                "          service_name: inner_svc\n",
                encoding="utf-8",
            )
            patch = root / "patch.yaml"
            patch.write_text(
                "- insert:\n"
                "    - id: extra\n"
                "      module: miniharness.example_plugins\n"
                "      config:\n"
                "        service_name: extra_svc\n"
                "  id: grp\n",
                encoding="utf-8",
            )
            ctx, activations = boot(config, patch)
            self.assertEqual([n for n, _ in activations], ["inner", "extra"])
            self.assertEqual(ctx.get("inner_svc")("x"), "hello, x!")
            self.assertEqual(ctx.get("extra_svc")("x"), "hello, x!")


class TestJsExprNode(unittest.TestCase):
    def test_membership_not_single_key(self):
        self.assertTrue(is_js_expr({"__jsExpr": "process.env.X"}))
        self.assertTrue(is_js_expr({"__jsExpr": "process.env.X", "extra": 1}))
        self.assertFalse(is_js_expr({"other": 1}))
        self.assertFalse(is_js_expr("plain"))

    def test_resolve_evaluates_env(self):
        os.environ["MINI_TREE_JS"] = "v"
        try:
            self.assertEqual(
                resolve_js_exprs({"k": {"__jsExpr": "process.env.MINI_TREE_JS"}}),
                {"k": "v"})
        finally:
            os.environ.pop("MINI_TREE_JS", None)


class TestLazyJsExpr(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("MINI_TREE_GREETING", None)

    def test_normal_row_evaluates_js_expr_at_activation(self):
        os.environ["MINI_TREE_GREETING"] = "hi-js"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cordis.yml"
            config.write_text(
                "plugins:\n"
                "  - id: greeter\n"
                "    module: miniharness.example_plugins\n"
                "    config:\n"
                "      greeting: !!js process.env.MINI_TREE_GREETING\n",
                encoding="utf-8",
            )
            ctx, activations = boot(config)
            self.assertEqual([n for n, _ in activations], ["greeter"])
            self.assertEqual(ctx.get("greeter")("名"), "hi-js, 名!")


class TestAuditFailures(unittest.TestCase):
    def test_missing_inject_service_names_dependency(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cordis.yml"
            config.write_text(
                "plugins:\n"
                "  - id: needsvc\n"
                "    module: miniharness.example_plugins\n"
                "    inject:\n"
                "      nosvc: {}\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "nosvc"):
                boot(config)


if __name__ == "__main__":
    unittest.main()