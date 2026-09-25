"""settings-controller：settings / credentials 两个 namespace 的写读与拒绝。

对齐 packages/api/settings-controller（index.ts / credentials.ts 的确定性面）。
"""

import os
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.seams.credentials_local import LocalCredentialProvider, install_credentials
from miniharness.settings import install_settings
from miniharness.settings_controller import SettingsFault, install_settings_controller


class StubPreset:
    def __init__(self, preset_id, trust, path):
        self.id = preset_id
        self.trust = trust
        self.path = path


class StubRoster:
    def __init__(self, presets):
        self._presets = presets

    def resolve(self, preset_id):
        for preset in self._presets:
            if preset.id == preset_id:
                return preset
        from miniharness.preset.presets import UnknownPresetError
        raise UnknownPresetError(preset_id, sorted(p.id for p in self._presets))

    def ids(self):
        return [preset.id for preset in self._presets]


class SettingsControllerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx = Context(name="settings-controller-test")
        self.addCleanup(self.ctx.dispose)
        self.settings = install_settings(self.ctx, path=os.path.join(self._tmp.name, "settings.json"))
        self.settings.register("demo", defaults={"level": 1, "api": {"key": None}},
                               secrets=[["api", "key"]])
        self.credentials_provider = LocalCredentialProvider(
            filename=os.path.join(self._tmp.name, "credentials.json"))
        install_credentials(self.ctx, self.credentials_provider)
        self.roster = StubRoster([StubPreset("standard", "system", os.path.join(self._tmp.name, "standard.json")),
                                  StubPreset("mine", "user", os.path.join(self._tmp.name, "user", "mine.json"))])
        self.controller = install_settings_controller(self.ctx, roster=self.roster)
        self.credentials = self.ctx.get("credentialsController")

    def _code(self, call):
        with self.assertRaises(SettingsFault) as caught:
            call()
        return caught.exception.code

    def test_describe_returns_redacted_namespaces(self):
        described = self.controller.describe()
        self.assertTrue(described["writable"])
        self.assertTrue(described["hasDocument"])
        entry = described["namespaces"][0]
        self.assertEqual(entry["ns"], "demo")
        self.assertEqual(entry["schema"], {})
        self.assertEqual(entry["value"]["level"], 1)
        self.assertEqual(entry["secrets"], [{"path": ["api", "key"], "set": False}])

    def test_writes_return_the_new_view_and_report_conflicts(self):
        updated = self.controller.update("demo", {"level": 2}, None)
        self.assertEqual(updated["value"]["level"], 2)
        self.assertEqual(updated["revision"], 1)
        conflict = self._code(lambda: self.controller.replace("demo", {"level": 3}, 0))
        self.assertEqual(conflict, "settings/conflict")
        replaced = self.controller.replace("demo", {"level": 9}, 1)
        self.assertEqual(replaced["value"]["level"], 9)
        mutated = self.controller.mutate("demo", [{"op": "set", "path": ["level"], "value": 5}], 2)
        self.assertEqual(mutated["value"]["level"], 5)

    def test_unknown_namespace_is_rejected(self):
        self.assertEqual(self._code(lambda: self.controller.update("nope", {}, None)),
                         "settings/rejected")
        self.assertEqual(self._code(lambda: self.controller.update("", {}, None)),
                         "gateway/bad-request")

    def test_missing_settings_provider_is_reported(self):
        bare = Context(name="no-settings")
        self.addCleanup(bare.dispose)
        controller = install_settings_controller(bare, roster=None)
        self.assertEqual(self._code(lambda: controller.describe()), "gateway/internal")

    def test_native_document_open_is_unavailable(self):
        # rc.1 删 `canOpenAgentPresetDirectory`/`openAgentPresetDirectory`（及 `nativeOpen`）；
        # 文档打开仍有 provider 缺席折 gateway/internal 的语义。
        self.assertFalse(hasattr(self.controller, "can_open_agent_preset_directory"))
        self.assertFalse(hasattr(self.controller, "open_agent_preset_directory"))
        self.assertEqual(self._code(lambda: self.controller.open_settings_document()),
                         "gateway/internal")

    def test_credentials_describe_set_unset(self):
        described = self.credentials.describe(["MY_KEY"])
        self.assertEqual(described, {"MY_KEY": {"configured": False, "writable": True}})
        self.credentials.set("MY_KEY", "secret-value")
        self.assertEqual(self.credentials.describe(["MY_KEY"]),
                         {"MY_KEY": {"configured": True, "source": "file", "writable": True}})
        self.credentials.unset("MY_KEY")
        self.assertEqual(self.credentials.describe(["MY_KEY"]),
                         {"MY_KEY": {"configured": False, "writable": True}})

    def test_credentials_reject_invalid_payloads(self):
        self.assertEqual(self._code(lambda: self.credentials.describe(["not-a-ref"])),
                         "gateway/bad-request")
        self.assertEqual(self._code(lambda: self.credentials.set("X", "")),
                         "gateway/bad-request")
        self.assertEqual(self._code(lambda: self.credentials.unset("1BAD")),
                         "gateway/bad-request")

    def test_credentials_missing_provider_is_reported(self):
        bare = Context(name="no-credentials")
        self.addCleanup(bare.dispose)
        install_settings(bare, path=os.path.join(self._tmp.name, "s3.json"))
        install_settings_controller(bare, roster=None)
        controller = bare.get("credentialsController")
        self.assertEqual(self._code(lambda: controller.describe(["X"])), "gateway/internal")


if __name__ == "__main__":
    unittest.main()
