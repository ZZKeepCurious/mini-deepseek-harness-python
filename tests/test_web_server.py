"""web 传输层测试：unary `{args}` RPC + `$events/result` + `/api/remote.mux` WebSocket。

对齐 `packages/api/gateway` + `packages/client/connection`：payload 恰 `{args}`、
业务错误恒 200 + result.ok=false、载体 404/415/400、WS mux open/cancel 帧往返。
用 fastapi.testclient.TestClient（web extra 依赖）走真实 ASGI 路由。
"""
import asyncio
import json
import os
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import WebApi
from miniharness.web.server import create_app

try:
    from fastapi.testclient import TestClient
    _HAVE_TC = True
except Exception:  # noqa: BLE001
    _HAVE_TC = False


def _fake():
    adapter = FakeLlmAdapter()
    adapter.model = "fake-model"
    return adapter


@unittest.skipUnless(_HAVE_TC, "fastapi TestClient unavailable")
class WebServerTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="web-server-test")
        self.api = WebApi(self.ctx, _fake())
        self.client = TestClient(create_app(self.api, self.api.gateway))

    def tearDown(self):
        self.client.close()
        self.ctx.dispose()

    def _post(self, endpoint, payload, rpc_id="r1"):
        body = {"type": "client-request", "rpcId": rpc_id, "method": endpoint,
                "payload": payload}
        return self.client.post(f"/api/{endpoint}", json=body)

    def _value(self, response):
        data = response.json()
        self.assertTrue(data["result"]["ok"], data["result"].get("error"))
        return data["result"].get("value")

    def test_unary_roundtrip_with_args_unwrap(self):
        resp = self._post("session.list", {"args": {}})
        self.assertEqual(resp.status_code, 200)
        value = self._value(resp)
        self.assertIn("items", value)

    def test_payload_must_be_exact_args_field(self):
        # 非恰 `{args:{...}}` 单字段 → bad-request（`{1:2}` 经 JSON 往来键变字符串，
        # 线上是合法 plain object，故不作非法样例）
        for payload in ({}, {"args": {}, "x": 1}, {"args": None}, {"args": []},
                        {"key": "val"}, {"args": ""}):
            resp = self._post("session.list", payload)
            data = resp.json()
            self.assertFalse(data["result"]["ok"], payload)
            self.assertEqual(data["result"]["error"]["code"], "gateway/bad-request", payload)

    def test_unknown_method_not_found(self):
        resp = self._post("session.nope", {"args": {}})
        self.assertEqual(resp.status_code, 404)

    def test_envelope_invalid_bad_request(self):
        resp = self.client.post("/api/session.list",
                                json={"type": "server-response"})
        data = resp.json()
        self.assertFalse(data["result"]["ok"])
        self.assertEqual(data["result"]["error"]["code"], "gateway/bad-request")

    def test_method_path_mismatch_bad_request(self):
        resp = self._post("session.list", {"args": {}}, rpc_id="r9")
        # path session.list 但 body method=session.list —— 一致；改 body 使不一致
        body = {"type": "client-request", "rpcId": "r9", "method": "session.create",
                "payload": {"args": {}}}
        resp = self.client.post("/api/session.list", json=body)
        data = resp.json()
        self.assertFalse(data["result"]["ok"])
        self.assertEqual(data["result"]["error"]["code"], "gateway/bad-request")

    def test_create_session_prompt_flow(self):
        resp = self._post("session.create", {"args": {"cwd": os.getcwd()}})
        session_id = self._value(resp)["sessionId"]
        # create 后创建 gateway 冷会话 loop 需驱动；直接验证 list 含它
        resp = self._post("session.list", {"args": {}})
        ids = [item["sessionId"] for item in self._value(resp)["items"]]
        self.assertIn(session_id, ids)


@unittest.skipUnless(_HAVE_TC, "fastapi TestClient unavailable")
class WebSocketMuxTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="web-ws-test")
        self.api = WebApi(self.ctx, _fake())
        self.client = TestClient(create_app(self.api, self.api.gateway))

    def tearDown(self):
        self.client.close()
        self.ctx.dispose()

    def test_events_open_ready_and_emit(self):
        with self.client.websocket_connect("/api/remote.mux") as ws:
            ws.send_json({"type": "open", "streamId": "e1", "endpoint": "$events",
                          "payload": {"args": {}}})
            ready = ws.receive_json()
            self.assertEqual(ready["type"], "item")
            self.assertEqual(ready["value"]["type"], "ready")
            self.assertIn("clientId", ready["value"])
            self.assertIn("host", ready["value"])
            # 主线程经 ctx.emit 广播 → 线程安全唤醒 portal 泵 → 下游 emit 帧
            self.api.dispatch("session.create", "rid", {"cwd": os.getcwd()})
            item = ws.receive_json()
            self.assertEqual(item["type"], "item")
            self.assertEqual(item["value"]["type"], "emit")
            self.assertEqual(item["value"]["event"], "api-session/added")

    def test_events_result_unknown_client_bad_request(self):
        # 未知 clientId → 载体侧拒绝（internal，折成 200 + result.ok=false）
        resp = self.client.post("/api/$events/result", json={
            "type": "client-request", "rpcId": "rr", "method": "$events/result",
            "payload": {"args": {"clientId": "nope", "eventId": "nope",
                                 "outcome": {"kind": "cancelled"}}}})
        data = resp.json()
        self.assertFalse(data["result"]["ok"])
        self.assertEqual(data["result"]["error"]["code"], "gateway/internal")


@unittest.skipUnless(_HAVE_TC, "fastapi TestClient unavailable")
class FileUploadHttpTest(unittest.TestCase):
    """原始字节上传路由（`packages/client/file-upload/src/http-route.ts`）。"""

    def setUp(self):
        from miniharness.attachment.store import LocalAttachmentStore
        self.ctx = Context(name="web-upload-test")
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx.provide("attachments", LocalAttachmentStore(
            root=self._tmp.name, limits=None))
        self.api = WebApi(self.ctx, _fake())
        self.client = TestClient(create_app(self.api, self.api.gateway))
        created = self.api.dispatch("session.create", "rc", {"cwd": os.getcwd()})
        self.session_id = created["result"]["value"]["sessionId"]

    def tearDown(self):
        self.client.close()
        self.ctx.dispose()

    def _upload(self, body=b"hello file\n", *, session_id=None, name="note.txt",
                content_type="application/octet-stream"):
        query = f"?sessionId={session_id if session_id is not None else self.session_id}"
        if name is not None:
            query += f"&name={name}"
        return self.client.post("/api/session/uploadFileBinary" + query,
                                content=body,
                                headers={"content-type": content_type})

    def test_streamed_upload_returns_receipt(self):
        response = self._upload()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["content-type"],
                         "application/json; charset=utf-8")
        self.assertEqual(response.headers["cache-control"], "no-store")
        result = response.json()
        self.assertTrue(result["ok"], result.get("error"))
        value = result["value"]
        self.assertEqual(value["file"]["name"], "note.txt")
        self.assertEqual(value["file"]["bytes"], 11)
        self.assertTrue(value["receiptId"])
        ref = self.api.file_uploads.resolve(self.session_id, value["receiptId"])
        self.assertIsNotNone(ref)
        self.assertEqual(b"".join(self.api.attachments.read_file_stream(ref)),
                         b"hello file\n")

    def test_get_method_not_allowed(self):
        response = self.client.get("/api/session/uploadFileBinary?sessionId=x")
        self.assertEqual(response.status_code, 405)
        self.assertEqual(response.headers["allow"], "POST")

    def test_requires_octet_stream(self):
        response = self._upload(content_type="application/json")
        self.assertEqual(response.status_code, 415)
        self.assertEqual(response.text, "content type must be application/octet-stream")

    def test_requires_session_id(self):
        response = self.client.post(
            "/api/session/uploadFileBinary", content=b"x",
            headers={"content-type": "application/octet-stream"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.text, "sessionId is required")

    def test_unknown_session_business_error(self):
        response = self._upload(session_id="session-nope")
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["code"], "session/not-found")


if __name__ == "__main__":
    unittest.main()
