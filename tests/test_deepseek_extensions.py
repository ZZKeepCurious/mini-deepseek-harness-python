import json
import unittest

from miniharness.llm.deepseek_extensions import (
    ExtensionRegistry,
    prepare_request_extensions,
)
from miniharness.llm.plugin_package_inventory_deepseek import (
    _distribution_of,
    _identity_from_distribution,
    collect_active_plugin_packages,
    install_plugin_package_inventory,
)
from miniharness.llm.protocol import LlmFailure, REQUEST_EXTENSION


class RegistryTest(unittest.TestCase):
    def test_blank_field_rejected(self):
        registry = ExtensionRegistry()
        with self.assertRaisesRegex(ValueError, "non-blank trimmed string"):
            registry.register("", {"prepare": lambda _r: None})
        with self.assertRaisesRegex(ValueError, "non-blank trimmed string"):
            registry.register(" spaced ", {"prepare": lambda _r: None})

    def test_duplicate_field_rejected(self):
        registry = ExtensionRegistry()
        registry.register("a", {"prepare": lambda _r: None})
        with self.assertRaisesRegex(ValueError, "already registered"):
            registry.register("a", {"prepare": lambda _r: None})

    def test_disposer_releases_field(self):
        registry = ExtensionRegistry()
        dispose = registry.register("a", {"prepare": lambda _r: {"value": 1}})
        dispose()
        self.assertEqual(registry.prepare({})["fields"], {})

    def test_none_result_omitted(self):
        registry = ExtensionRegistry()
        registry.register("a", {"prepare": lambda _r: None})
        registry.register("b", {"prepare": lambda _r: {"value": 2}})
        self.assertEqual(registry.prepare({})["fields"], {"b": 2})

    def test_failed_preparation_omitted_and_warned_once(self):
        registry = ExtensionRegistry()
        calls = {"n": 0}

        def prepare(_request):
            calls["n"] += 1
            raise RuntimeError("boom")

        registry.register("a", {"prepare": prepare})
        with self.assertLogs("miniharness.llm.deepseek_extensions",
                             level="WARNING") as logs:
            self.assertEqual(registry.prepare({})["fields"], {})
            self.assertEqual(registry.prepare({})["fields"], {})
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(logs.output), 1)

    def test_value_cloned_not_aliased(self):
        registry = ExtensionRegistry()
        original = {"nested": [1]}
        registry.register("a", {"prepare": lambda _r: {"value": original}})
        fields = registry.prepare({})["fields"]
        original["nested"].append(2)
        self.assertEqual(fields["a"], {"nested": [1]})

    def test_unserializable_value_omitted(self):
        registry = ExtensionRegistry()
        registry.register("a", {"prepare": lambda _r: {"value": float("nan")}})
        self.assertEqual(registry.prepare({})["fields"], {})

    def test_accept_runs_after_prepare(self):
        registry = ExtensionRegistry()
        seen = []
        registry.register("a", {"prepare": lambda _r: {
            "value": 1, "accept": lambda: seen.append("accepted")}})
        prepared = registry.prepare({})
        self.assertEqual(seen, [])
        prepared["accept"]()
        self.assertEqual(seen, ["accepted"])


class PrepareRequestExtensionsTest(unittest.TestCase):
    def test_none_registry_is_passthrough(self):
        body = {"messages": []}
        payload, accept = prepare_request_extensions(body, {}, None)
        self.assertIs(payload, body)
        accept()

    def test_fields_merged_into_payload(self):
        registry = ExtensionRegistry()
        registry.register("dsh_plugin_packages",
                          {"prepare": lambda _r: {"value": {"version": 1, "packages": []}}})
        payload, _accept = prepare_request_extensions({"messages": []}, {}, registry)
        self.assertEqual(payload["messages"], [])
        self.assertEqual(payload["dsh_plugin_packages"],
                         {"version": 1, "packages": []})

    def test_collision_raises(self):
        registry = ExtensionRegistry()
        registry.register("messages", {"prepare": lambda _r: {"value": []}})
        with self.assertRaises(LlmFailure) as caught:
            prepare_request_extensions({"messages": []}, {}, registry)
        self.assertEqual(caught.exception.code, REQUEST_EXTENSION)

    def test_prepare_failure_raises_request_extension(self):
        class BrokenRegistry:
            def prepare(self, _request):
                raise RuntimeError("nope")

        with self.assertRaises(LlmFailure) as caught:
            prepare_request_extensions({}, {}, BrokenRegistry())
        self.assertEqual(caught.exception.code, REQUEST_EXTENSION)

    def test_unserializable_field_falls_back_to_base(self):
        class RawRegistry:
            def prepare(self, _request):
                return {"fields": {"b": {"bad": float("nan")}},
                        "accept": lambda: None}

        omitted = []
        body = {"messages": []}
        payload, accept = prepare_request_extensions(
            body, {}, RawRegistry(),
            on_omitted=lambda fields, error: omitted.append((fields, error)))
        self.assertIs(payload, body)
        self.assertEqual(omitted[0][0], ["b"])
        self.assertIsInstance(omitted[0][1], ValueError)
        accept()  # no-op accept 不应抛出

    def test_unserializable_base_body_raises(self):
        class RawRegistry:
            def prepare(self, _request):
                return {"fields": {"b": {"ok": 1}}, "accept": lambda: None}

        body = {"messages": [], "temperature": float("nan")}
        with self.assertRaises(ValueError):
            prepare_request_extensions(body, {}, RawRegistry())

    def test_accept_failure_reported_not_raised(self):
        registry = ExtensionRegistry()

        def explode():
            raise RuntimeError("late")

        registry.register("a", {"prepare": lambda _r: {"value": 1, "accept": explode}})
        failures = []
        # 传入 on_unaccepted 时，accept 的真实异常被吞并上报而不使请求失败。
        payload, accept = prepare_request_extensions(
            {}, {}, registry, on_unaccepted=failures.append)
        accept()
        self.assertEqual(len(failures), 1)
        self.assertEqual(payload, {"a": 1})


class PluginPackageInventoryTest(unittest.TestCase):
    def test_distribution_of_top_level_package(self):
        from importlib import metadata
        distributions = metadata.packages_distributions()
        self.assertEqual(_distribution_of("miniharness.llm.fake", distributions),
                         "mini-deepseek-harness-python")

    def test_distribution_of_loose_module_is_none(self):
        from importlib import metadata
        distributions = metadata.packages_distributions()
        self.assertIsNone(_distribution_of("__main__", distributions))
        self.assertIsNone(_distribution_of("", distributions))

    def test_identity_carries_version_when_present(self):
        identity = _identity_from_distribution("mini-deepseek-harness-python")
        self.assertEqual(identity["name"], "mini-deepseek-harness-python")
        self.assertIn("version", identity)
        self.assertTrue(identity["version"])

    def test_identity_unknown_distribution_is_none(self):
        self.assertIsNone(_identity_from_distribution("definitely-not-installed-xyz"))

    def test_collect_active_plugin_packages(self):
        from miniharness.core.scope import Context
        ctx = Context(name="inventory")
        # 一个真实插件函数，其 __module__ 落在 miniharness 包内，故可映射到发行版。
        def sample_plugin(_ctx, _config=None):
            return None

        sample_plugin.__module__ = "miniharness.llm.fake"
        ctx.plugin(sample_plugin)
        packages = collect_active_plugin_packages(ctx)
        self.assertIn({"name": "mini-deepseek-harness-python",
                       "version": _identity_from_distribution(
                           "mini-deepseek-harness-python")["version"]}, packages)

    def test_collect_skips_unmapped_modules(self):
        from miniharness.core.scope import Context
        ctx = Context(name="inventory-loose")

        def loose_plugin(_ctx, _config=None):
            return None

        loose_plugin.__module__ = "__main__"
        ctx.plugin(loose_plugin)
        self.assertEqual(collect_active_plugin_packages(ctx), [])

    def test_install_registers_dsh_plugin_packages(self):
        from miniharness.core.scope import Context
        ctx = Context(name="inventory-install")
        registry = ExtensionRegistry()
        dispose = install_plugin_package_inventory(ctx, registry)
        fields = registry.prepare({})["fields"]
        self.assertEqual(fields["dsh_plugin_packages"]["version"], 1)
        self.assertIsInstance(fields["dsh_plugin_packages"]["packages"], list)
        dispose()
        self.assertEqual(registry.prepare({})["fields"], {})

    def test_install_disabled_is_noop(self):
        registry = ExtensionRegistry()
        install_plugin_package_inventory(None, registry, enabled=False)
        self.assertEqual(registry.prepare({})["fields"], {})


class InventoryValueIsJsonTest(unittest.TestCase):
    def test_value_serializable(self):
        from miniharness.llm.plugin_package_inventory_deepseek import (
            install_plugin_package_inventory as install,
        )
        from miniharness.core.scope import Context
        ctx = Context(name="inventory-json")
        registry = ExtensionRegistry()
        install(ctx, registry)
        fields = registry.prepare({})["fields"]
        json.dumps(fields["dsh_plugin_packages"])


if __name__ == "__main__":
    unittest.main()
