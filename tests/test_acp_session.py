"""第 12 章测试补：ACP 会话生命周期 + 模型配置 + 更新流投影。

覆盖：initialize 的 sessionCapabilities；new_session configOptions 结构；
模型 options 分组 / set 语义与逐字错误文案 / 选择对 request/header 信封的施加
（含 provider 缺省 reasoning 复位）；close → list → resume 生命周期与各拒绝、
恢复路由 selectionFor 回退、turn 编号延续与 resume reason；list_sessions 分页
keyset 游标（canonical 往返 / 畸形拒绝 / 非规范拒绝）；更新流投影
（agent_message_chunk 带 messageId、agent_thought_chunk、tool_call /
tool_call_update 的 completed/failed、projected_seq 跨提示不重放）。
"""
import base64
import json
import os
import unittest

from miniharness.llm import FakeLlmAdapter, StreamChunk
from miniharness.protocol.acp import (
    AcpModelConfigError,
    AcpRequestError,
    AcpServer,
    selection_for,
)

# 双平台均为绝对路径（与 test_acp.py 同约定，CI 在 ubuntu / windows 都跑）
_CWD = os.path.abspath("work")
_CWD2 = os.path.abspath("work-alt")


def _model_val(provider: str, model: str) -> str:
    return json.dumps([provider, model], ensure_ascii=False, separators=(",", ":"))


def _last_header_config(server: AcpServer, session_id: str) -> dict:
    """会话日志里最近一次 request/header 的 config（应用后的模型可见信封）。"""
    last = None
    for ev in server.sessions[session_id]["session"].events:
        if ev["type"] == "request/header":
            last = ev
    assert last is not None, "no request/header logged"
    return last["data"]["header"]["config"]


# ---- 测试适配器 ----

class _CatalogAdapter(FakeLlmAdapter):
    """内置 fake + 多模型目录 + reasoning 目录（无模型缺省 effort）。

    教学扩展：models_catalog / resolve_model_info['reasoning'] 是模型选择目录
    的 mini 载体（上游为 llm 服务 listProviders / listModels）。"""

    model = "fake-model"
    models_catalog = [
        {"provider": "fake", "model": "fake-model", "name": "Fake Model"},
        {"provider": "fake", "model": "fake-model-pro", "name": "Fake Pro",
         "description": "bigger brain"},
        {"provider": "other", "model": "o-1", "name": "Other"},
    ]

    def resolve_model_info(self) -> dict:
        info = dict(super().resolve_model_info())
        info["reasoning"] = {
            "efforts": [
                {"id": "low", "name": "Low"},
                {"id": "high", "name": "High", "description": "extra thinking"},
            ],
        }
        return info


class _DefaultEffortAdapter(FakeLlmAdapter):
    """reasoning 目录声明了模型缺省 effort → provider-default 空值不合法。"""

    model = "fake-model"

    def resolve_model_info(self) -> dict:
        return {
            "provider": self.provider,
            "model": self.model,
            "input_modalities": ["text"],
            "reasoning": {
                "efforts": [{"id": "low", "name": "Low"}],
                "defaultEffort": "low",
            },
        }


class _ThoughtAdapter(FakeLlmAdapter):
    """首回合流式产出 reasoning 块 + 文本块（测试 agent_thought_chunk 投影）。"""

    async def stream(self, messages, tools, signal=None):
        yield StreamChunk("block-start", index=0, blockType="reasoning")
        yield StreamChunk("reasoning-delta", index=0, text="think hard")
        yield StreamChunk("block-end", index=0,
                          block={"type": "reasoning", "text": "think hard"})
        yield StreamChunk("block-start", index=1, blockType="text")
        yield StreamChunk("text-delta", index=1, text="answer")
        yield StreamChunk("block-end", index=1,
                          block={"type": "text", "text": "answer"})
        yield StreamChunk("finish", reason={"kind": "stop"})


class _UsageAdapter(FakeLlmAdapter):
    """带 contextWindow 容量 + 产出 usage chunk 的适配器（测试 usage_update）。"""

    context_window = 64_000

    async def stream(self, messages, tools, signal=None):
        yield StreamChunk("block-start", index=0, blockType="text")
        yield StreamChunk("text-delta", index=0, text="answer")
        yield StreamChunk("block-end", index=0,
                          block={"type": "text", "text": "answer"})
        yield StreamChunk("usage", usage={
            "inputTokens": 10, "outputTokens": 5,
        })
        yield StreamChunk("finish", reason={"kind": "stop"})



def _assert_invalid(cm, needle: str | None = None) -> str:
    """统一断言：code -32602 + 可选子串。"""
    assert isinstance(cm.exception, AcpRequestError), cm.exception
    detail = cm.exception.detail
    assert cm.exception.code == -32602, (cm.exception.code, detail)
    if needle is not None:
        assert needle in detail, detail
    return detail


# ---- 握手与配置 ----

class TestSessionCapabilities(unittest.TestCase):
    def test_initialize_advertises_session_capabilities(self):
        info = AcpServer().initialize()
        caps = info["agentCapabilities"]
        self.assertEqual(caps["sessionCapabilities"],
                         {"close": {}, "list": {}, "resume": {}})
        self.assertNotIn("mcpCapabilities", caps)


class TestModelConfig(unittest.TestCase):
    def setUp(self):
        self.server = AcpServer(adapter=_CatalogAdapter())
        self.session_id = self.server.new_session(_CWD)["sessionId"]

    def _set(self, config_id, value):
        return self.server.set_session_config_option(
            self.session_id, config_id, value)

    def test_new_session_returns_config_options(self):
        result = self.server.new_session(_CWD2)
        options = result["configOptions"]
        self.assertEqual([o["id"] for o in options], ["model", "reasoning_effort"])
        model, reasoning = options
        self.assertEqual(model["currentValue"], _model_val("fake", "fake-model"))
        self.assertEqual([g["group"] for g in model["options"]], ["fake", "other"])
        fake, other = model["options"]
        self.assertEqual(len(fake["options"]), 2)
        self.assertEqual(fake["options"][0]["value"], _model_val("fake", "fake-model"))
        self.assertEqual(other["options"][0]["value"], _model_val("other", "o-1"))
        self.assertEqual(reasoning["currentValue"], "")
        self.assertEqual([o["value"] for o in reasoning["options"]],
                         ["", "low", "high"])

    def test_switch_model_returns_state_and_resets_reasoning(self):
        self._set("reasoning_effort", "high")
        state = self._set("model", _model_val("fake", "fake-model-pro"))
        server_opts = state["configOptions"]
        model = next(o for o in server_opts if o["id"] == "model")
        self.assertEqual(model["currentValue"], _model_val("fake", "fake-model-pro"))
        reasoning = next(o for o in server_opts if o["id"] == "reasoning_effort")
        self.assertEqual(reasoning["currentValue"], "")

    def test_reasoning_applies_to_next_request_header(self):
        self._set("reasoning_effort", "high")
        self.server.prompt(self.session_id, [{"type": "text", "text": "hi"}])
        self.assertEqual(_last_header_config(self.server, self.session_id), {
            "provider": "fake", "model": "fake-model",
            "reasoningEffort": "high",
        })

    def test_provider_default_reasoning_restores_default(self):
        self._set("reasoning_effort", "high")
        self._set("reasoning_effort", "")
        self.server.prompt(self.session_id, [{"type": "text", "text": "hi"}])
        self.assertEqual(_last_header_config(self.server, self.session_id), {
            "provider": "fake", "model": "fake-model",
        })

    def test_switch_model_applies_to_next_header(self):
        self._set("model", _model_val("fake", "fake-model-pro"))
        self.server.prompt(self.session_id, [{"type": "text", "text": "hi"}])
        config = _last_header_config(self.server, self.session_id)
        self.assertEqual(config["provider"], "fake")
        self.assertEqual(config["model"], "fake-model-pro")
        self.assertNotIn("reasoningEffort", config)

    def test_switch_to_catalog_membership_route(self):
        state = self._set("model", _model_val("other", "o-1"))
        server_opts = state["configOptions"]
        model = next(o for o in server_opts if o["id"] == "model")
        self.assertEqual(model["currentValue"], _model_val("other", "o-1"))

    def test_unknown_model_option_rejected(self):
        with self.assertRaises(AcpRequestError) as cm:
            self._set("model", _model_val("fake", "nope"))
        _assert_invalid(cm, "unknown model option")

    def test_unknown_config_id_rejected(self):
        with self.assertRaises(AcpRequestError) as cm:
            self._set("temperature", "1")
        _assert_invalid(cm, "unknown session config option")

    def test_non_string_value_rejected(self):
        with self.assertRaises(AcpRequestError) as cm:
            self._set("model", 3)
        _assert_invalid(cm, "requires a select value")

    def test_unknown_reasoning_effort_rejected(self):
        with self.assertRaises(AcpRequestError) as cm:
            self._set("reasoning_effort", "turbo")
        _assert_invalid(cm, f"unknown reasoning effort for fake/fake-model")

    def test_unknown_session_rejected(self):
        with self.assertRaises(AcpRequestError) as cm:
            self.server.set_session_config_option("nope", "model", "x")
        _assert_invalid(cm, "unknown session")


class TestDefaultEffortReasoning(unittest.TestCase):
    def test_provider_default_invalid_when_model_declares_default(self):
        server = AcpServer(adapter=_DefaultEffortAdapter())
        session_id = server.new_session(_CWD)["sessionId"]
        control = server.sessions[session_id]["model_control"]
        reasoning = control.options()[1]
        self.assertEqual([o["value"] for o in reasoning["options"]], ["low"])
        with self.assertRaises(AcpRequestError) as cm:
            server.set_session_config_option(session_id, "reasoning_effort", "")
        _assert_invalid(cm, "unknown reasoning effort")


class TestModelControlUnit(unittest.TestCase):
    def test_no_selection_error(self):
        from miniharness.protocol.acp import AcpModelControl
        control = AcpModelControl(FakeLlmAdapter(), None)
        self.assertEqual(control.options(), [])
        with self.assertRaises(AcpModelConfigError) as cm:
            control.set("model", "x")
        self.assertEqual(cm.exception.message, "this session has no model selection")

    def test_selection_for_prefers_logged_header(self):
        logged = {"config": {"provider": "fake", "model": "m2",
                             "reasoningEffort": "high"}}
        self.assertEqual(
            selection_for(logged, {"provider": "fake", "model": "m1"}),
            {"provider": "fake", "model": "m2", "reasoningEffort": "high"})

    def test_selection_for_drops_adapter_defaulted_effort(self):
        logged = {"config": {"provider": "fake", "model": "m2",
                             "reasoningEffort": "high"},
                  "adapterDefaults": {"reasoningEffort": True}}
        self.assertEqual(selection_for(logged, None), {"provider": "fake", "model": "m2"})

    def test_selection_for_falls_back(self):
        self.assertIsNone(selection_for(None, None))
        fallback = {"provider": "p", "model": "m"}
        self.assertEqual(selection_for(None, fallback), fallback)


# ---- 生命周期：close / list / resume ----

class TestLifecycle(unittest.TestCase):
    def setUp(self):
        self.server = AcpServer(adapter=_CatalogAdapter())
        self.session_id = self.server.new_session(_CWD)["sessionId"]

    def test_close_archives_and_list_excludes_active(self):
        listed = self.server.list_sessions()
        self.assertEqual(listed, {"sessions": []})  # 活跃会话不可恢复列出
        self.server.close_session(self.session_id)
        listed = self.server.list_sessions()
        self.assertEqual([s["sessionId"] for s in listed["sessions"]],
                         [self.session_id])
        self.assertEqual(listed["sessions"][0]["cwd"], _CWD)

    def test_close_unknown_rejected(self):
        with self.assertRaises(AcpRequestError) as cm:
            self.server.close_session("nope")
        _assert_invalid(cm, "unknown session")

    def test_close_twice_second_unknown(self):
        self.server.close_session(self.session_id)
        with self.assertRaises(AcpRequestError) as cm:
            self.server.close_session(self.session_id)
        _assert_invalid(cm, "unknown session")

    def test_prompt_after_close_rejected(self):
        self.server.close_session(self.session_id)
        with self.assertRaises(AcpRequestError) as cm:
            self.server.prompt(self.session_id, [{"type": "text", "text": "x"}])
        _assert_invalid(cm, "unknown session")

    def test_resume_missing_rejected(self):
        with self.assertRaises(AcpRequestError) as cm:
            self.server.resume_session("nope", _CWD)
        _assert_invalid(cm, "session is not resumable")

    def test_resume_active_rejected(self):
        with self.assertRaises(AcpRequestError) as cm:
            self.server.resume_session(self.session_id, _CWD)
        _assert_invalid(cm, "session is already active")

    def test_resume_after_close_returns_options(self):
        self.server.close_session(self.session_id)
        result = self.server.resume_session(self.session_id, _CWD)
        self.assertEqual([o["id"] for o in result["configOptions"]],
                         ["model", "reasoning_effort"])
        self.assertIn(self.session_id, self.server.sessions)

    def test_resume_cwd_mismatch_rejected(self):
        self.server.close_session(self.session_id)
        with self.assertRaises(AcpRequestError) as cm:
            self.server.resume_session(self.session_id, _CWD2)
        _assert_invalid(cm, "session cwd does not match")

    def test_resume_cwd_relative_rejected(self):
        self.server.close_session(self.session_id)
        with self.assertRaises(AcpRequestError) as cm:
            self.server.resume_session(self.session_id, "work")
        _assert_invalid(cm, "cwd must be an absolute path")

    def test_resume_mcp_servers_rejected(self):
        self.server.close_session(self.session_id)
        with self.assertRaises(AcpRequestError) as cm:
            self.server.resume_session(self.session_id, _CWD,
                                       mcp_servers=[{"name": "s"}])
        _assert_invalid(cm, "mcpServers")


class TestResumeContinuation(unittest.TestCase):
    def setUp(self):
        self.server = AcpServer(adapter=_CatalogAdapter())
        self.session_id = self.server.new_session(_CWD)["sessionId"]
        self.server.prompt(self.session_id, [{"type": "text", "text": "第一轮"}])

    def test_resume_retains_history_and_turn_numbering(self):
        events_before = len(self.server.sessions[self.session_id]["session"].events)
        self.server.close_session(self.session_id)
        result = self.server.resume_session(self.session_id, _CWD)
        model = next(o for o in result["configOptions"] if o["id"] == "model")
        self.assertEqual(model["currentValue"], _model_val("fake", "fake-model"))
        self.server.prompt(self.session_id, [{"type": "text", "text": "第二轮"}])
        events = self.server.sessions[self.session_id]["session"].events
        self.assertGreater(len(events), events_before)
        self.assertEqual(
            [e["data"]["turn"] for e in events if e["type"] == "turn/start"],
            [1, 2])
        headers = [e for e in events if e["type"] == "request/header"]
        self.assertEqual(headers[0]["data"]["reason"], "initial")
        self.assertEqual(headers[1]["data"]["reason"], "resume")

    def test_resume_restores_selected_route(self):
        self.server.set_session_config_option(
            self.session_id, "model", _model_val("fake", "fake-model-pro"))
        self.server.prompt(self.session_id, [{"type": "text", "text": "chooser"}])
        self.server.close_session(self.session_id)
        result = self.server.resume_session(self.session_id, _CWD)
        model = next(o for o in result["configOptions"] if o["id"] == "model")
        self.assertEqual(model["currentValue"], _model_val("fake", "fake-model-pro"))
        self.server.prompt(self.session_id, [{"type": "text", "text": "hi"}])
        config = _last_header_config(self.server, self.session_id)
        self.assertEqual(config["provider"], "fake")
        self.assertEqual(config["model"], "fake-model-pro")


class TestListSessionsPagination(unittest.TestCase):
    def setUp(self):
        self.server = AcpServer()
        self.ids = [self.server.new_session(os.path.abspath(f"pg-{i}"))["sessionId"]
                    for i in range(3)]
        for sid in self.ids:
            self.server.close_session(sid)
        self.expected = [r["session"].session_id for r in sorted(
            self.server.archived.values(),
            key=lambda r: (-r["session"].created_at,
                           r["session"].session_id.encode("utf-8")))]

    def test_single_page_when_fits(self):
        listed = self.server.list_sessions()
        self.assertEqual(sorted(s["sessionId"] for s in listed["sessions"]),
                         sorted(self.ids))
        self.assertNotIn("nextCursor", listed)

    def test_pagination_cursor_roundtrip(self):
        self.server._session_list_page_size = 2
        page1 = self.server.list_sessions()
        self.assertEqual([s["sessionId"] for s in page1["sessions"]],
                         self.expected[:2])
        self.assertIn("nextCursor", page1)
        page2 = self.server.list_sessions(cursor=page1["nextCursor"])
        self.assertEqual([s["sessionId"] for s in page2["sessions"]],
                         self.expected[2:])
        self.assertNotIn("nextCursor", page2)
        # 同一游标反复使用结果一致（无会话进入游标之前新创）
        again = self.server.list_sessions(cursor=page1["nextCursor"])
        self.assertEqual(again["sessions"], page2["sessions"])

    def test_invalid_cursor_rejected(self):
        for bad in ["%%%", "aGVsbG8", "AAAA"]:
            with self.assertRaises(AcpRequestError) as cm:
                self.server.list_sessions(cursor=bad)
            self.assertEqual(cm.exception.code, -32602, bad)
            self.assertIn("cursor is invalid", cm.exception.detail)

    def test_non_canonical_cursor_rejected(self):
        raw = json.dumps([1, "s"], separators=(",", ":")).encode()
        canonical = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        with self.assertRaises(AcpRequestError) as cm:
            self.server.list_sessions(cursor=canonical + "A")
        _assert_invalid(cm, "cursor is invalid")

    def test_relative_cwd_rejected(self):
        with self.assertRaises(AcpRequestError) as cm:
            self.server.list_sessions(cwd="work")
        _assert_invalid(cm, "cwd must be an absolute path")

    def test_cwd_filter(self):
        other = self.server.new_session(_CWD2)["sessionId"]
        self.server.close_session(other)
        listed = self.server.list_sessions(cwd=_CWD2)
        self.assertEqual([s["sessionId"] for s in listed["sessions"]], [other])

    def test_invalid_page_size_fails_loud(self):
        for bad in (0, -3, 1.5):
            with self.assertRaises(ValueError):
                AcpServer(session_list_page_size=bad)


# ---- 更新流投影 ----

class TestSessionUpdates(unittest.TestCase):
    def test_tool_call_and_result_updates(self):
        adapter = FakeLlmAdapter(
            tool_call={"name": "job_list", "arguments": {"detail": False}})
        server = AcpServer(adapter=adapter)
        session_id = server.new_session(_CWD)["sessionId"]
        server.prompt(session_id, [{"type": "text", "text": "看下作业"}])
        kinds = [u["update"]["sessionUpdate"] for u in server.updates]
        self.assertLess(kinds.index("tool_call"), kinds.index("tool_call_update"))
        self.assertIn("agent_message_chunk", kinds)
        call = next(u["update"] for u in server.updates
                    if u["update"]["sessionUpdate"] == "tool_call")
        self.assertEqual(call["toolCallId"], "call_0")
        self.assertEqual(call["title"], "job_list")
        self.assertEqual(call["kind"], "other")
        self.assertEqual(call["status"], "in_progress")
        self.assertEqual(call["rawInput"], {"detail": False})
        done = next(u["update"] for u in server.updates
                    if u["update"]["sessionUpdate"] == "tool_call_update")
        self.assertEqual(done["toolCallId"], "call_0")
        self.assertEqual(done["status"], "completed")
        self.assertTrue(done["content"])
        self.assertEqual(done["content"][0]["type"], "content")

    def test_unknown_tool_update_failed(self):
        adapter = FakeLlmAdapter(
            tool_call={"name": "__no_such_tool__", "arguments": {}})
        server = AcpServer(adapter=adapter)
        session_id = server.new_session(_CWD)["sessionId"]
        server.prompt(session_id, [{"type": "text", "text": "hi"}])
        done = next(u["update"] for u in server.updates
                    if u["update"]["sessionUpdate"] == "tool_call_update")
        self.assertEqual(done["toolCallId"], "call_0")
        self.assertEqual(done["status"], "failed")

    def test_reasoning_projected_as_thought_chunk(self):
        server = AcpServer(adapter=_ThoughtAdapter())
        session_id = server.new_session(_CWD)["sessionId"]
        server.prompt(session_id, [{"type": "text", "text": "hi"}])
        updates = [u["update"] for u in server.updates]
        thoughts = [u for u in updates
                    if u["sessionUpdate"] == "agent_thought_chunk"]
        self.assertEqual(len(thoughts), 1)
        self.assertEqual(thoughts[0]["content"], {"type": "text", "text": "think hard"})
        chunks = [u for u in updates
                  if u["sessionUpdate"] == "agent_message_chunk"]
        self.assertEqual(chunks[0]["content"], {"type": "text", "text": "answer"})
        message_id = next(
            e["data"]["message"]["id"] for e in
            server.sessions[session_id]["session"].events
            if e["type"] == "assistant/message")
        self.assertEqual(thoughts[0]["messageId"], message_id)
        self.assertEqual(chunks[0]["messageId"], message_id)

    def test_message_id_on_text_chunk(self):
        server = AcpServer()
        session_id = server.new_session(_CWD)["sessionId"]
        server.prompt(session_id, [{"type": "text", "text": "hi"}])
        chunk = next(u["update"] for u in server.updates
                     if u["update"]["sessionUpdate"] == "agent_message_chunk")
        message_id = next(
            e["data"]["message"]["id"] for e in
            server.sessions[session_id]["session"].events
            if e["type"] == "assistant/message")
        self.assertEqual(chunk["messageId"], message_id)

    def test_projected_cutoff_does_not_repeat_old_tool_updates(self):
        adapter = FakeLlmAdapter(tool_call={"name": "job_list", "arguments": {}})
        server = AcpServer(adapter=adapter)
        session_id = server.new_session(_CWD)["sessionId"]
        server.prompt(session_id, [{"type": "text", "text": "第一轮"}])
        first_calls = [u for u in server.updates
                       if u["update"]["sessionUpdate"] == "tool_call"]
        self.assertEqual(len(first_calls), 1)
        server.prompt(session_id, [{"type": "text", "text": "第二轮"}])
        all_calls = [u for u in server.updates
                     if u["update"]["sessionUpdate"] == "tool_call"]
        self.assertEqual(len(all_calls), 1)
        chunks = [u for u in server.updates
                  if u["update"]["sessionUpdate"] == "agent_message_chunk"]
        self.assertEqual(len(chunks), 2)

    def test_usage_update_emitted_when_usage_and_capacity(self):
        server = AcpServer(adapter=_UsageAdapter())
        session_id = server.new_session(_CWD)["sessionId"]
        server.prompt(session_id, [{"type": "text", "text": "hi"}])
        usage = next(u["update"] for u in server.updates
                     if u["update"]["sessionUpdate"] == "usage_update")
        self.assertEqual(usage["size"], 64_000)
        self.assertIsInstance(usage["used"], int)
        self.assertGreaterEqual(usage["used"], 0)
        # 优于消息块之后（对齐上游 assistantUpdates：usage 尾随 message chunks）
        kinds = [u["update"]["sessionUpdate"] for u in server.updates]
        chunk = kinds.index("agent_message_chunk")
        self.assertLess(chunk, kinds.index("usage_update"))

    def test_request_context_logs_context_window(self):
        server = AcpServer(adapter=_UsageAdapter())
        session_id = server.new_session(_CWD)["sessionId"]
        server.prompt(session_id, [{"type": "text", "text": "hi"}])
        ctx = server.sessions[session_id]["session"].request_context()
        self.assertEqual(ctx["provider"], "fake")
        self.assertEqual(ctx["contextWindow"], 64_000)

    def test_no_usage_update_without_capacity(self):
        # FakeLlmAdapter 无 context_window → 即便有 usage 也不发射（对齐 usageUpdate）
        server = AcpServer(adapter=FakeLlmAdapter())
        session_id = server.new_session(_CWD)["sessionId"]
        server.prompt(session_id, [{"type": "text", "text": "hi"}])
        kinds = [u["update"]["sessionUpdate"] for u in server.updates]
        self.assertNotIn("usage_update", kinds)


if __name__ == "__main__":
    unittest.main()