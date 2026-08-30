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

    def test_webui_dist_build(self):
        # 产品化前端构建产物（如 webui/dist/，Vite 输出形态）：index + assets/hash 路径、
        # 嵌套资源子目录、SPA 回退——同一 serve_static 纯函数即可承载。
        import tempfile

        with tempfile.TemporaryDirectory() as dist:
            asroot = os.path.join(dist, "index.html")
            asassets = os.path.join(dist, "assets")
            os.makedirs(asassets)
            with open(asroot, "wb") as handle:
                handle.write(b"<html>webui</html>")
            with open(os.path.join(asassets, "index-abc123.js"), "wb") as handle:
                handle.write(b"console.log(1)")
            with open(os.path.join(asassets, "index-abc123.css"), "wb") as handle:
                handle.write(b"body{}")
            with open(os.path.join(dist, "app.webmanifest"), "wb") as handle:
                handle.write(b"{}")

            status, headers, body = serve_static("/", dist, DIST_INDEX)
            self.assertEqual(status, 200)
            self.assertEqual(body, b"<html>webui</html>")
            self.assertEqual(headers["content-type"], "text/html; charset=utf-8")

            status, headers, _ = serve_static("/assets/index-abc123.js", dist, DIST_INDEX)
            self.assertEqual(status, 200)
            self.assertEqual(headers["content-type"], "text/javascript; charset=utf-8")

            status, _, _ = serve_static("/assets/index-abc123.css", dist, DIST_INDEX)
            self.assertEqual(status, 200)
            status, headers, _ = serve_static("/app.webmanifest", dist, DIST_INDEX)
            self.assertEqual(headers["content-type"], "application/manifest+json")

            # SPA 回退：未命中的客户端路由仍还 index
            status, headers, body = serve_static("/sessions/abc", dist, DIST_INDEX)
            self.assertEqual(status, 200)
            self.assertEqual(body, b"<html>webui</html>")


if __name__ == "__main__":
    unittest.main()