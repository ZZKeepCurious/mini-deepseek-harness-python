import hashlib
import hmac
import json
import os
import tempfile
import time
import unittest

from fastapi.testclient import TestClient

from miniharness.core.scope import Context
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import WebApi
from miniharness.web.server import create_app
from miniharness.webhook.github_ingress import mount_github_webhook_ingress


class WebhookGithubRoutesTest(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.secret = "test-webhook-secret"
        os.environ["DSH_GITHUB_WEBHOOK_SECRET"] = self.secret
        self.addCleanup(lambda: os.environ.pop("DSH_GITHUB_WEBHOOK_SECRET", None))
        ctx = Context(name="webhook-github-routes")
        self.addCleanup(ctx.dispose)
        self.api = WebApi(ctx, FakeLlmAdapter(), cwd=self._tmpdir.name)
        mount_github_webhook_ingress(
            self.api,
            {
                "path": "/webhook/github/review",
                "source": "review",
                "secretEnv": "DSH_GITHUB_WEBHOOK_SECRET",
                "rule": {
                    "repository": "org/repo",
                    "workspacePath": self._tmpdir.name,
                    "agentPreset": "default",
                    "permissionPreset": "read-only",
                },
            },
        )
        self.client = TestClient(create_app(self.api, self.api.gateway))

    def _sign(self, body: bytes) -> dict[str, str]:
        digest = hmac.new(self.secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
        return {
            "content-type": "application/json",
            "x-hub-signature-256": f"sha256={digest}",
            "x-github-delivery": "delivery-1",
            "x-github-event": "pull_request",
        }

    def test_rejects_invalid_signature(self):
        body = b"{}"
        headers = self._sign(body)
        headers["x-hub-signature-256"] = "sha256=deadbeef"
        response = self.client.post("/webhook/github/review", content=body, headers=headers)
        self.assertEqual(response.status_code, 401)

    def test_rejects_missing_event_header(self):
        body = b"{}"
        headers = self._sign(body)
        del headers["x-github-event"]
        response = self.client.post("/webhook/github/review", content=body, headers=headers)
        self.assertEqual(response.status_code, 400)

    def test_accepts_signed_delivery(self):
        payload = {
            "action": "ready_for_review",
            "number": 7,
            "repository": {"full_name": "org/repo"},
            "pull_request": {
                "title": "Fix things",
                "html_url": "https://example/pr/7",
                "user": {"login": "alice"},
                "base": {"ref": "main", "sha": "base-sha"},
                "head": {"ref": "feature", "sha": "head-sha"},
            },
        }
        body = json.dumps(payload).encode("utf-8")
        response = self.client.post(
            "/webhook/github/review",
            content=body,
            headers=self._sign(body),
        )
        self.assertEqual(response.status_code, 202)
        deadline = time.time() + 5.0
        while time.time() < deadline:
            items = self.api.list_sessions({}).get("items") or []
            if items:
                break
            time.sleep(0.05)
        else:
            self.fail("expected webhook rule to create a session")
        items = self.api.list_sessions({}).get("items") or []
        self.assertEqual(len(items), 1)
        session_id = items[0]["sessionId"]
        found = self.api._agent_for(session_id)
        self.assertIsNotNone(found)


if __name__ == "__main__":
    unittest.main()
