"""app 归因头测试：对照上游 packages/llm/llm/tests/attribution.spec.ts。"""
import unittest

from miniharness.core.version import __version__
from miniharness.llm import APP_IDENTITY, AppIdentity, attribution_headers, user_agent

FORK = AppIdentity(
    product="fork-agent",
    version="9.9.9",
    url="https://example.com/fork-agent",
)


class TestAppIdentity(unittest.TestCase):
    def test_version_comes_from_the_single_version_source(self):
        # attribution.spec.ts:16-18：版本来自包元数据，不是手抄常量。
        self.assertEqual(APP_IDENTITY.version, __version__)

    def test_carries_only_static_public_product_facts(self):
        self.assertEqual(APP_IDENTITY.product, "mini-harness")
        self.assertEqual(
            APP_IDENTITY.url,
            "https://github.com/mini-harness/mini-deepseek-harness-python")


class TestUserAgent(unittest.TestCase):
    def test_renders_product_version_with_plus_url_comment(self):
        self.assertEqual(
            user_agent(),
            f"mini-harness/{__version__} "
            "(+https://github.com/mini-harness/mini-deepseek-harness-python)")

    def test_renders_a_custom_identity(self):
        self.assertEqual(
            user_agent(FORK),
            "fork-agent/9.9.9 (+https://example.com/fork-agent)")


class TestAttributionHeaders(unittest.TestCase):
    def test_defaults_to_user_agent_and_nothing_else(self):
        # attribution.spec.ts:41-44：缺省只有 user-agent。
        self.assertEqual(attribution_headers(), {"user-agent": user_agent()})

    def test_maps_a_custom_identity_onto_user_agent_only(self):
        self.assertEqual(
            attribution_headers(FORK),
            {"user-agent": "fork-agent/9.9.9 (+https://example.com/fork-agent)"})


if __name__ == "__main__":
    unittest.main()
