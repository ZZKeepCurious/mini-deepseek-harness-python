"""boot plugin-manager（对齐 packages/boot/plugin-manager/src/index.ts 的 mini 子集）。"""
import os
import tempfile
import unittest

from miniharness.boot import boot
from miniharness.boot.config_editor import install_config_editor
from miniharness.boot.plugin_manager import install_plugin_manager
from miniharness.boot.profile import (
    PROFILE_PATCH_FILENAME,
    init_profile,
    load_profile_directory,
    resolve_profile_dir,
)
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import WebApi
from miniharness.web.args import BoundaryReject, validate_args


class PluginManagerTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.abspath(self._tmp.name)
        self.config_file = os.path.join(self.home, "cordis.yml")
        with open(self.config_file, "w", encoding="utf-8") as handle:
            handle.write(
                "plugins:\n"
                "  - id: greeter\n"
                "    module: miniharness.example_plugins\n"
                "    config:\n"
                "      greeting: hi\n")
        self.ctx, _ = boot(self.config_file)
        self.addCleanup(self.ctx.dispose)
        profile_dir = resolve_profile_dir("web", self.home)
        init_profile(profile_dir, [])
        self.patch_path = os.path.join(profile_dir, PROFILE_PATCH_FILENAME)
        self.editor = install_config_editor(
            self.ctx, profile_dir=profile_dir, patch_path=self.patch_path,
            home=self.home)
        self.manager = install_plugin_manager(self.ctx, self.editor)

    def _row(self, entry_id="include:greeter"):
        return next(item for item in self.manager.list_plugins()
                    if item["entryId"] == entry_id)

    def _patch_rows(self):
        profile = load_profile_directory("miniharness",
                                         os.path.dirname(self.patch_path))
        return [row for row in profile.patches if row.get("id") == "greeter"]

    def test_list_plugins_projects_addressable_patch_target(self):
        row = self._row()
        self.assertEqual(row["moduleName"], "miniharness.example_plugins")
        self.assertTrue(row["enabled"])
        self.assertEqual(row["patchId"], "greeter")
        self.assertNotIn("readOnlyReason", row)
        self.assertEqual(row["fiberPhase"], "active")

    def test_set_plugin_enabled_disables_and_reenables(self):
        events = []
        self.ctx.on("plugin-manager/changed", events.append, global_=True)
        result = self.manager.set_plugin_enabled("include:greeter", False)
        self.assertTrue(result["changed"])
        self.assertEqual(result["application"], "applied")
        self.assertNotIn("error", result)
        self.assertEqual(self._patch_rows(), [{"id": "greeter", "disabled": True}])
        self.assertIsNone(self.ctx.get("greeter"))
        self.assertFalse(self._row()["enabled"])
        self.assertEqual(events, [{"reason": "plugin"}])

        again = self.manager.set_plugin_enabled("include:greeter", False)
        self.assertFalse(again["changed"])
        self.assertEqual(again["application"], "applied")

        result = self.manager.set_plugin_enabled("include:greeter", True)
        self.assertTrue(result["changed"])
        self.assertEqual(result["application"], "applied")
        self.assertEqual(self._patch_rows(), [{"id": "greeter", "disabled": False}])
        self.assertEqual(self.ctx.get("greeter")("x"), "hi, x!")
        self.assertTrue(self._row()["enabled"])

    def test_unknown_plugin_is_reported_not_raised(self):
        result = self.manager.set_plugin_enabled("include:missing", False)
        self.assertFalse(result["changed"])
        self.assertEqual(result["application"], "failed")
        self.assertEqual(result["error"], {"code": "unknown-plugin"})

    def test_install_is_idempotent(self):
        self.assertIs(install_plugin_manager(self.ctx, self.editor), self.manager)

    def test_web_endpoints_dispatch_through_api(self):
        api = WebApi(self.ctx, FakeLlmAdapter())
        listed = api.dispatch("pluginManager/listPlugins", "r1", {})
        self.assertTrue(listed["result"]["ok"], listed["result"])
        rows = listed["result"]["value"]
        by_id = {row["entryId"]: row for row in rows}
        self.assertTrue(by_id["include:greeter"]["enabled"])
        self.assertEqual(by_id["include"]["readOnlyReason"], "unaddressable")
        inventory = api.dispatch("pluginInventory/list", "r1b", {})
        self.assertTrue(inventory["result"]["value"]["managementAvailable"])
        disabled = api.dispatch("pluginManager/setPluginEnabled", "r2",
                                {"id": "include:greeter", "enabled": False})
        self.assertTrue(disabled["result"]["ok"], disabled["result"])
        self.assertTrue(disabled["result"]["value"]["changed"])
        self.assertIsNone(self.ctx.get("greeter"))
        missing = api.dispatch("pluginManager/setPluginEnabled", "r3",
                               {"id": "include:missing", "enabled": False})
        self.assertTrue(missing["result"]["ok"])
        self.assertEqual(missing["result"]["value"]["error"],
                         {"code": "unknown-plugin"})

    def test_args_boundary_matches_wire_contract(self):
        validate_args("pluginManager/listPlugins", {})
        validate_args("pluginManager/setPluginEnabled",
                      {"id": "include:greeter", "enabled": False})
        with self.assertRaises(BoundaryReject) as caught:
            validate_args("pluginManager/setPluginEnabled", {"id": "include:greeter"})
        self.assertEqual(caught.exception.code, "gateway/arguments-invalid")
        with self.assertRaises(BoundaryReject) as caught:
            validate_args("pluginManager/setPluginEnabled",
                          {"id": "include:greeter", "enabled": "yes"})
        self.assertEqual(caught.exception.code, "gateway/input-invalid")


if __name__ == "__main__":
    unittest.main()
