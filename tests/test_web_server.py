"""web 传输层测试：FastAPI 载体契约（对齐 packages/host/apiproxy/src/fetch/handler.ts）。

可选前提：fastapi + uvicorn 已安装（`[web]` extra，缺任一即 skip，不进默认门禁）。
本地运行：python -m pip install "fastapi>=0.110" "uvicorn>=0.29"

SSE 增量读依赖真实 HTTP 传输（TestClient/ASGITransport 会缓冲响应体），故每
测试起一个 127.0.0.1 随机端口上的 uvicorn 后台线程，用 httpx 同步客户端驱动。
"""
from __future__ import annotations

import importlib.util
import json
import os
import threading
import unittest
from unittest.mock import patch as mock_patch

import httpx

HAS_WEB = (importlib.util.find_spec("fastapi") is not None
           and importlib.util.find_spec("uvicorn") is not None)


class _UvicornThread(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        import uvicorn

        self._server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0,
                                                     log_level="error"))

    def run(self):
        self._server.run()

    def wait_started(self):
        while not self._server.started:
            if not self.is_alive():
                raise RuntimeError("uvicorn server exited before start")
            threading.Event().wait(0.01)

    @property
    def port(self):
        socket = self._server.servers[0].sockets[0]
        return socket.getsockname()[1]

    def stop(self):
        self._server.should_exit = True
        self.join(timeout=5)


@unittest.skipUnless(HAS_WEB, "需要 fastapi/uvicorn（pip install fastapi uvicorn，[web] extra）")
class WebServerTest(unittest.TestCase):
    def setUp(self):
        from miniharness.core.scope import Context
        from miniharness.llm.fake import FakeLlmAdapter
        from miniharness.web.api import WebApi
        from miniharness.web.server import create_app
        from miniharness.web.streams import StreamHub

        self.ctx = Context(name="test")
        adapter = FakeLlmAdapter()
        adapter.model = "fake-model"
        self.api = WebApi(self.ctx, adapter)
        self.hub = StreamHub(self.ctx, self.api)
        self._server = _UvicornThread(create_app(self.api, self.hub))
        self._server.start()
        self._server.wait_started()
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self._server.port}",
                                   timeout=15)

    def tearDown(self):
        self.client.close()
        self._server.stop()
        self.hub.dispose()
        self.ctx.dispose()

    def _rpc(self, method: str, payload: dict, rpc_id: str = "r-1") -> dict:
        return {"type": "client-request", "rpcId": rpc_id, "method": method, "payload": payload}

    def _post(self, method: str, rpc_id: str = "r-1", payload: dict | None = None):
        return self.client.post(f"/api/{method}", json=self._rpc(method, payload or {}, rpc_id))

    def _create(self) -> str:
        response = self._post("session.create", "r-c", {"cwd": os.getcwd(), "sessionId": "session-t"})
        self.assertEqual(response.status_code, 200)
        return response.json()["result"]["value"]["sessionId"]


class TestUnaryCarrier(WebServerTest):
    def test_describe_roundtrip(self):
        response = self._post("host.describe")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], "server-response")
        self.assertEqual(body["rpcId"], "r-1")
        self.assertTrue(body["result"]["ok"])
        self.assertIn("version", body["result"]["value"])
        self.assertIn("cwd", body["result"]["value"])

    def test_unknown_method_404(self):
        response = self._post("session.nope")
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.text, "not found")

    def test_non_post_404(self):
        response = self.client.get("/api/host.describe")
        self.assertEqual(response.status_code, 404)
        response = self.client.put("/api/host.describe")
        self.assertEqual(response.status_code, 404)

    def test_non_api_path_non_get_405(self):
        # /api2/ 不在 /api/ 下 → 静态服务 fallback（frontend-static）：非 GET/HEAD → 405
        response = self.client.post("/api2/host.describe")
        self.assertEqual(response.status_code, 405)

    def test_non_json_media_type_415(self):
        response = self.client.post("/api/host.describe", content="{}",
                                    headers={"content-type": "text/plain"})
        self.assertEqual(response.status_code, 415)

    def test_bad_json_400(self):
        response = self.client.post("/api/host.describe", content="not json",
                                    headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.text, "body is not JSON")

    def test_invalid_envelope_uses_sentinel_rpc_id(self):
        response = self.client.post("/api/host.describe",
                                    json={"type": "server-response"})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["type"], "server-response")
        self.assertEqual(body["rpcId"], "invalid-request")
        self.assertFalse(body["result"]["ok"])
        self.assertEqual(body["result"]["error"]["code"], "bad-request")

    def test_invalid_envelope_salvages_rpc_id(self):
        response = self.client.post("/api/host.describe",
                                    json={"rpcId": "keep-me", "payload": {}})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["rpcId"], "keep-me")
        self.assertFalse(body["result"]["ok"])

    def test_method_mismatch_bad_request(self):
        response = self.client.post("/api/session.list", json=self._rpc("host.describe", {}, "r-2"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["rpcId"], "r-2")
        self.assertFalse(body["result"]["ok"])
        self.assertEqual(body["result"]["error"]["code"], "bad-request")

    def test_business_error_is_200(self):
        response = self.client.post("/api/session.prompt",
                                    json=self._rpc("session.prompt", {
                                        "sessionId": "ghost", "mode": "queue",
                                        "content": [{"type": "text", "text": "hi"}],
                                    }, "r-3"))
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertFalse(body["result"]["ok"])
        self.assertEqual(body["result"]["error"]["code"], "session-not-found")


class TestSse(WebServerTest):
    def _iter_lines(self, url):
        return self.client.stream("GET", url)

    def _read_frame(self, it):
        while True:
            line = next(it)
            if line.startswith("data: "):
                return json.loads(line[6:])

    def test_mux_sse(self):
        sid = self._create()
        with self.client.stream("GET", "/api/events.mux") as response:
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["content-type"].split(";")[0], "text/event-stream")
            it = response.iter_lines()
            self.assertEqual(next(it), ": connected")
            frames = [self._read_frame(it)]
            self.assertEqual(frames[0]["method"], "session/subscribed")
            self.assertEqual(frames[0]["payload"]["sessionId"], sid)
            # 开播后 prompt：实时帧应到达
            response = self._post("session.prompt", "r-p", {"sessionId": sid, "mode": "queue",
                                                            "content": [{"type": "text", "text": "hi"}]})
            self.assertEqual(response.status_code, 200)
            while True:
                frame = self._read_frame(it)
                frames.append(frame)
                if (frame["method"] == "session/event"
                        and frame["payload"]["event"]["type"] == "turn/end"):
                    break
        # 每帧独立 rpcId（对齐上游 frame() 每帧 randomUUID）；rpcId 只活在外层
        # envelope 上，payload 不含它；method = 帧 type
        rpc_ids = {frame["rpcId"] for frame in frames}
        self.assertGreater(len(rpc_ids), 1)
        for frame in frames:
            self.assertEqual(frame["type"], "server-request")
            self.assertEqual(frame["method"], frame["payload"]["type"])
            self.assertNotIn("rpcId", frame["payload"])
        types = [frame["method"] for frame in frames]
        self.assertIn("session/event", types)
        self.assertTrue(any(frame["method"] == "session/event"
                            and frame["payload"]["event"]["type"] == "assistant/message"
                            for frame in frames))

    def test_host_sse(self):
        with self.client.stream("GET", "/api/events.host") as response:
            self.assertEqual(response.status_code, 200)
            it = response.iter_lines()
            self.assertEqual(next(it), ": connected")
            sid = self._create()
            frame = self._read_frame(it)
            self.assertEqual(frame["method"], "host/session-added")
            self.assertEqual(frame["payload"]["sessionId"], sid)

    def test_sse_post_method_404(self):
        response = self._post("events.mux")
        self.assertEqual(response.status_code, 404)


class TestRespondCarrier(WebServerTest):
    """POST /api/respond 的载体契约（approval 的 client-response 入口）。"""

    def _respond(self, body: dict):
        return self.client.post("/api/respond", json=body)

    def test_respond_not_pending(self):
        response = self._respond({"type": "client-response", "rpcId": "ghost",
                                  "result": {"ok": True, "value": {
                                      "sessionId": "s", "approvalId": "a",
                                      "outcome": "allowed-once"}}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": False, "reason": "not-pending"})

    def test_respond_invalid_envelope_bad_response(self):
        # 信封不合法 / 非 client-response → 200 bad-response 回执（handler.ts 同款）
        response = self._respond({"type": "client-request", "method": "x", "payload": {}})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": False, "reason": "bad-response"})
        response = self._respond({"rpcId": "r"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"accepted": False, "reason": "bad-response"})

    def test_respond_non_json_415(self):
        response = self.client.post("/api/respond", content="{}",
                                    headers={"content-type": "text/plain"})
        self.assertEqual(response.status_code, 415)

    def test_respond_bad_json_400(self):
        response = self.client.post("/api/respond", content="not json",
                                    headers={"content-type": "application/json"})
        self.assertEqual(response.status_code, 400)

    def test_respond_subpath_404(self):
        response = self._respond({"type": "client-response", "rpcId": "x",
                                  "result": {"ok": True, "value": {}}})
        self.assertEqual(response.status_code, 200)
        response = self.client.post("/api/respond/extra", json={})
        self.assertEqual(response.status_code, 404)


class TestStaticHttp(WebServerTest):
    """非 /api/ 路径的静态服务载体（frontend-static 契约）。"""

    def test_get_root_serves_spa(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/html")
        self.assertIn("MiniHarness", response.text)

    def test_get_asset_mime(self):
        response = self.client.get("/app.js")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/javascript")

    def test_get_spa_route_fallback(self):
        response = self.client.get("/some/client/route")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"].split(";")[0], "text/html")

    def test_non_get_methods_405(self):
        for method in ("POST", "PUT", "DELETE"):
            response = self.client.request(method, "/not-an-api-path")
            self.assertEqual(response.status_code, 405)


@unittest.skipUnless(HAS_WEB, "需要 fastapi/uvicorn（pip install fastapi uvicorn，[web] extra）")
class TestApprovalHttp(unittest.TestCase):
    """端到端：工具 ask → mux approval/requested → POST /api/respond → 结算帧。"""

    def setUp(self):
        from miniharness.core.scope import Context
        from miniharness.core.tools import Tool, ToolRegistry
        from miniharness.llm.fake import FakeLlmAdapter
        from miniharness.web.api import WebApi
        from miniharness.web.server import create_app
        from miniharness.web.streams import StreamHub

        self.ctx = Context(name="approval-test")
        self.tools = ToolRegistry(self.ctx)
        self.tools.register(Tool(
            name="echo", description="echo text",
            execute=lambda args, exec_: {"echo": args.get("text", "")},
            parameters={"type": "object", "properties": {"text": {"type": "string"}}},
        ))
        adapter = FakeLlmAdapter(tool_call={"name": "echo", "arguments": {"text": "hi"}})
        adapter.model = "fake-model"
        self.api = WebApi(self.ctx, adapter, self.tools)
        self.hub = StreamHub(self.ctx, self.api)
        self._server = _UvicornThread(create_app(self.api, self.hub))
        self._server.start()
        self._server.wait_started()
        self.client = httpx.Client(base_url=f"http://127.0.0.1:{self._server.port}", timeout=15)

    def tearDown(self):
        self.client.close()
        self._server.stop()
        self.hub.dispose()
        self.ctx.dispose()

    def _create(self):
        response = self.api.dispatch("session.create", "r-c", {"cwd": os.getcwd()})
        self.assertTrue(response["result"]["ok"])
        return response["result"]["value"]["sessionId"]

    def test_respond_roundtrip(self):
        sid = self._create()
        loop = self.api._agents[sid]
        loop.ctx.on("tools/pre-execute", lambda payload, nxt: {"kind": "ask"})
        with self.client.stream("GET", "/api/events.mux") as response:
            self.assertEqual(response.status_code, 200)
            it = response.iter_lines()
            self.assertEqual(next(it), ": connected")

            prompt = self.client.post("/api/session.prompt", json={
                "type": "client-request", "rpcId": "r-p", "method": "session.prompt",
                "payload": {"sessionId": sid, "mode": "queue",
                            "content": [{"type": "text", "text": "hi"}]},
            })
            self.assertEqual(prompt.status_code, 200)

            requested = None
            for _ in range(200):
                line = next(it)
                if not line.startswith("data: "):
                    continue
                frame = json.loads(line[6:])
                if frame["method"] == "approval/requested":
                    requested = frame
                    break
            self.assertIsNotNone(requested)
            payload = requested["payload"]
            self.assertEqual(payload["toolName"], "echo")

            receipt = self.client.post("/api/respond", json={
                "type": "client-response",
                "rpcId": requested["rpcId"],
                "result": {"ok": True, "value": {
                    "sessionId": sid, "approvalId": payload["approvalId"],
                    "outcome": "allowed-once",
                }},
            })
            self.assertEqual(receipt.status_code, 200)
            self.assertEqual(receipt.json(), {"accepted": True})

            resolved = False
            for _ in range(200):
                line = next(it)
                if not line.startswith("data: "):
                    continue
                frame = json.loads(line[6:])
                if frame["method"] == "approval/resolved":
                    self.assertEqual(frame["payload"]["outcome"], "allowed-once")
                    self.assertEqual(frame["rpcId"], requested["rpcId"])
                    resolved = True
                    break
            self.assertTrue(resolved)


class TestLauncher(unittest.TestCase):
    """web/launcher：监听契约（host 两值 + port 0）与纯装配 build_app。"""

    def test_resolve_bind_defaults(self):
        from miniharness.web.launcher import _resolve_bind

        with mock_patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MINIHARNESS_WEB_HOST", None)
            os.environ.pop("MINIHARNESS_WEB_PORT", None)
            self.assertEqual(_resolve_bind(None, None), ("127.0.0.1", 0))

    def test_resolve_bind_env_and_override(self):
        from miniharness.web.launcher import _resolve_bind

        with mock_patch.dict(os.environ, {"MINIHARNESS_WEB_HOST": "0.0.0.0",
                                          "MINIHARNESS_WEB_PORT": "8000"}, clear=False):
            self.assertEqual(_resolve_bind(None, None), ("0.0.0.0", 8000))
            # 显式参数优先于 env
            self.assertEqual(_resolve_bind("127.0.0.1", 9000), ("127.0.0.1", 9000))

    def test_resolve_bind_invalid_host(self):
        from miniharness.web.launcher import _resolve_bind

        with self.assertRaises(ValueError):
            _resolve_bind("example.com", None)

    def test_resolve_bind_invalid_port(self):
        from miniharness.web.launcher import _resolve_bind

        with self.assertRaises(ValueError):
            _resolve_bind(None, 70000)

    @unittest.skipUnless(HAS_WEB, "需要 fastapi/uvicorn")
    def test_build_app_serves_api(self):
        from miniharness.core.scope import Context
        from miniharness.llm.fake import FakeLlmAdapter
        from miniharness.web.launcher import build_app

        ctx = Context(name="launcher-test")
        adapter = FakeLlmAdapter()
        adapter.model = "fake-model"
        app = build_app(adapter, None, ctx)
        self.assertIsNotNone(app)
        server = _UvicornThread(app)
        server.start()
        server.wait_started()
        client = httpx.Client(base_url=f"http://127.0.0.1:{server.port}", timeout=15)
        try:
            response = client.post("/api/host.describe", json={
                "type": "client-request", "rpcId": "r-x", "method": "host.describe",
                "payload": {},
            })
            self.assertEqual(response.status_code, 200)
            body = response.json()
            self.assertTrue(body["result"]["ok"])
            self.assertEqual(body["rpcId"], "r-x")
        finally:
            client.close()
            server.stop()
            ctx.dispose()


if __name__ == "__main__":
    unittest.main()