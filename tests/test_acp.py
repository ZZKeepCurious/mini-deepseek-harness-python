"""第 12 章测试：ACP 最小子集 —— 自动化专用协议服务。"""
import os
import unittest

from miniharness.acp import (
    AcpRequestError,
    AcpServer,
    acp_prompt_to_text,
    invalid_params,
    prompt_has_unsupported_content,
    turn_end_to_stop_reason,
)

# 双平台均为绝对路径（CI 在 ubuntu 与 windows 上都会跑）
_CWD = os.path.abspath("work")


class TestAcpHandshake(unittest.TestCase):
    def test_initialize_advertises_no_fancy_capabilities(self):
        server = AcpServer()
        info = server.initialize()
        self.assertEqual(info["agentInfo"]["name"], "deepseek-harness-acp")
        self.assertEqual(info["agentCapabilities"]["promptCapabilities"],
                         {"image": False, "audio": False, "embeddedContext": False})
        self.assertEqual(info["authMethods"], [])

    def test_authenticate_is_noop(self):
        self.assertIsNone(AcpServer().authenticate())


class TestAcpSession(unittest.TestCase):
    def setUp(self):
        self.server = AcpServer()

    def test_new_session_requires_absolute_cwd(self):
        with self.assertRaises(AcpRequestError) as cm:
            self.server.new_session("relative/path")
        self.assertEqual(cm.exception.code, -32602)
        self.assertIn("absolute path", cm.exception.detail)

    def test_new_session_rejects_additional_directories(self):
        with self.assertRaises(AcpRequestError) as cm:
            self.server.new_session(_CWD, additional_directories=["/x"])
        self.assertIn("additionalDirectories", cm.exception.detail)

    def test_new_session_rejects_mcp_servers(self):
        with self.assertRaises(AcpRequestError) as cm:
            self.server.new_session(_CWD, mcp_servers=[{"name": "s"}])
        self.assertIn("mcpServers", cm.exception.detail)

    def test_new_session_mints_id(self):
        result = self.server.new_session(_CWD)
        self.assertIn("sessionId", result)
        self.assertIn(result["sessionId"], self.server.sessions)


class TestAcpPrompt(unittest.TestCase):
    def setUp(self):
        self.server = AcpServer()
        self.session_id = self.server.new_session(_CWD)["sessionId"]

    def test_prompt_text_returns_end_turn(self):
        result = self.server.prompt(self.session_id,
                                    [{"type": "text", "text": "你好"}])
        self.assertEqual(result["stopReason"], "end_turn")
        loop = self.server.sessions[self.session_id]["loop"]
        self.assertEqual(loop.session.events[0]["type"], "turn/start")
        self.assertEqual(loop.session.events[-1]["type"], "turn/end")

    def test_prompt_unknown_session_rejected(self):
        with self.assertRaises(AcpRequestError) as cm:
            self.server.prompt("no-such-session", [{"type": "text", "text": "hi"}])
        self.assertEqual(cm.exception.code, -32602)
        self.assertIn("unknown session", cm.exception.detail)

    def test_prompt_empty_rejected(self):
        with self.assertRaises(AcpRequestError) as cm:
            self.server.prompt(self.session_id, [{"type": "text", "text": "   "}])
        self.assertIn("empty prompt", cm.exception.detail)

    def test_prompt_unsupported_content_rejected(self):
        with self.assertRaises(AcpRequestError) as cm:
            self.server.prompt(self.session_id, [{"type": "image", "attachment": {}}])
        self.assertIn("only text and resource_link", cm.exception.detail)

    def test_prompt_inflight_rejected(self):
        self.server.sessions[self.session_id]["inflight"] = True
        with self.assertRaises(AcpRequestError) as cm:
            self.server.prompt(self.session_id, [{"type": "text", "text": "hi"}])
        self.assertIn("already in flight", cm.exception.detail)

    def test_prompt_resource_link_flattened(self):
        result = self.server.prompt(self.session_id, [
            {"type": "text", "text": "读文件"},
            {"type": "resource_link", "name": "notes", "uri": "file:///tmp/a.txt"},
        ])
        self.assertEqual(result["stopReason"], "end_turn")

    def test_cancel_unknown_session_noop(self):
        self.server.cancel("no-such-session")   # 不抛

    def test_cancel_known_session(self):
        self.server.prompt(self.session_id, [{"type": "text", "text": "hi"}])
        self.server.cancel(self.session_id)     # 已 idle：no-op，不破坏会话
        self.server.prompt(self.session_id, [{"type": "text", "text": "再来"}])
        self.assertEqual(self.server.sessions[self.session_id]["loop"]
                         .session.events[-1]["type"], "turn/end")


class TestStopReasonMapping(unittest.TestCase):
    def test_mapping_table(self):
        cases = [
            ({"kind": "completed"}, "end_turn"),
            ({"kind": "max-tokens"}, "max_tokens"),
            ({"kind": "aborted"}, "end_turn"),          # cancelled 留给显式取消
            ({"kind": "interrupted"}, "cancelled"),
            ({"kind": "blocked"}, "end_turn"),
            ({"kind": "error"}, "end_turn"),
        ]
        for reason, expected in cases:
            self.assertEqual(turn_end_to_stop_reason(reason), expected, reason)

    def test_unknown_kind_defaults_end_turn(self):
        self.assertEqual(turn_end_to_stop_reason({"kind": "weird"}), "end_turn")


class TestAcpCodec(unittest.TestCase):
    def test_prompt_to_text_concatenates(self):
        text = acp_prompt_to_text([{"type": "text", "text": "a"},
                                   {"type": "text", "text": "b"}])
        self.assertEqual(text, "ab")

    def test_prompt_to_text_renders_resource_link(self):
        text = acp_prompt_to_text([{"type": "resource_link", "name": "n",
                                    "uri": "file:///x"}])
        self.assertIn("[resource_link name='n' uri='file:///x']", text)

    def test_unsupported_detection(self):
        self.assertFalse(prompt_has_unsupported_content(
            [{"type": "text", "text": "a"}]))
        self.assertTrue(prompt_has_unsupported_content(
            [{"type": "image", "attachment": {}}]))


class TestAcpApprovalBridge(unittest.TestCase):
    def setUp(self):
        self.server = AcpServer()

    def test_call_id_missing_delegates(self):
        called = []
        result = self.server.bridge_approval({"toolName": "bash"},
                                             lambda: called.append(True) or "x")
        self.assertEqual(result, "x")
        self.assertEqual(called, [True])

    def test_allow_once_maps(self):
        self.server.set_answerer(lambda req: "allow-once")
        result = self.server.bridge_approval({"toolName": "bash", "callId": "c1"},
                                             lambda: "never")
        self.assertEqual(result, "allowed-once")

    def test_reject_once_maps(self):
        self.server.set_answerer(lambda req: "reject-once")
        result = self.server.bridge_approval({"toolName": "bash", "callId": "c1"},
                                             lambda: "never")
        self.assertEqual(result, "rejected")

    def test_cancelled_maps(self):
        self.server.set_answerer(lambda req: "cancelled")
        result = self.server.bridge_approval({"toolName": "bash", "callId": "c1"},
                                             lambda: "never")
        self.assertEqual(result, "cancelled")

    def test_default_answerer_allows(self):
        result = self.server.bridge_approval({"toolName": "bash", "callId": "c1"},
                                             lambda: "never")
        self.assertEqual(result, "allowed-once")


class TestAcpLifecycle(unittest.TestCase):
    def test_closed_bridge_rejects(self):
        server = AcpServer()
        server.close()
        with self.assertRaises(AcpRequestError) as cm:
            server.new_session("/work")
        self.assertEqual(cm.exception.code, -32603)
        self.assertIn("disposed", cm.exception.detail)

    def test_error_helper_codes(self):
        self.assertEqual(invalid_params("x").code, -32602)
        self.assertEqual(invalid_params("x").detail, "x")


if __name__ == "__main__":
    unittest.main()