"""第 12 章测试：SDK 线协议 —— JSON-RPC 信封 + 最小运行服务。"""
import json
import unittest

from miniharness.protocol.sdk import (
    JsonRpcLineTransport,
    JsonRpcResponseError,
    SdkRuntime,
)


class TestTransportFraming(unittest.TestCase):
    def setUp(self):
        self.t = JsonRpcLineTransport()

    def test_request_frame_shapes(self):
        pending = self.t.request("session/prompt", {"sessionId": "s1"})
        frame = json.loads(self.t.out_lines[0])
        self.assertEqual(frame["jsonrpc"], "2.0")
        self.assertTrue(frame["id"].startswith("req_"))
        self.assertEqual(frame["method"], "session/prompt")
        self.assertEqual(frame["params"], {"sessionId": "s1"})
        self.assertEqual(pending.id, frame["id"])

    def test_notify_without_params_omits_member(self):
        self.t.notify("session.event")
        frame = json.loads(self.t.out_lines[0])
        self.assertEqual(frame, {"jsonrpc": "2.0", "method": "session.event"})
        self.assertNotIn("params", frame)
        self.assertNotIn("id", frame)

    def test_response_settles_pending_result(self):
        pending = self.t.request("initialize", {})
        self.t.feed(json.dumps({"jsonrpc": "2.0", "id": pending.id,
                                "result": {"serverInfo": {"name": "x"}}}))
        self.assertTrue(pending.settled)
        self.assertEqual(pending.result["serverInfo"]["name"], "x")
        self.assertIsNone(pending.error)

    def test_error_response_rejects_with_code(self):
        pending = self.t.request("session/prompt", {"sessionId": "s1"})
        self.t.feed(json.dumps({"jsonrpc": "2.0", "id": pending.id,
                                "error": {"code": -32603, "message": "boom",
                                          "data": {"k": 1}}}))
        self.assertIsNone(pending.result)
        self.assertIsInstance(pending.error, JsonRpcResponseError)
        self.assertEqual(pending.error.code, -32603)
        self.assertEqual(pending.error.data, {"k": 1})

    def test_unknown_response_id_ignored(self):
        pending = self.t.request("initialize", {})
        self.t.feed(json.dumps({"jsonrpc": "2.0", "id": "req_unknown",
                                "result": {"x": 1}}))
        self.assertFalse(pending.settled)

    def test_malformed_line_ignored(self):
        pending = self.t.request("initialize", {})
        self.t.feed("not json {")
        self.t.feed("42")
        self.assertFalse(pending.settled)
        self.assertEqual(len(self.t.out_lines), 1)

    def test_request_without_handler_answers_32601(self):
        self.t.feed(json.dumps({"jsonrpc": "2.0", "id": 7, "method": "nope",
                                "params": {}}))
        frame = json.loads(self.t.out_lines[0])
        self.assertEqual(frame["id"], 7)
        self.assertEqual(frame["error"]["code"], -32601)

    def test_handler_throw_answers_32603(self):
        def handler(method, params):
            raise ValueError("boom")
        self.t.on_request(handler)
        self.t.feed(json.dumps({"jsonrpc": "2.0", "id": "a", "method": "m"}))
        frame = json.loads(self.t.out_lines[0])
        self.assertEqual(frame["error"]["code"], -32603)
        self.assertEqual(frame["error"]["message"], "boom")

    def test_handler_result_roundtrip(self):
        def handler(method, params):
            return {"echo": params}
        self.t.on_request(handler)
        self.t.feed(json.dumps({"jsonrpc": "2.0", "id": "a", "method": "m",
                                "params": {"p": 1}}))
        frame = json.loads(self.t.out_lines[0])
        self.assertEqual(frame["result"], {"echo": {"p": 1}})

    def test_params_normalization_array_and_scalar_collapse(self):
        seen = []

        def handler(method, params):
            seen.append(params)
            return {}
        self.t.on_request(handler)
        self.t.feed(json.dumps({"jsonrpc": "2.0", "id": "a", "method": "m",
                                "params": [1, 2]}))
        self.t.feed(json.dumps({"jsonrpc": "2.0", "id": "b", "method": "m",
                                "params": "scalar"}))
        self.assertEqual(seen, [{}, {}])

    def test_notification_without_handler_dropped(self):
        self.t.feed(json.dumps({"jsonrpc": "2.0", "method": "session.event",
                                "params": {}}))
        self.assertEqual(self.t.out_lines, [])

    def test_notification_handler_invoked(self):
        seen = []
        self.t.on_notification(lambda method, params: seen.append((method, params)))
        self.t.feed(json.dumps({"jsonrpc": "2.0", "method": "session.status",
                                "params": {"status": "running"}}))
        self.assertEqual(seen, [("session.status", {"status": "running"})])

    def test_close_rejects_pending_and_blocks_writes(self):
        pending = self.t.request("initialize", {})
        self.t.close()
        self.assertTrue(pending.settled)
        self.assertIsInstance(pending.error, JsonRpcResponseError)
        with self.assertRaises(RuntimeError):
            self.t.notify("session.event")

    def test_flush_emits_empty_barrier(self):
        self.t.flush()
        self.assertEqual(self.t.out_lines[-1], "")


class TestSdkRuntime(unittest.TestCase):
    def test_initialize_returns_wire_stable_server_info(self):
        rt = SdkRuntime()
        result = rt.handle("initialize", {"cwd": "/work", "provider": "fake",
                                          "model": "m"})
        self.assertEqual(result["serverInfo"]["name"],
                         "deepseek-harness-sdk-runtime")
        self.assertEqual(result["serverInfo"]["version"], "0.0.1")

    def test_session_prompt_lazily_creates_and_returns_message_id(self):
        rt = SdkRuntime()
        result = rt.handle("session/prompt",
                           {"sessionId": "sdk-1",
                            "contentBlocks": [{"type": "text", "text": "你好"}]})
        self.assertEqual(result["messageId"], "msg-1")
        self.assertIn("sdk-1", rt.sessions)
        events = rt.sessions["sdk-1"].session.events
        self.assertEqual(events[0]["type"], "turn/start")
        # 回合真的跑完：turn/end 已落日志
        self.assertEqual(events[-1]["type"], "turn/end")

    def test_session_prompt_unknown_session_lazy_creates_again(self):
        rt = SdkRuntime()
        rt.handle("session/prompt", {"sessionId": "a", "contentBlocks": []})
        rt.handle("session/prompt", {"sessionId": "b", "contentBlocks": []})
        self.assertEqual(set(rt.sessions), {"a", "b"})

    def test_shutdown_returns_empty(self):
        self.assertEqual(SdkRuntime().handle("shutdown", {}), {})

    def test_unknown_method_fails_loud(self):
        with self.assertRaises(ValueError):
            SdkRuntime().handle("session/close", {})

    def test_prompt_missing_session_id_fails(self):
        with self.assertRaises(ValueError):
            SdkRuntime().handle("session/prompt", {"contentBlocks": []})

    def test_end_to_end_stdio_line(self):
        """完整客户端仿真：写请求行 → feed → 读响应行。"""
        rt = SdkRuntime()
        client = JsonRpcLineTransport()
        server = JsonRpcLineTransport()
        server.on_request(rt.handle)

        pending = client.request("initialize", {"cwd": ".", "provider": "fake",
                                                "model": "m"})
        server.feed(client.out_lines[-1])            # 请求帧给服务端
        client.feed(server.out_lines[-1])            # 响应帧给客户端
        self.assertEqual(pending.result["serverInfo"]["name"],
                         "deepseek-harness-sdk-runtime")


if __name__ == "__main__":
    unittest.main()