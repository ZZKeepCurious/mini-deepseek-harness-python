"""boot plugin-market + pluginRegistryProbe（Python 索引 wire）。"""
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from miniharness.boot import boot
from miniharness.boot.config_editor import install_config_editor
from miniharness.boot.plugin_market import MarketFault, install_plugin_market
from miniharness.boot.plugin_registry_probe import install_plugin_registry_probe
from miniharness.boot.profile import (
    PROFILE_PATCH_FILENAME,
    init_profile,
    resolve_profile_dir,
)
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.settings.forms import install_settings_forms
from miniharness.web.api import WebApi
from miniharness.web.args import validate_args


def _sample_index():
    return {
        "format": 1,
        "title": {"en": "Python Market"},
        "metadata": {"categories": {}, "labels": {}, "sortKeys": {}},
        "plugins": [{
            "name": "demo-greeter",
            "title": {"en": "Greeter"},
            "versions": [{
                "version": "1.0.0",
                "publishTimestamp": 1,
                "source": {
                    "type": "python",
                    "module": "miniharness.example_plugins",
                    "pip": "mini-deepseek-harness-python",
                },
            }],
        }],
    }


class _IndexHandler(BaseHTTPRequestHandler):
    payload: bytes = b"{}"

    def do_GET(self):  # noqa: N802
        self.send_response(200)
        self.send_header("content-type", "application/json")
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *_args):
        return


class PluginMarketTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = os.path.abspath(self._tmp.name)
        self.config_file = os.path.join(self.home, "cordis.yml")
        with open(self.config_file, "w", encoding="utf-8") as handle:
            handle.write(
                "plugins:\n"
                "  - id: plugin-market\n"
                "    module: miniharness.cli.plugins.system_prompt\n")
        self.ctx, _ = boot(self.config_file)
        self.addCleanup(self.ctx.dispose)
        profile_dir = resolve_profile_dir("web", self.home)
        init_profile(profile_dir, [])
        patch_path = os.path.join(profile_dir, PROFILE_PATCH_FILENAME)
        self.editor = install_config_editor(
            self.ctx, profile_dir=profile_dir, patch_path=patch_path, home=self.home)
        install_settings_forms(self.ctx, config_editor=self.editor, profile_home=self.home)
        install_plugin_registry_probe(self.ctx, registryProbeEnabled=False)
        self.market = install_plugin_market(self.ctx, self.editor, indexes=[])

        _IndexHandler.payload = json.dumps(_sample_index()).encode("utf-8")
        self._server = HTTPServer(("127.0.0.1", 0), _IndexHandler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        self.addCleanup(self._shutdown_server)
        self.index_url = f"http://127.0.0.1:{self._server.server_address[1]}/index.json"

    def _shutdown_server(self):
        self._server.shutdown()
        self._thread.join(timeout=5)

    def test_set_indexes_search_and_python_install_target(self):
        self.market.set_indexes([self.index_url])
        summaries = self.market.list_indexes()
        self.assertEqual(len(summaries), 1)
        self.assertEqual(summaries[0]["url"], self.index_url)
        result = self.market.search(self.index_url, {
            "sort": {"kind": "index"},
            "offset": 0,
            "limit": 10,
            "text": "greeter",
        })
        self.assertEqual(result["total"], 1)
        entry = result["entries"][0]
        self.assertEqual(entry["name"], "demo-greeter")
        install = entry["versions"][0]["install"]
        self.assertEqual(install["kind"], "python")
        self.assertEqual(install["module"], "miniharness.example_plugins")

    def test_read_entry_detail_returns_none_without_npm_git_fetch(self):
        self.market.set_indexes([self.index_url])
        detail = self.market.read_entry_detail(
            self.index_url, "demo-greeter", "1.0.0", "en")
        self.assertEqual(detail, {"status": "none"})

    def test_registry_probe_disabled_returns_null(self):
        probe = self.ctx.get("pluginRegistryProbe")
        self.assertIsNone(probe.fastest())

    def test_web_endpoints(self):
        api = WebApi(self.ctx, FakeLlmAdapter())
        self.market.set_indexes([self.index_url])
        listed = api.dispatch("pluginMarket/listIndexes", "r1", {})
        self.assertTrue(listed["result"]["ok"], listed["result"])
        search = api.dispatch("pluginMarket/search", "r2", {
            "url": self.index_url,
            "query": {"sort": {"kind": "index"}, "offset": 0, "limit": 5},
        })
        self.assertTrue(search["result"]["ok"], search["result"])
        probe = api.dispatch("pluginRegistryProbe/fastest", "r3", {})
        self.assertTrue(probe["result"]["ok"], probe["result"])
        self.assertIsNone(probe["result"]["value"])
        validate_args("pluginMarket/setIndexes", {"urls": [self.index_url]})

    def test_invalid_index_url_on_set_indexes(self):
        with self.assertRaises(MarketFault) as error:
            self.market.set_indexes(["not-a-url"])
        self.assertEqual(error.exception.code, "plugin-market/invalid-index-url")
