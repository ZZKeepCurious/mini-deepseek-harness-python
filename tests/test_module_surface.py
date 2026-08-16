"""步骤 2 验收：llm / session 拆分后的模块面与文件边界。

上游对照：packages/llm/llm/src + packages/llm/llm-deepseek/src + packages/core/session/src
（index.ts 聚合再导出，子路径即声明边界）。

本测试钉死两件事：
  1. 聚合再导出：`miniharness.llm` / `miniharness.core.session` 保持拆分前的
     旧模块面（全集再导出），外部浅路径 import 继续可用；
  2. 文件边界：每个符号落在文档约定的文件里，深路径/浅路径同一对象
     （架构文档 §4.1 目录树，步骤 2）。
"""
import unittest

from miniharness import llm
from miniharness.core import session
from miniharness.core.session import (
    KNOWN_TYPES,
    SESSION_FORMAT_VERSION,
    SURFACE_TYPES,
    TOOL_NOT_STARTED,
    TOOL_OUTCOME_UNKNOWN,
    Session,
    create_message,
    deep_freeze,
    derive_messages,
    is_json_safe,
    now_ms,
    reasoning_block,
    repair_interrupted_turn,
    text_block,
    thaw,
    tool_call_block,
    tool_result_block,
    turn_balance,
)
from miniharness.llm import (
    AUTH,
    CONTEXT_WINDOW_EXCEEDED,
    EMPTY_RESPONSE,
    RATE_LIMIT,
    REQUEST_ERROR,
    SERVER,
    STREAM_CHUNK_KINDS,
    STREAM_CLOSED,
    TIMEOUT,
    TRANSPORT,
    BlockAssembler,
    DeepSeekAdapter,
    FakeLlmAdapter,
    LlmAdapter,
    LlmFailure,
    StreamChunk,
    provider_retry_after_ms,
    request_id,
    serialize_messages,
)


class SessionModuleSurfaceTest(unittest.TestCase):
    """拆前 session.py 的模块面必须全部经聚合器再导出。"""

    def test_old_module_face_preserved(self):
        expected = {
            "SESSION_FORMAT_VERSION", "KNOWN_TYPES", "SURFACE_TYPES",
            "TOOL_NOT_STARTED", "TOOL_OUTCOME_UNKNOWN", "Session",
            "create_message", "deep_freeze", "derive_messages", "is_json_safe",
            "now_ms", "reasoning_block", "repair_interrupted_turn", "text_block",
            "thaw", "tool_call_block", "tool_result_block", "turn_balance",
        }
        self.assertLessEqual(expected, set(session.__all__ if hasattr(session, "__all__") else dir(session)))

    def test_file_boundaries(self):
        """每个符号落在架构文档 §4.1 约定的文件里。"""
        from miniharness.core.session import invariant, json as session_json, message, repair, surface, types

        self.assertIs(session.Session, session.session.Session)
        self.assertIs(is_json_safe, session_json.is_json_safe)
        self.assertIs(deep_freeze, session_json.deep_freeze)
        self.assertIs(now_ms, session_json.now_ms)
        self.assertIs(thaw, session_json.thaw)
        self.assertIs(KNOWN_TYPES, types.KNOWN_TYPES)
        self.assertIs(SURFACE_TYPES, types.SURFACE_TYPES)
        self.assertIs(SESSION_FORMAT_VERSION, types.SESSION_FORMAT_VERSION)
        self.assertIs(TOOL_NOT_STARTED, types.TOOL_NOT_STARTED)
        self.assertIs(TOOL_OUTCOME_UNKNOWN, types.TOOL_OUTCOME_UNKNOWN)
        self.assertIs(create_message, message.create_message)
        self.assertIs(text_block, message.text_block)
        self.assertIs(reasoning_block, message.reasoning_block)
        self.assertIs(tool_call_block, message.tool_call_block)
        self.assertIs(tool_result_block, message.tool_result_block)
        self.assertIs(repair_interrupted_turn, repair.repair_interrupted_turn)
        self.assertIs(turn_balance, repair.turn_balance)
        self.assertIs(derive_messages, surface.derive_messages)
        self.assertEqual(invariant.NEXT_TURN, 1)
        self.assertEqual(invariant.NEXT_STEP, 1)
        self.assertTrue(callable(invariant.validate_event))

    def test_message_builders_live_in_session_domain(self):
        """message.py 保留在会话域（L0 不依赖 llm），上游在 llm/llm/src/message.ts。"""
        from miniharness.core.session import message

        self.assertFalse(hasattr(message, "LlmAdapter"))


class LlmModuleSurfaceTest(unittest.TestCase):
    """拆前 llm.py 的模块面必须全部经聚合器再导出。"""

    def test_old_module_face_preserved(self):
        expected = {
            "STREAM_CHUNK_KINDS", "AUTH", "RATE_LIMIT", "CONTEXT_WINDOW_EXCEEDED",
            "SERVER", "TIMEOUT", "TRANSPORT", "STREAM_CLOSED", "EMPTY_RESPONSE",
            "REQUEST_ERROR", "StreamChunk", "LlmFailure", "LlmAdapter",
            "BlockAssembler", "FakeLlmAdapter", "serialize_messages",
            "provider_retry_after_ms", "request_id", "DeepSeekAdapter",
        }
        self.assertLessEqual(expected, set(llm.__all__ if hasattr(llm, "__all__") else dir(llm)))

    def test_file_boundaries(self):
        """协议 / fake / wire 分属 protocol.py / fake.py / deepseek.py。"""
        from miniharness.llm import deepseek, fake, protocol

        self.assertIs(StreamChunk, protocol.StreamChunk)
        self.assertIs(LlmFailure, protocol.LlmFailure)
        self.assertIs(LlmAdapter, protocol.LlmAdapter)
        self.assertIs(BlockAssembler, protocol.BlockAssembler)
        self.assertIs(STREAM_CHUNK_KINDS, protocol.STREAM_CHUNK_KINDS)
        self.assertIs(FakeLlmAdapter, fake.FakeLlmAdapter)
        self.assertIs(DeepSeekAdapter, deepseek.DeepSeekAdapter)
        self.assertIs(serialize_messages, deepseek.serialize_messages)
        self.assertIs(provider_retry_after_ms, deepseek.provider_retry_after_ms)
        self.assertIs(request_id, deepseek.request_id)
        # 错误码词汇在协议层（llm/llm/src），wire 层复用
        self.assertIs(AUTH, protocol.AUTH)
        self.assertIs(CONTEXT_WINDOW_EXCEEDED, protocol.CONTEXT_WINDOW_EXCEEDED)
        self.assertIs(EMPTY_RESPONSE, protocol.EMPTY_RESPONSE)
        self.assertIs(RATE_LIMIT, protocol.RATE_LIMIT)
        self.assertIs(REQUEST_ERROR, protocol.REQUEST_ERROR)
        self.assertIs(SERVER, protocol.SERVER)
        self.assertIs(STREAM_CLOSED, protocol.STREAM_CLOSED)
        self.assertIs(TIMEOUT, protocol.TIMEOUT)
        self.assertIs(TRANSPORT, protocol.TRANSPORT)


class AdapterBoundaryTest(unittest.TestCase):
    """协议/实现分离：agent-loop 只依赖协议层，wire 适配器不进协议层。"""

    def test_protocol_has_no_wire_imports(self):
        from miniharness.llm import protocol

        self.assertFalse(hasattr(protocol, "DeepSeekAdapter"))
        self.assertFalse(hasattr(protocol, "FakeLlmAdapter"))
        self.assertFalse(hasattr(protocol, "serialize_messages"))

    def test_fake_implements_protocol(self):
        self.assertEqual(FakeLlmAdapter.provider, "fake")
        self.assertIsInstance(FakeLlmAdapter(), LlmAdapter)

    def test_deepseek_implements_protocol(self):
        self.assertEqual(DeepSeekAdapter.provider, "deepseek-official")
        self.assertIsInstance(DeepSeekAdapter(api_key=""), LlmAdapter)


if __name__ == "__main__":
    unittest.main()