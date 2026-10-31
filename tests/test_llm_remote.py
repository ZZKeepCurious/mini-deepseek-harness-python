import unittest

from miniharness.core.scope import Context
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import WebApi


class LlmRemoteTest(unittest.TestCase):
    def setUp(self):
        ctx = Context(name="llm-remote")
        self.addCleanup(ctx.dispose)
        self.api = WebApi(ctx, FakeLlmAdapter())

    def test_list_configurable_providers_from_adapter(self):
        rows = self.api.llm_list_configurable_providers({})
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "fake")
        self.assertEqual(rows[0]["settingsNs"], "credentials")

    def test_discover_models_for_configured_route(self):
        rows = self.api.llm_discover_models({
            "settingsNs": "credentials",
            "request": {"provider": "fake"},
        })
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "fake-model")

    def test_discover_models_rejects_unknown_provider(self):
        from miniharness.web.api import _Reject

        with self.assertRaises(_Reject) as ctx:
            self.api.llm_discover_models({
                "settingsNs": "credentials",
                "request": {"provider": "unknown"},
            })
        self.assertEqual(ctx.exception.code, "llm/model-discovery-rejected")


if __name__ == "__main__":
    unittest.main()
