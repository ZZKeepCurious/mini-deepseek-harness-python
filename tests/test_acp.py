"""第 12 章测试：ACP 最小子集 —— 自动化专用协议服务。"""
import base64
import os
import tempfile
import unittest

from miniharness.attachment import LocalAttachmentStore
from miniharness.llm import FakeLlmAdapter
from miniharness.protocol.acp import (
    AcpRequestError,
    AcpServer,
    acp_prompt_to_text,
    invalid_params,
    prompt_has_unsupported_content,
    supports_acp_image_prompts,
    turn_end_to_stop_reason,
)

# 双平台均为绝对路径（CI 在 ubuntu 与 windows 上都会跑）
_CWD = os.path.abspath("work")

# 1x1 PNG（base64，可被 stdlib 头部解析）
_ONE_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADElEQVR4nGP4//8/AAAF/gL+p5ocAAAAAElFTkSuQmCC"
)
_ONE_PNG = base64.b64decode(_ONE_PNG_B64)


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
        # 输入先落 agent/inbox/spliced，随后真实回合 turn/start → turn/end
        self.assertEqual(loop.session.events[0]["type"], "agent/inbox/spliced")
        self.assertIn("turn/start", [e["type"] for e in loop.session.events])
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
        # 未宣称 image 能力（默认无 attachment store）→ image prompt 拒绝
        with self.assertRaises(AcpRequestError) as cm:
            self.server.prompt(self.session_id, [{"type": "image", "attachment": {}}])
        self.assertEqual(cm.exception.code, -32602)
        self.assertIn("not advertised", cm.exception.detail)

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


class TestAcpRichMedia(unittest.TestCase):
    """富媒体跟进：image 能力如实宣称 + 输入受理 + 输出回传。"""

    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="mini-acp-rm-")
        self.store = LocalAttachmentStore(root=self._tmp)
        self.server = AcpServer(adapter=FakeLlmAdapter(), attachment=self.store)
        self.session_id = self.server.new_session(_CWD)["sessionId"]

    def tearDown(self):
        import shutil
        shutil.rmtree(self._tmp, ignore_errors=True)

    def test_initialize_advertises_image_when_supported(self):
        info = self.server.initialize()
        self.assertTrue(info["agentCapabilities"]["promptCapabilities"]["image"])
        self.assertEqual(info["agentCapabilities"]["promptCapabilities"]["audio"], False)

    def test_no_store_means_no_image_capability(self):
        server = AcpServer(adapter=FakeLlmAdapter(), attachment=None)
        info = server.initialize()
        self.assertFalse(info["agentCapabilities"]["promptCapabilities"]["image"])

    def test_supports_flag_requires_text_only_adapter_false(self):
        # DeepSeek 适配器只支持文本 → image 能力不宣称（对齐 adapter resolveModelInfo）
        from miniharness.llm import DeepSeekAdapter
        self.assertFalse(supports_acp_image_prompts(
            self.store, DeepSeekAdapter(model="deepseek-chat")))

    def test_prompt_with_image_accepted_and_stored(self):
        result = self.server.prompt(self.session_id, [
            {"type": "text", "text": "看看这张图"},
            {"type": "image", "mimeType": "image/png", "data": _ONE_PNG_B64},
        ])
        self.assertEqual(result["stopReason"], "end_turn")
        loop = self.server.sessions[self.session_id]["loop"]
        user_msg = next(
            e["data"] for e in loop.session.events
            if e["type"] == "user/message" and e["data"]["role"] == "user"
        )
        image_block = next(
            b for b in user_msg["content"] if b.get("type") == "image")
        ref = image_block["attachment"]
        self.assertTrue(str(ref["attachmentId"]).startswith("sha256:"))
        # 存储里确实有该对象（引用读回字节一致）
        from miniharness.attachment import AttachmentId, ImageAttachmentRef
        roundtrip = self.store.read_image(ImageAttachmentRef(
            attachmentId=AttachmentId(ref["attachmentId"]),
            mediaType=ref["mediaType"],
            bytes=ref["bytes"],
            width=ref["width"],
            height=ref["height"],
        ))
        self.assertEqual(roundtrip.data, _ONE_PNG)

    def test_prompt_image_without_advertised_capability_rejected(self):
        # 无 store 的 server 未宣称 image → image prompt invalid params
        server = AcpServer(adapter=FakeLlmAdapter(), attachment=None)
        session_id = server.new_session(_CWD)["sessionId"]
        with self.assertRaises(AcpRequestError) as cm:
            server.prompt(session_id, [
                {"type": "image", "mimeType": "image/png", "data": _ONE_PNG_B64},
            ])
        self.assertEqual(cm.exception.code, -32602)

    def test_prompt_bad_base64_rejected(self):
        with self.assertRaises(AcpRequestError) as cm:
            self.server.prompt(self.session_id, [
                {"type": "image", "mimeType": "image/png",
                 "data": "not!!valid!!base64"},
            ])
        self.assertEqual(cm.exception.code, -32602)

    def test_prompt_bad_mime_rejected(self):
        with self.assertRaises(AcpRequestError) as cm:
            self.server.prompt(self.session_id, [
                {"type": "image", "mimeType": "image/tiff", "data": _ONE_PNG_B64},
            ])
        self.assertEqual(cm.exception.code, -32602)

    def test_assistant_image_output_roundtrips_base64(self):
        # 假模型输出一个 image 块 → ACP 更新流读回 base64 内联
        from miniharness.attachment import SaveImageAttachment
        ref = self.store.save_image(
            SaveImageAttachment(data=_ONE_PNG, mediaType="image/png"))
        self.server = AcpServer(
            adapter=FakeLlmAdapter(image=ref.to_dict()), attachment=self.store)
        session_id = self.server.new_session(_CWD)["sessionId"]
        self.server.prompt(session_id, [{"type": "text", "text": "hi"}])
        update = self.server.updates[-1]
        self.assertEqual(update["update"]["sessionUpdate"], "agent_message_chunk")
        # 逐 block 一条 update（content 为单块，对齐上游 index.ts:230-237）
        self.assertEqual(update["update"]["content"]["type"], "image")
        self.assertEqual(update["update"]["content"]["mimeType"], "image/png")
        self.assertEqual(base64.b64decode(update["update"]["content"]["data"]), _ONE_PNG)


class TestDeepSeekSerializeImageRejection(unittest.TestCase):
    """对齐上游 serialize.ts assertTextOnly：image 块显式拒绝不静默丢弃。"""

    def test_serialize_rejects_image_block(self):
        from miniharness.llm import LlmFailure, serialize_messages
        from miniharness.llm.deepseek import UNSUPPORTED_CONTENT
        from miniharness.core.session import image_block

        message = {
            "id": "m1", "role": "user",
            "content": [image_block({"attachmentId": "sha256:" + "0" * 64})],
            "source": {"kind": "user"},
        }
        with self.assertRaises(LlmFailure) as cm:
            serialize_messages([message])
        self.assertEqual(cm.exception.code, UNSUPPORTED_CONTENT)


if __name__ == "__main__":
    unittest.main()