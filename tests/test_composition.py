"""composition.py：YAML 载体 / !!js 子集 / .env / 组合 dump 渲染。"""
import json
import os
import tempfile
import unittest
from pathlib import Path

from miniharness.boot import apply_patch, boot
from miniharness.boot.composition import (
    compose_with_origins,
    evaluate_js_expr,
    load_composition,
    load_dotenv_file,
    load_patch_list,
    render_composition_dump,
    resolve_js_exprs,
)


def _base_entries():
    return [
        {"id": "greeter", "module": "miniharness.example_plugins", "config": {"greeting": "你好"}},
        {"id": "base2", "module": "miniharness.example_plugins", "config": {"service_name": "b"}},
    ]


class TestYamlLoading(unittest.TestCase):
    def test_yaml_config_and_patch_boot(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cordis.yml"
            config.write_text(
                "plugins:\n"
                "  - id: greeter\n"
                "    module: miniharness.example_plugins\n"
                "    config:\n"
                "      greeting: 你好\n",
                encoding="utf-8",
            )
            patch = root / "patch.yaml"
            patch.write_text(
                "- replace:\n"
                "    id: greeter\n"
                "    config:\n"
                "      greeting: 你好呀\n",
                encoding="utf-8",
            )
            ctx, activations = boot(config, patch)
            self.assertEqual([n for n, _ in activations], ["greeter"])
            self.assertEqual(ctx.inject("greeter")("张三"), "你好呀, 张三!")

    def test_yaml_bad_syntax_fails_with_prefix(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yml"
            path.write_text("plugins: [", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "failed to parse config"):
                load_composition(path)

    def test_config_top_level_array_accepted_as_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yml"
            path.write_text("- id: a\n  module: m\n", encoding="utf-8")
            self.assertEqual(load_composition(path)[0]["id"], "a")

    def test_config_top_level_scalar_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.yml"
            path.write_text("just a string\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "顶层必须是对象或数组"):
                load_composition(path)

    def test_patch_top_level_must_be_array(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.yml"
            path.write_text("plugins: []\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "顶层必须是数组"):
                load_patch_list(path)

    def test_patch_entry_must_be_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.yml"
            path.write_text("- replace: {}\n- 42\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "必须是对象"):
                load_patch_list(path)

    def test_missing_file_fails_with_prefix(self):
        with self.assertRaisesRegex(RuntimeError, "failed to read config"):
            load_composition("no_such_file.json")

    def test_unknown_extension_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.toml"
            path.write_text("x = 1\n", encoding="utf-8")
            with self.assertRaisesRegex(RuntimeError, "未知配置扩展名"):
                load_composition(path)

    def test_json_still_works(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "c.json"
            path.write_text(json.dumps({"plugins": [{"id": "a", "module": "x"}]}), encoding="utf-8")
            self.assertEqual(load_composition(path)[0]["id"], "a")


class TestJsExpr(unittest.TestCase):
    def test_evaluate_env(self):
        self.assertEqual(evaluate_js_expr("process.env.MINI_TEST_VAR", {"MINI_TEST_VAR": "v"}), "v")

    def test_unsupported_expression_fails_loud(self):
        with self.assertRaisesRegex(ValueError, "不支持的 !!js 表达式"):
            evaluate_js_expr("globalThis.x", {})

    def test_resolve_recursive(self):
        data = {"a": {"__jsExpr": "process.env.MINI_TEST_VAR"}, "b": [{"__jsExpr": "process.env.MINI_TEST_VAR"}]}
        resolved = resolve_js_exprs(data, {"MINI_TEST_VAR": "v"})
        self.assertEqual(resolved, {"a": "v", "b": ["v"]})

    def test_boot_evaluates_js_expr(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cordis.yml"
            config.write_text(
                "plugins:\n"
                "  - id: greeter\n"
                "    module: miniharness.example_plugins\n"
                "    config:\n"
                "      greeting: !!js process.env.MINI_GREETING\n",
                encoding="utf-8",
            )
            old = os.environ.get("MINI_GREETING")
            os.environ["MINI_GREETING"] = "来自环境"
            try:
                ctx, _ = boot(config)
                self.assertEqual(ctx.inject("greeter")("王五"), "来自环境, 王五!")
            finally:
                if old is None:
                    os.environ.pop("MINI_GREETING", None)
                else:
                    os.environ["MINI_GREETING"] = old

    def test_empty_js_expr_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.yml"
            path.write_text("- id: a\n  config:\n    x: !!js\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                load_patch_list(path)


class TestDotenv(unittest.TestCase):
    def test_missing_file_silent(self):
        warns = []
        load_dotenv_file("no_such.env", warn=warns.append)
        self.assertEqual(warns, [])

    def test_loads_into_environ(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("MINI_ENV_A=1\n# comment\n\nMINI_ENV_B='x y'\n", encoding="utf-8")
            env = {}
            load_dotenv_file(path, environ=env)
            self.assertEqual(env, {"MINI_ENV_A": "1", "MINI_ENV_B": "x y"})

    def test_does_not_override_existing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("MINI_ENV_A=2\n", encoding="utf-8")
            env = {"MINI_ENV_A": "keep"}
            load_dotenv_file(path, environ=env)
            self.assertEqual(env["MINI_ENV_A"], "keep")

    def test_bootstrap_only_name_rejected_before_materialization(self):
        # 上游 readEnvLayer（index.ts:153-162）：bootstrap-only 名字在任何值
        # 物化前整体拒绝；非 bootstrap 名不受影响
        from miniharness.boot.dotenv import is_bootstrap_only
        self.assertTrue(is_bootstrap_only("PATH"))
        self.assertTrue(is_bootstrap_only("PYTHONPATH"))
        self.assertTrue(is_bootstrap_only("DSH_HOME"))
        self.assertTrue(is_bootstrap_only("XDG_CONFIG_HOME"))
        self.assertFalse(is_bootstrap_only("DEEPSEEK_API_KEY"))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / ".env"
            path.write_text("OK_KEY=1\nPATH=/evil\n", encoding="utf-8")
            env = {}
            with self.assertRaisesRegex(ValueError, 'sets "PATH"'):
                load_dotenv_file(path, environ=env)
            # 拒绝发生在物化前：OK_KEY 也未写入
            self.assertEqual(env, {})


class TestComposeAndDump(unittest.TestCase):
    def test_origins_track_replace_and_insert(self):
        combined, records = compose_with_origins(_base_entries(), [("p1.yml", [
            {"replace": {"id": "greeter", "config": {"greeting": "hi"}}},
            {"insert": [{"id": "new1", "module": "m"}]},
        ])])
        # provenance 累积（上游 index.ts:422-436）：replace 改写保留 origin + patchedBy，
        # insert 新行 origin 为层 label
        self.assertEqual(records, [
            {"origin": "base", "patchedBy": ["p1.yml"]},
            {"origin": "base", "patchedBy": []},
            {"origin": "p1.yml", "patchedBy": []},
        ])
        self.assertEqual([e["id"] for e in combined], ["greeter", "base2", "new1"])

    def test_skipped_patch_warns_and_continues(self):
        warns = []
        combined, records = compose_with_origins(_base_entries(), [
            ("bad.yml", [{"replace": {"id": "nope", "config": {}}}]),
            ("ok.yml", [{"insert": [{"id": "new1", "module": "m"}]}]),
        ], warn=warns.append)
        self.assertEqual(len(warns), 1)
        self.assertIn("被跳过", warns[0])
        self.assertEqual([e["id"] for e in combined], ["greeter", "base2", "new1"])
        self.assertEqual(records[-1], {"origin": "ok.yml", "patchedBy": []})

    def test_dump_sections_and_reloadable(self):
        rendered = render_composition_dump("miniharness", "base.yml", _base_entries(), [
            ("patch.yml", [{"replace": {"id": "greeter", "config": {"greeting": "hi"}}}]),
        ])
        # provenance 注释（上游 groupedDump index.ts:460-462）：
        # 改写行显示 "origin, patched by <layer>"
        self.assertIn("# == base.yml, patched by patch.yml", rendered)
        self.assertIn("# == base.yml", rendered)
        self.assertEqual(rendered.count("- id:"), 2)

    def test_dump_js_expr_verbatim(self):
        rendered = render_composition_dump("miniharness", "base.yml", [
            {"id": "g", "module": "m", "config": {"value": {"__jsExpr": "process.env.X"}}},
        ], [])
        self.assertIn("!!js", rendered)
        self.assertIn("process.env.X", rendered)

    def test_dump_roundtrip_reload(self):
        base = _base_entries()
        rendered = render_composition_dump("miniharness", "base.yml", base, [
            ("patch.yml", [{"insert": [{"id": "new1", "module": "m", "config": {"__jsExpr": "process.env.MINI_DUMP"}}]}]),
        ])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "dump.yml"
            path.write_text(rendered, encoding="utf-8")
            reloaded = load_composition(path)
            self.assertEqual([e["id"] for e in reloaded], ["greeter", "base2", "new1"])
            resolved = resolve_js_exprs(reloaded[2]["config"], {"MINI_DUMP": "v"})
            self.assertEqual(resolved, "v")

    def test_dump_skipped_patch_warns_not_fails(self):
        warns = []
        rendered = render_composition_dump("miniharness", "base.yml", _base_entries(), [
            ("bad.yml", [{"replace": {"id": "nope", "config": {}}}]),
        ], warn=warns.append)
        self.assertEqual(len(warns), 1)
        self.assertIn("- id:", rendered)


if __name__ == "__main__":
    unittest.main()