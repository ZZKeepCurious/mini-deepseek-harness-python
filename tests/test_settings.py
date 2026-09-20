"""settings 域验收（对齐 packages/settings/settings + settings-file）。"""
import json
import os
import pathlib
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.settings import (
    SettingsConflictError,
    SettingsFileProvider,
    SettingsProvider,
    install_settings,
    parse_settings_namespace,
    redact_secrets,
)


class TestHelpers(unittest.TestCase):
    def test_namespace_pattern(self):
        self.assertEqual(parse_settings_namespace("llm-deepseek"), "llm-deepseek")
        for bad in ("Llm", "1abc", "a_b", ""):
            with self.assertRaises(TypeError):
                parse_settings_namespace(bad)

    def test_redact_secrets(self):
        value = {"apiKey": "s3cret", "nested": {"token": "t", "model": "m"}}
        redacted, views = redact_secrets(value, [["apiKey"], ["nested", "token"]])
        self.assertEqual(redacted, {"nested": {"model": "m"}})
        self.assertEqual(views, [{"path": ["apiKey"], "set": True},
                                 {"path": ["nested", "token"], "set": True}])


class SettingsCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        self.settings = SettingsProvider(self.ctx)
        self.events = []
        self.ctx.on("settings/updated", lambda p: self.events.append(p))

    def tearDown(self):
        self.ctx.dispose()

    def test_layering_defaults_base_user(self):
        scope = self.settings.register("llm", defaults={"model": "d", "temp": 1},
                                       base={"temp": 2})
        self.assertEqual(scope.get(), {"model": "d", "temp": 2})
        self.assertIsNone(self.settings._user_section("llm"))

    async def test_update_merges_and_emits(self):
        scope = self.settings.register("llm", defaults={"model": "d", "temp": 1})
        seen = []
        scope.watch(lambda nxt, prev: seen.append((nxt, prev)))
        await scope.update({"temp": 5})
        self.assertEqual(scope.get(), {"model": "d", "temp": 5})
        self.assertEqual(self.settings._user_section("llm"), {"temp": 5})
        self.assertEqual(seen[-1][0], {"model": "d", "temp": 5})
        self.assertEqual(self.events[-1]["source"], "update")

    async def test_replace_and_mutate_unset(self):
        scope = self.settings.register("llm", defaults={"a": 1, "b": 2})
        await scope.update({"a": 9, "b": 8})
        await scope.mutate([{"op": "unset", "path": ["a"]}])
        self.assertEqual(scope.get(), {"a": 1, "b": 8})
        await scope.replace({})
        self.assertEqual(scope.get(), {"a": 1, "b": 2})

    async def test_conflict_on_stale_revision(self):
        scope = self.settings.register("llm", defaults={"a": 1})
        with self.assertRaises(SettingsConflictError) as cm:
            await scope.update({"a": 2}, expected_revision=99)
        self.assertEqual(cm.exception.code, "SETTINGS_CONFLICT")

    async def test_validate_rejects_bad_value(self):
        def validate(value):
            if value["count"] < 0:
                raise ValueError("count must be >= 0")
        scope = self.settings.register("llm", defaults={"count": 0}, validate=validate)
        with self.assertRaises(ValueError):
            await scope.update({"count": -1})
        self.assertEqual(scope.get(), {"count": 0})

    def test_describe_redacts(self):
        self.settings.register("llm", defaults={"apiKey": "k", "model": "m"},
                               secrets=[["apiKey"]])
        view = self.settings.describe(redact_secrets=True)["namespaces"][0]
        self.assertEqual(view["value"], {"model": "m"})
        self.assertEqual(view["secrets"], [{"path": ["apiKey"], "set": True}])


class SettingsFileCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.ctx = Context(name="root")
        self.path = os.path.join(self._tmp.name, "settings.json")

    def tearDown(self):
        self.ctx.dispose()
        self._tmp.cleanup()

    async def test_file_roundtrip(self):
        provider = SettingsFileProvider(self.ctx, self.path)
        scope = provider.register("llm", defaults={"model": "d"})
        await scope.update({"model": "x"})
        on_disk = json.loads(pathlib.Path(self.path).read_text(encoding="utf-8"))
        self.assertEqual(on_disk, {"llm": {"model": "x"}})

    def test_external_reload(self):
        provider = SettingsFileProvider(self.ctx, self.path)
        scope = provider.register("llm", defaults={"model": "d"})
        pathlib.Path(self.path).write_text("{\"llm\": {\"model\": \"z\"}}", encoding="utf-8")
        provider.reload_from_provider()
        self.assertEqual(scope.get(), {"model": "z"})

    def test_install_idempotent(self):
        first = install_settings(self.ctx)
        self.assertIs(self.ctx.get("settings"), first)
        self.assertIs(install_settings(self.ctx), first)


if __name__ == "__main__":
    unittest.main()
