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

    def test_non_api_path_404(self):
        response = self.client.post("/api2/host.describe")
        self.assertEqual(response.status_code, 404)

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
        # 流级单一 rpcId；server-request 全形；method = 帧 type
        rpc_ids = {frame["rpcId"] for frame in frames}
        self.assertEqual(len(rpc_ids), 1)
        for frame in frames:
            self.assertEqual(frame["type"], "server-request")
            self.assertEqual(frame["method"], frame["payload"]["type"])
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


if __name__ == "__main__":
    unittest.main()