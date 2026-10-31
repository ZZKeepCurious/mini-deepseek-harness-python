"""official listing：listed-catalog 解析与 badge 解码。"""
import asyncio
import threading
import time
import unittest

import httpx

from miniharness.core.agent_loop.resident_loop import shutdown_resident_loop
from miniharness.llm.deepseek import DeepSeekAdapter
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


def _usable_model(mid: str) -> dict:
    return {"id": mid, "api_capabilities": {"anthropic_messages": {}}}


class OfficialListingRefreshTest(unittest.TestCase):
    def tearDown(self):
        shutdown_resident_loop()

    def test_available_models_waits_off_event_loop(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                return httpx.Response(200, json={"data": [_usable_model("deepseek-flash")]})
            return httpx.Response(404)

        adapter = DeepSeekAdapter(
            api_key="sk-test",
            transport=httpx.MockTransport(handler),
        )
        models = adapter.available_models()
        ids = {row["model"] for row in models}
        self.assertIn("deepseek-flash", ids)

    def test_abandonable_refresh_while_event_loop_running(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path.endswith("/models"):
                time.sleep(3)
                return httpx.Response(200, json={"data": [_usable_model("deepseek-flash")]})
            return httpx.Response(404)

        adapter = DeepSeekAdapter(
            api_key="sk-test",
            transport=httpx.MockTransport(handler),
            models_timeout_ms=150,
        )
        loop = asyncio.new_event_loop()
        ready = threading.Event()

        def loop_thread() -> None:
            asyncio.set_event_loop(loop)
            ready.set()
            loop.run_forever()

        threading.Thread(target=loop_thread, daemon=True).start()
        ready.wait()
        outcome: dict = {}

        def on_loop() -> None:
            asyncio.set_event_loop(loop)
            start = time.monotonic()
            outcome["models"] = adapter.available_models()
            outcome["elapsed"] = time.monotonic() - start

        loop.call_soon_threadsafe(on_loop)
        deadline = time.monotonic() + 1.0
        while "elapsed" not in outcome and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertIn("elapsed", outcome)
        self.assertLess(outcome["elapsed"], 0.5)
        self.assertGreater(len(outcome["models"]), 0)
        loop.call_soon_threadsafe(loop.stop)


if __name__ == "__main__":
    unittest.main()
