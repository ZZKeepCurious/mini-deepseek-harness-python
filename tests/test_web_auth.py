"""web 认证门测试（`web/auth.py` TokenGateMiddleware + `server.create_app(token=)`）。

对齐上游 alpha.1 形态：`connection.requestRejection(req)` 可插拔拒绝面 ——
WS 升级拒绝 = HTTP 401 响应后断开（`rejectRemoteStreamUpgrade`，mini 经
ASGI `websocket.http.response.*` 同形）；token 缺省 = 无门（回环开发形态）。
"""
import asyncio
import json
import os
import unittest
from unittest import mock

from miniharness.core.scope import Context
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import WebApi
from miniharness.web.auth import (
    WEB_TOKEN_ENV,
    TokenGateMiddleware,
    extract_bearer,
    provided_token,
    query_token,
    resolve_web_token,
    token_matches,
)
from miniharness.web.server import create_app

try:
    from fastapi.testclient import TestClient
    _HAVE_TC = True
except Exception:  # noqa: BLE001
    _HAVE_TC = False


class ResolveWebTokenTest(unittest.TestCase):
    def test_precedence_explicit_over_env(self):
        with mock.patch.dict(os.environ, {WEB_TOKEN_ENV: "env-t"}):
            self.assertEqual(resolve_web_token(), "env-t")
            self.assertEqual(resolve_web_token("explicit"), "explicit")
        with mock.patch.dict(os.environ, {WEB_TOKEN_ENV: ""}):
            self.assertIsNone(resolve_web_token())

    def test_unset_env_is_none(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(WEB_TOKEN_ENV, None)
            self.assertIsNone(resolve_web_token())


class TokenExtractionTest(unittest.TestCase):
    def test_extract_bearer(self):
        self.assertEqual(extract_bearer("Bearer abc"), "abc")
        self.assertEqual(extract_bearer("bearer abc"), "abc")
        self.assertIsNone(extract_bearer("Basic abc"))
        self.assertIsNone(extract_bearer("Bearer "))
        self.assertIsNone(extract_bearer(None))

    def test_query_token(self):
        self.assertEqual(query_token(b"token=abc&x=1"), "abc")
        self.assertIsNone(query_token("x=1"))
        self.assertIsNone(query_token(b""))

    def test_provided_token_prefers_query_then_header(self):
        scope = {"query_string": b"token=via-query",
                 "headers": [(b"authorization", b"Bearer via-header")]}
        self.assertEqual(provided_token(scope), "via-query")
        scope = {"query_string": b"",
                 "headers": [(b"authorization", b"Bearer via-header")]}
        self.assertEqual(provided_token(scope), "via-header")
        self.assertIsNone(provided_token({"query_string": b"", "headers": []}))

    def test_token_matches(self):
        self.assertTrue(token_matches("abc", "abc"))
        self.assertFalse(token_matches("ab", "abc"))
        self.assertFalse(token_matches(None, "abc"))


class _SinkApp:
    """记录调用/输出（send 消息）的下游 ASGI 应用。"""

    def __init__(self):
        self.calls: list = []
        self.sent: list = []

    async def __call__(self, scope, receive, send):
        self.calls.append(scope["type"])

        async def send_wrapper(message):
            self.sent.append(message)

        await _noop_receive_and_send(scope, receive, send_wrapper)


async def _noop_receive_and_send(scope, receive, send):  # pragma: no cover
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


class MiddlewareASGITest(unittest.TestCase):
    def setUp(self):
        self.sink = _SinkApp()
        self.gate = TokenGateMiddleware(self.sink, "secret")

    def _run(self, scope):
        async def receive():  # pragma: no cover - gate 不读 body
            raise AssertionError("gate must not read the body")

        async def send(message):
            self.sink.sent.append(message)

        asyncio.run(self.gate(scope, receive, send))

    def test_http_rejected_with_401(self):
        self._run({"type": "http", "path": "/api/session.list",
                   "headers": [], "query_string": b""})
        self.assertEqual(self.sink.calls, [])
        start = self.sink.sent[0]
        self.assertEqual(start["status"], 401)
        body = self.sink.sent[1]["body"]
        self.assertEqual(json.loads(body), {"error": "unauthorized"})

    def test_http_allowed_with_bearer_header(self):
        self._run({"type": "http", "path": "/api/session.list",
                   "headers": [(b"authorization", b"Bearer secret")],
                   "query_string": b""})
        self.assertEqual(self.sink.calls, ["http"])

    def test_http_allowed_with_query_token(self):
        self._run({"type": "http", "path": "/api/session.list",
                   "headers": [], "query_string": b"token=secret"})
        self.assertEqual(self.sink.calls, ["http"])

    def test_ws_rejected_401_then_close(self):
        self._run({"type": "websocket", "path": "/api/remote.mux",
                   "headers": [], "query_string": b""})
        self.assertEqual(self.sink.calls, [])
        types = [m["type"] for m in self.sink.sent]
        self.assertEqual(types[0], "websocket.http.response.start")
        self.assertEqual(self.sink.sent[0]["status"], 401)
        self.assertEqual(types[-1], "websocket.close")

    def test_ws_allowed_with_query_token(self):
        self._run({"type": "websocket", "path": "/api/remote.mux",
                   "headers": [], "query_string": b"token=secret"})
        self.assertEqual(self.sink.calls, ["websocket"])

    def test_non_api_path_bypasses_gate(self):
        self._run({"type": "http", "path": "/index.html",
                   "headers": [], "query_string": b""})
        self.assertEqual(self.sink.calls, ["http"])

    def test_middleware_requires_non_empty_token(self):
        for bad in ("", None):
            with self.assertRaises(ValueError):
                TokenGateMiddleware(lambda s, r, se: None, bad)  # type: ignore[arg-type]


@unittest.skipUnless(_HAVE_TC, "fastapi TestClient unavailable")
class TokenGateAppTest(unittest.TestCase):
    """create_app(token=…) 装配后的端到端载体行为。"""

    def setUp(self):
        self.ctx = Context(name="web-auth-test")
        self.api = WebApi(self.ctx, FakeLlmAdapter())
        self.client = TestClient(create_app(self.api, self.api.gateway, token="secret"))

    def tearDown(self):
        self.client.close()
        self.ctx.dispose()

    def _post(self, headers=None, params=None):
        body = {"type": "client-request", "rpcId": "r1", "method": "session.list",
                "payload": {"args": {}}}
        return self.client.post("/api/session.list", json=body,
                                headers=headers or {}, params=params or {})

    def test_unary_requires_token(self):
        self.assertEqual(self._post().status_code, 401)
        self.assertEqual(self._post(headers={"Authorization": "wrong"}).status_code, 401)
        self.assertEqual(self._post(headers={"Authorization": "Bearer nope"}).status_code, 401)

    def test_unary_accepts_bearer_and_query(self):
        resp = self._post(headers={"Authorization": "Bearer secret"})
        self.assertEqual(resp.status_code, 200)
        resp = self._post(params={"token": "secret"})
        self.assertEqual(resp.status_code, 200)

    def test_export_requires_token(self):
        # 无 token → 401（先于 query 业务校验）；带 token → 进入业务面（缺会话 404）
        resp = self.client.get("/api/session.export", params={"sessionId": "s"})
        self.assertEqual(resp.status_code, 401)
        resp = self.client.get("/api/session.export",
                               params={"sessionId": "s", "token": "secret"})
        self.assertEqual(resp.status_code, 404)

    def test_static_served_without_token(self):
        # SPA shell 不设门：非 /api 路径缺 token 也不得出现 401
        resp = self.client.get("/definitely-not-here.js")
        self.assertNotEqual(resp.status_code, 401)

    def test_ws_rejected_without_token(self):
        try:
            with self.client.websocket_connect("/api/remote.mux") as ws:
                ws.send_json({"type": "open", "streamId": "e1", "endpoint": "$events",
                              "payload": {"args": {}}})
                ws.receive_json()
        except Exception:  # noqa: BLE001 - close-before-accept = 升级被拒
            return
        self.fail("websocket upgrade must be rejected without a token")

    def test_ws_accepts_with_query_token(self):
        with self.client.websocket_connect("/api/remote.mux?token=secret") as ws:
            ws.send_json({"type": "open", "streamId": "e1", "endpoint": "$events",
                          "payload": {"args": {}}})
            ready = ws.receive_json()
            self.assertEqual(ready["type"], "item")
            self.assertEqual(ready["value"]["type"], "ready")


if __name__ == "__main__":
    unittest.main()
