"""web 表面测试：typert Remote 流 wire 语法（`stream_protocol.py`）。

对齐 `packages/api/gateway/src/stream-protocol.ts` 的逐字段校验：客户端两帧、
服务端三帧、`$events/result` 三型 outcome、无损 JSON 判定。未知键按
schemastery 投影语义丢弃（参考 test_web_envelope.TestMessages.test_extra_keys_accepted）。
"""
import json
import unittest

from miniharness.web.stream_protocol import (
    DEFAULT_WEBSOCKET_HEARTBEAT_INTERVAL_MS,
    REMOTE_EVENT_RESULT_ENDPOINT,
    REMOTE_EVENT_STREAM_ENDPOINT,
    REMOTE_STREAM_MUX_PATH,
    StreamProtocolError,
    is_remote_json_value,
    parse_remote_event_result_payload,
    parse_remote_stream_client_message,
    parse_remote_stream_server_message,
)


class TestConstants(unittest.TestCase):
    def test_paths(self):
        self.assertEqual(REMOTE_EVENT_RESULT_ENDPOINT, "$events/result")
        self.assertEqual(REMOTE_EVENT_STREAM_ENDPOINT, "$events")
        self.assertEqual(REMOTE_STREAM_MUX_PATH, "/api/remote.mux")

    def test_heartbeat_interval_matches_gateway_config(self):
        # gateway Config websocketHeartbeatIntervalMs @default 2000（index.ts）
        self.assertEqual(DEFAULT_WEBSOCKET_HEARTBEAT_INTERVAL_MS, 2_000)


class TestClientMessages(unittest.TestCase):
    def test_open_roundtrip(self):
        frame = {"type": "open", "streamId": "s1", "endpoint": "session/follow",
                 "payload": {"args": {"request": {"sessionId": "x"}}}}
        self.assertEqual(parse_remote_stream_client_message(json.dumps(frame)), frame)

    def test_open_payload_any_json(self):
        for payload in (None, 0, "plain", [], {"args": {}}, {"args": {"x": 1}}):
            frame = {"type": "open", "streamId": "s1", "endpoint": "e",
                     "payload": payload}
            self.assertEqual(
                parse_remote_stream_client_message(json.dumps(frame)), frame)

    def test_cancel_roundtrip(self):
        self.assertEqual(parse_remote_stream_client_message(
            json.dumps({"type": "cancel", "streamId": "s1"})),
            {"type": "cancel", "streamId": "s1"})

    def test_extra_keys_dropped_by_projection(self):
        # schemastery 投影：未知键丢弃而非报错
        parsed = parse_remote_stream_client_message(json.dumps(
            {"type": "open", "streamId": "s1", "endpoint": "e", "payload": {},
             "garbage": 1}))
        self.assertEqual(parsed,
                         {"type": "open", "streamId": "s1", "endpoint": "e",
                          "payload": {}})

    def test_invalid_shapes_rejected(self):
        for bad in (
            None,
            42,
            "text",
            [],
            {},
            {"type": "open"},
            {"type": "open", "streamId": "s1", "endpoint": "e"},
            {"type": "open", "streamId": 5, "endpoint": "e", "payload": {}},
            {"type": "open", "streamId": "s1", "endpoint": "", "payload": {}},
            {"type": "cancel"},
            {"type": "cancel", "streamId": ""},
            {"type": "nope", "streamId": "s1"},
        ):
            with self.assertRaises(StreamProtocolError):
                parse_remote_stream_client_message(json.dumps(bad))

    def test_deep_extra_open_rejected_clean(self):
        # 判别字段都在、payload 深层结构 —— payload 值任意合法
        payload = {"args": {"request": {"x": {"y": []}}}}
        parsed = parse_remote_stream_client_message(
            json.dumps({"type": "open", "streamId": "s1", "endpoint": "e",
                        "payload": payload}))
        self.assertEqual(parsed["payload"], payload)

    def test_json_decode_failures(self):
        for text in ("", "not json", "42", '"str"'):
            with self.assertRaises(StreamProtocolError):
                parse_remote_stream_client_message(text)

    def test_non_object_json_rejected(self):
        for text in ('[1]', '"x"', 'null'):
            with self.assertRaises(StreamProtocolError):
                parse_remote_stream_client_message(text)


class TestServerMessages(unittest.TestCase):
    def test_item_roundtrip(self):
        for frame in ({"type": "item", "streamId": "s1"},
                      {"type": "item", "streamId": "s1", "value": {"ok": True}},
                      {"type": "item", "streamId": "s1", "value": [1, 2, 3]},
                      {"type": "item", "streamId": "s1", "value": None}):
            self.assertEqual(
                parse_remote_stream_server_message(json.dumps(frame)), frame)

    def test_end_roundtrip(self):
        self.assertEqual(parse_remote_stream_server_message(
            json.dumps({"type": "end", "streamId": "s1"})),
            {"type": "end", "streamId": "s1"})

    def test_error_roundtrip(self):
        frame = {"type": "error", "streamId": "s1",
                 "error": {"code": "gateway/internal", "message": "boom", "details": {}}}
        self.assertEqual(parse_remote_stream_server_message(json.dumps(frame)), frame)

    def test_error_details_required_object(self):
        for bad in (
            {"type": "error", "streamId": "s1",
             "error": {"code": "x", "message": "x"}},
            {"type": "error", "streamId": "s1",
             "error": {"code": 5, "message": "x", "details": {}}},
            {"type": "error", "streamId": "s1",
             "error": {"code": "x", "message": "x", "details": []}},
            {"type": "error", "streamId": "s1", "error": "string"},
        ):
            with self.assertRaises(StreamProtocolError):
                parse_remote_stream_server_message(json.dumps(bad))

    def test_unknown_type_rejected(self):
        for bad in ({"type": "nope", "streamId": "s1"},
                    {"type": "item", "streamId": ""},
                    {"type": "end", "streamId": 0},
                    {"streamId": "s1"}):
            with self.assertRaises(StreamProtocolError):
                parse_remote_stream_server_message(json.dumps(bad))


class TestJsonValue(unittest.TestCase):
    def test_plain_values_lossless(self):
        for value in (None, True, False, "x", 0, 1, -1, 1.5, [], {}, {"a": [1, {"b": None}]}):
            self.assertTrue(is_remote_json_value(value))

    def test_non_finite_rejected(self):
        from math import inf, nan
        for value in (inf, -inf, nan, float("inf")):
            self.assertFalse(is_remote_json_value(value))

    def test_negzero_rejected(self):
        self.assertFalse(is_remote_json_value(-0.0))
        self.assertFalse(is_remote_json_value([-0.0]))
        self.assertTrue(is_remote_json_value(0.0))

    def test_decorated_rejected(self):
        from collections import OrderedDict
        self.assertFalse(is_remote_json_value(OrderedDict()))
        self.assertFalse(is_remote_json_value({"list": (1, 2)}))
        self.assertFalse(is_remote_json_value({1: 2}))
        self.assertFalse(is_remote_json_value(set()))

    def test_cyclic_rejected(self):
        ref = {}
        ref["self"] = ref
        self.assertFalse(is_remote_json_value(ref))
        lst = [1]
        lst.append(lst)
        self.assertFalse(is_remote_json_value(lst))

    def test_large_ints_lossless(self):
        # Python 任意精度 int 可由 json 无损序列化（无 IEEE754 精度损失）
        self.assertTrue(is_remote_json_value(1 << 200))
        self.assertTrue(is_remote_json_value(10 ** 400))


class TestEventResultOutcome(unittest.TestCase):
    def test_payload_requires_args(self):
        for bad in (None, 42, "x", [], {}, {"clientId": "c", "eventId": "e",
                                            "outcome": {"kind": "next"}},
                    {"args": None}, {"args": "x"}, {"args": []}):
            with self.assertRaises(StreamProtocolError):
                parse_remote_event_result_payload(bad)

    def test_next(self):
        payload = {"args": {"clientId": "c1", "eventId": "e1",
                            "outcome": {"kind": "next"}}}
        self.assertEqual(parse_remote_event_result_payload(payload),
                         {"clientId": "c1", "eventId": "e1",
                          "outcome": {"kind": "next"}})

    def test_result_with_and_without_value(self):
        payload = {"args": {"clientId": "c1", "eventId": "e1",
                            "outcome": {"kind": "result", "value": "allowed-once"}}}
        self.assertEqual(parse_remote_event_result_payload(payload),
                         {"clientId": "c1", "eventId": "e1",
                          "outcome": {"kind": "result", "value": "allowed-once"}})
        bare = {"args": {"clientId": "c1", "eventId": "e1",
                         "outcome": {"kind": "result"}}}
        self.assertEqual(parse_remote_event_result_payload(bare),
                         {"clientId": "c1", "eventId": "e1",
                          "outcome": {"kind": "result"}})

    def test_rejected_variants(self):
        base = {"args": {"clientId": "c1", "eventId": "e1", "outcome": {
            "kind": "rejected", "error": {"name": "Err", "message": "nope"}}}}
        self.assertEqual(parse_remote_event_result_payload(base),
                         {"clientId": "c1", "eventId": "e1",
                          "outcome": {"kind": "rejected",
                                      "error": {"name": "Err", "message": "nope"}}})
        with_details = {"args": {"clientId": "c1", "eventId": "e1", "outcome": {
            "kind": "rejected",
            "error": {"name": "Err", "message": "nope", "code": "cancelled",
                      "details": {"x": 1}}}}}
        self.assertEqual(
            parse_remote_event_result_payload(with_details),
            {"clientId": "c1", "eventId": "e1",
             "outcome": {"kind": "rejected",
                         "error": {"name": "Err", "message": "nope", "code": "cancelled",
                                   "details": {"x": 1}}}})

    def test_unknown_keys_dropped_not_rejected(self):
        payload = {"args": {"clientId": "c1", "eventId": "e1", "extra": True,
                            "outcome": {"kind": "result", "value": "x", "junk": []}}}
        self.assertEqual(parse_remote_event_result_payload(payload),
                         {"clientId": "c1", "eventId": "e1",
                          "outcome": {"kind": "result", "value": "x"}})

    def test_invalid_outcomes_rejected(self):
        for outcome in (
            {},
            {"kind": "nope"},
            {"kind": "next", "value": 1},
            {"kind": "result", "value": float("nan")},
            {"kind": "result", "error": {}},
            {"kind": "rejected"},
            {"kind": "rejected", "error": {}},
            {"kind": "rejected", "error": {"message": "x"}},
            {"kind": "rejected", "error": {"name": "", "message": "x"}},
            {"kind": "rejected", "error": {"name": "E", "message": "x", "code": 5}},
            {"kind": "rejected", "error": {"name": "E", "message": "x",
                                           "details": float("inf")}},
        ):
            with self.assertRaises(StreamProtocolError):
                parse_remote_event_result_payload(
                    {"args": {"clientId": "c1", "eventId": "e1", "outcome": outcome}})

    def test_ids_required_strings(self):
        for client_id, event_id in (("", "e"), ("c", ""), (5, "e"), ("c", None)):
            with self.assertRaises(StreamProtocolError):
                parse_remote_event_result_payload(
                    {"args": {"clientId": client_id, "eventId": event_id,
                              "outcome": {"kind": "next"}}})


if __name__ == "__main__":
    unittest.main()