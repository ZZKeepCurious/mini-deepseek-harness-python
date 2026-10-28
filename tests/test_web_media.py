import os
import tempfile
import unittest

from fastapi.testclient import TestClient

from miniharness.attachment.store import LocalAttachmentStore
from miniharness.attachment.types import ImageAttachmentLimits
from miniharness.core.scope import Context
from miniharness.fs.local import LocalFileSystem
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import WebApi
from miniharness.web.server import create_app


class FileRouteTest(unittest.TestCase):
    def setUp(self):
        adapter = FakeLlmAdapter()
        adapter.model = "fake-model"
        self.ctx = Context(name="web-media-test")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.fs = LocalFileSystem(self.ctx, {"cwd": self._tmp.name})
        self.ctx.provide("attachments", LocalAttachmentStore(
            root=os.path.join(self._tmp.name, "attachments"),
            limits=ImageAttachmentLimits(maxImageBytes=8)))
        self.api = WebApi(self.ctx, adapter)
        self.client = TestClient(create_app(self.api, self.api.gateway))
        self.addCleanup(self.client.close)
        self.addCleanup(self.ctx.dispose)
        self.file_path = os.path.join(self._tmp.name, "pixel.png")
        with open(self.file_path, "wb") as handle:
            handle.write(b"\x89PNG\r\n\x1a\n")

    def test_get_returns_bounded_file_with_display_headers(self):
        response = self.client.get("/api/file", params={"path": self.file_path})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"\x89PNG\r\n\x1a\n")
        self.assertEqual(response.headers["content-type"], "image/png")
        self.assertEqual(response.headers["content-length"], "8")
        self.assertEqual(response.headers["cache-control"], "private, no-store")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["content-security-policy"],
                         "sandbox; default-src 'none'")

    def test_head_returns_metadata_without_body(self):
        response = self.client.head("/api/file", params={"path": self.file_path})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["content-length"], "8")

    def test_missing_and_relative_paths_are_rejected(self):
        for params, message in (({}, "missing path"), ({"path": ""}, "missing path"),
                                ({"path": "pixel.png"}, "absolute path required"),
                                ({"path": f"{self.file_path}\0x"},
                                 "absolute path required")):
            response = self.client.get("/api/file", params=params)
            self.assertEqual(response.status_code, 400, params)
            self.assertEqual(response.text, message, params)

    def test_missing_file_is_not_found(self):
        response = self.client.get("/api/file", params={
            "path": os.path.join(self._tmp.name, "missing.txt")})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.text, "FS_NOT_FOUND")

    def test_directory_is_not_a_regular_file(self):
        response = self.client.get("/api/file", params={"path": self._tmp.name})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.text, "FS_NOT_REGULAR_FILE")

    def test_file_over_limit_is_rejected_for_get_and_head(self):
        path = os.path.join(self._tmp.name, "large.txt")
        with open(path, "wb") as handle:
            handle.write(b"123456789")
        for method in ("GET", "HEAD"):
            response = self.client.request(method, "/api/file", params={"path": path})
            self.assertEqual(response.status_code, 413, method)
            if method == "GET":
                self.assertEqual(response.text, "FS_TOO_LARGE")

    def test_route_requires_token_when_configured(self):
        client = TestClient(create_app(self.api, self.api.gateway, token="secret"))
        self.addCleanup(client.close)
        response = client.get("/api/file", params={"path": self.file_path})
        self.assertEqual(response.status_code, 401)
        allowed = client.get("/api/file", params={"path": self.file_path},
                             headers={"Authorization": "Bearer secret"})
        self.assertEqual(allowed.status_code, 200)
