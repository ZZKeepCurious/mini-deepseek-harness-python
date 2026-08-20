"""web 静态服务测试：serve_static 契约（对齐 packages/host/frontend-static/src/index.ts）。

纯函数测试，stdlib-only：遍历 403、根/SPA 回退 200、MIME 按扩展、未知扩展
octet-stream。HTTP 载体（405 / 静态 GET）在 test_web_server.py 的 TestStaticHttp。
"""
import os
import unittest

from miniharness.web.frontend import DIST_INDEX, DIST_ROOT, serve_static

ROOT = DIST_ROOT


def _index():
    with open(os.path.join(ROOT, DIST_INDEX), "rb") as handle:
        return handle.read()


class ServeStaticTest(unittest.TestCase):
    def test_root_serves_index(self):
        status, headers, body = serve_static("/", ROOT, DIST_INDEX)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(body, _index())

    def test_existing_file_with_mime(self):
        status, headers, _ = serve_static("/app.js", ROOT, DIST_INDEX)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/javascript; charset=utf-8")
        status, headers, _ = serve_static("/style.css", ROOT, DIST_INDEX)
        self.assertEqual(headers["content-type"], "text/css; charset=utf-8")

    def test_spa_fallback_returns_index(self):
        # 未命中（客户端路由）→ 200 + index 内容
        status, headers, body = serve_static("/some/client/route", ROOT, DIST_INDEX)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertEqual(body, _index())

    def test_traversal_forbidden(self):
        status, _, _ = serve_static("/../secret", ROOT, DIST_INDEX)
        self.assertEqual(status, 403)
        status, _, _ = serve_static("/a/../../etc/passwd", ROOT, DIST_INDEX)
        self.assertEqual(status, 403)

    def test_unknown_extension_octet_stream(self):
        import tempfile

        with tempfile.TemporaryDirectory() as dist:
            with open(os.path.join(dist, "logo.data"), "wb") as handle:
                handle.write(b"x")
            status, headers, _ = serve_static("/logo.data", dist, DIST_INDEX)
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "application/octet-stream")

    def test_nonexistent_dist_root_returns_none(self):
        self.assertIsNone(serve_static("/", os.path.join(ROOT, "missing"), DIST_INDEX))


if __name__ == "__main__":
    unittest.main()