"""web 表面测试：四象限 RPC 信封（对齐 `packages/host/apiproxy/src/api/rpc.ts`）。"""
import unittest

from miniharness.web.envelope import (
    RPC_ERROR_CODES,
    EnvelopeError,
    client_request,
    client_response,
    parse_message,
    rpc_id,
    rpc_error,
    rpc_receipt_accepted,
    rpc_receipt_rejected,
    rpc_result_error,
    rpc_result_ok,
    server_request,
    server_response,
    transport_error,
)


class TestRpcId(unittest.TestCase):
    def test_minted_by_initiator_uuid(self):
        # 发起方签发 UUID；响应回显，从不重铸
        rid = rpc_id()
        self.assertIsInstance(rid, str)
        self.assertTrue(rid)

    def test_response_echoes_request_id(self):
        rid = rpc_id()
        req = client_request(rid, "session.list", {})
        resp = server_response(req["rpcId"], rpc_result_ok([]))
        self.assertEqual(resp["rpcId"], rid)


class TestErrorCodeSet(unittest.TestCase):
    def test_closed_set_size_matches_upstream(self):
        # RpcErrorDetailsMap 键集共 39 个
        self.assertEqual(len(RPC_ERROR_CODES), 39)

    def test_known_codes_present(self):
        for code in ("bad-request", "session-not-found", "model-unavailable",
                     "agent-busy", "attachment-error", "command-error",
                     "unknown-command", "internal", "cancelled"):
            self.assertIn(code, RPC_ERROR_CODES)

    def test_out_of_set_code_fails_loud(self):
        with self.assertRaises(EnvelopeError):
            rpc_error("not-a-real-code", "x")

    def test_internal_is_explicit_details(self):
        err = rpc_error("internal", "boom", {})
        self.assertEqual(err, {"code": "internal", "message": "boom", "details": {}})


class TestTransportError(unittest.TestCase):
    def test_folds_exception_to_internal(self):
        result = transport_error(ValueError("kaboom"))
        self.assertEqual(result, {"ok": False, "error": {
            "code": "internal", "message": "kaboom", "details": {}}})

    def test_folds_non_error_value(self):
        result = transport_error("raw string")
        self.assertEqual(result["error"]["message"], "raw string")


class TestResult(unittest.TestCase):
    def test_ok_branch(self):
        self.assertEqual(rpc_result_ok({"a": 1}), {"ok": True, "value": {"a": 1}})

    def test_error_branch(self):
        result = rpc_result_error(rpc_error("session-not-found", "missing", {"sessionId": "s1"}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["details"], {"sessionId": "s1"})


class TestMessages(unittest.TestCase):
    def test_four_members_discriminated_by_type(self):
        msgs = [
            client_request("a", "session.list", {}),
            server_response("b", rpc_result_ok(None)),
            server_request("c", "approval/requested", {}),
            client_response("d", rpc_result_ok(None)),
        ]
        self.assertEqual({m["type"] for m in msgs},
                         {"client-request", "server-response", "server-request", "client-response"})

    def test_roundtrip_all_four(self):
        for m in (client_request("a", "session.list", {}),
                  server_response("b", rpc_result_ok([])),
                  server_request("c", "approval/requested", {"id": 1}),
                  client_response("d", rpc_result_error(rpc_error("cancelled", "x", {})))):
            self.assertEqual(parse_message(m), m)

    def test_invalid_shapes_rejected(self):
        for bad in (
            None,
            42,
            "text",
            {},
            {"type": "nope", "rpcId": "x"},
            {"type": "client-request", "rpcId": "x", "method": 5, "payload": {}},
            {"type": "client-request", "rpcId": "x", "method": "m"},
            {"type": "server-response", "rpcId": "x", "result": {}},
            {"type": "server-response", "rpcId": "x", "result": {"ok": False}},
        ):
            with self.assertRaises(EnvelopeError):
                parse_message(bad)


class TestReceipt(unittest.TestCase):
    def test_accepted(self):
        self.assertEqual(rpc_receipt_accepted(), {"accepted": True})

    def test_rejected_reasons(self):
        self.assertEqual(rpc_receipt_rejected("not-pending"),
                         {"accepted": False, "reason": "not-pending"})
        self.assertEqual(rpc_receipt_rejected("bad-response"),
                         {"accepted": False, "reason": "bad-response"})

    def test_unknown_reason_fails_loud(self):
        with self.assertRaises(EnvelopeError):
            rpc_receipt_rejected("other")


if __name__ == "__main__":
    unittest.main()