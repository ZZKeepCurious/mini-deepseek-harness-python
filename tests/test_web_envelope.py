"""web 表面测试：两信封 RPC（对齐 `packages/client/connection/src/rpc-schema.ts` + rpc.ts）。"""
import unittest

from miniharness.web.envelope import (
    RPC_ERROR_CODES,
    EnvelopeError,
    client_request,
    parse_message,
    rpc_id,
    rpc_error,
    rpc_result_error,
    rpc_result_ok,
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
        req = client_request(rid, "session.list", {"args": {}})
        resp = server_response(req["rpcId"], rpc_result_ok({"items": []}))
        self.assertEqual(resp["rpcId"], rid)


class TestErrorCodeSet(unittest.TestCase):
    def test_closed_set_matches_session_error_details_map(self):
        # RemoteErrorDetailsMap 键集（alpha.2 命名空间码，typert-protocol/types.ts + 各域注册）23 码
        # （R3 闭合：路由层边界校验新增 gateway/input-invalid）
        self.assertEqual(len(RPC_ERROR_CODES), 23)

    def test_known_codes_present(self):
        for code in ("gateway/bad-request", "session/not-found", "session/model-unavailable",
                     "session/conflict", "session/invalid-time-zone", "workspace/not-found",
                     "agent-preset/conflict", "session/agent-busy",
                     "session/attachment-invalid", "session/queue-item-not-found",
                     "session/steer-unavailable", "session/title-invalid",
                     "session/fork-unavailable", "subagent/not-found",
                     "subagent/unauthorized", "gateway/internal", "gateway/cancelled",
                     "gateway/arguments-invalid", "gateway/input-invalid"):
            self.assertIn(code, RPC_ERROR_CODES)

    def test_apiproxy_only_codes_retired(self):
        # 旧 apiproxy 域码已随 apiproxy 删除退出闭集
        for code in ("command-error", "unknown-command", "directory-exists",
                     "workspace-invalid-path", "model-discovery-failed"):
            self.assertNotIn(code, RPC_ERROR_CODES)

    def test_out_of_set_code_fails_loud(self):
        with self.assertRaises(EnvelopeError):
            rpc_error("not-a-real-code", "x")

    def test_internal_is_explicit_details(self):
        err = rpc_error("gateway/internal", "boom", {})
        self.assertEqual(err, {"code": "gateway/internal", "message": "boom", "details": {}}) 


class TestTransportError(unittest.TestCase):
    def test_folds_exception_to_internal(self):
        result = transport_error(ValueError("kaboom"))
        self.assertEqual(result, {"ok": False, "error": {
            "code": "gateway/internal", "message": "kaboom", "details": {}}})

    def test_folds_non_error_value(self):
        result = transport_error("raw string")
        self.assertEqual(result["error"]["message"], "raw string")


class TestResult(unittest.TestCase):
    def test_ok_branch_with_value(self):
        self.assertEqual(rpc_result_ok({"a": 1}), {"ok": True, "value": {"a": 1}})

    def test_ok_branch_omits_absent_value(self):
        # 上游结果 undefined 时整体省略 value（JSON 无 undefined）
        self.assertEqual(rpc_result_ok(), {"ok": True})

    def test_error_branch(self):
        result = rpc_result_error(rpc_error("session/not-found", "missing", {"sessionId": "s1"}))
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"]["details"], {"sessionId": "s1"})


class TestMessages(unittest.TestCase):
    def test_two_members_discriminated_by_type(self):
        msgs = [
            client_request("a", "session.list", {"args": {}}),
            server_response("b", rpc_result_ok({"accepted": True})),
        ]
        self.assertEqual({m["type"] for m in msgs},
                         {"client-request", "server-response"})

    def test_roundtrip_both(self):
        for m in (client_request("a", "session.list", {"args": {}}),
                  client_request("a", "session.prompt", {"args": {"sessionId": "s"}}),
                  server_response("b", rpc_result_ok({"accepted": True})),
                  server_response("b", rpc_result_ok()),
                  server_response("b", rpc_result_error(rpc_error("gateway/cancelled", "x", {})))):
            self.assertEqual(parse_message(m), m)

    def test_invalid_shapes_rejected(self):
        for bad in (
            None,
            42,
            "text",
            {},
            {"type": "nope", "rpcId": "x"},
            {"type": "server-request", "rpcId": "x", "method": "m", "payload": {}},
            {"type": "client-request", "rpcId": "x", "method": 5, "payload": {}},
            {"type": "client-request", "rpcId": "x", "method": "m"},
            {"type": "server-response", "rpcId": "x", "result": {}},
            {"type": "server-response", "rpcId": "x", "result": {"ok": False}},
            {"type": "server-response", "rpcId": "x",
             "result": {"ok": False, "error": {"code": 5, "message": "x", "details": {}}}},
            {"type": "server-response", "rpcId": "x",
             "result": {"ok": False, "error": {"code": "x", "message": "x", "details": []}}},
        ):
            with self.assertRaises(EnvelopeError):
                parse_message(bad)

    def test_extra_keys_accepted(self):
        # schemastery 对象 schema 忽略未知附加键
        msg = client_request("a", "session.list", {"args": {}})
        msg["extra"] = 1
        self.assertEqual(parse_message(msg), msg)


if __name__ == "__main__":
    unittest.main()