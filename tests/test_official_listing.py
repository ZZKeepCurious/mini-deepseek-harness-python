"""official listing：listed-catalog 解析与 badge 解码。"""
import unittest

from miniharness.llm.deepseek_account import CatalogRefusal, parse_model_listing
from miniharness.llm.deepseek_files import DEFAULT_MODELS
from miniharness.llm.deepseek_files.model_info import catalog_model_info
from miniharness.llm.listed_catalog import refuse_listed_model, resolve_listed_catalog
from miniharness.llm.protocol import APP_IDENTITY, CLIENT_UPGRADE_REQUIRED, LlmFailure


class ListedCatalogTest(unittest.TestCase):
    def test_badge_on_discover_models(self):
        body = {
            "data": [{
                "id": "deepseek-flash",
                "api_capabilities": {"anthropic_messages": {}},
                "client_metadata": {
                    APP_IDENTITY.product: {
                        "badge": {"text": "New", "tone": "info"},
                    },
                },
            }],
        }
        listing = parse_model_listing(body)
        catalog = resolve_listed_catalog(
            listing=listing.entries,
            supported=DEFAULT_MODELS,
            overrides={},
            route_max_tokens=8192,
            client_version=APP_IDENTITY.version,
        )
        self.assertIn("deepseek-flash", catalog.badges)
        row = catalog_model_info(
            "deepseek-official",
            catalog.offered[0],
            catalog.badges.get("deepseek-flash"),
        )
        self.assertEqual(row["badge"]["tone"], "info")
        self.assertEqual(row["badge"]["text"]["en"], "New")

    def test_refuse_listed_model_upgrade(self):
        with self.assertRaises(LlmFailure) as ctx:
            refuse_listed_model("m", CatalogRefusal("upgrade-required", "9.9.9"))
        self.assertEqual(ctx.exception.code, CLIENT_UPGRADE_REQUIRED)


if __name__ == "__main__":
    unittest.main()
