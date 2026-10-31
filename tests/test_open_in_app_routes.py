import unittest

from fastapi.testclient import TestClient

from miniharness.core.scope import Context
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import WebApi
from miniharness.web.server import create_app


class OpenInAppRoutesTest(unittest.TestCase):
    def setUp(self):
        ctx = Context(name="open-in-app-routes")
        self.addCleanup(ctx.dispose)
        api = WebApi(ctx, FakeLlmAdapter())
        self.client = TestClient(create_app(api, api.gateway))

    def test_apps_route_returns_empty_catalog(self):
        response = self.client.get("/open-in-app/apps")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"apps": []})

    def test_open_route_is_unavailable(self):
        response = self.client.post(
            "/open-in-app/open",
            json={"app": "finder", "path": "C:\\\\work"},
        )
        self.assertEqual(response.status_code, 503)


if __name__ == "__main__":
    unittest.main()
