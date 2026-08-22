"""第 4 章验收：Agent Loop 状态机 + LLM 流式。运行：python -m unittest discover -s tests -t ."""

import asyncio
import unittest

from miniharness.core.scope import Context
from miniharness.llm import FakeLlmAdapter, LlmFailure, StreamChunk
from miniharness.llm.protocol import StreamAborted, _aiter_raced
from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.session import Session, derive_messages, turn_balance
from miniharness.core.tools import Tool, ToolRegistry


async def _collect(stream):
    """async 迭代器收集（适配器 stream 已 async 化）。"""
    return [c async for c in stream]


def _make_env(tool_call=None, final="搞定。", extra=None):
    session = Session("s1")
    ctx = Context()
    reg = ToolRegistry(ctx)
    bash = Tool(
        name="bash",
        description="Run a shell command.",
        parameters={"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
        execute=lambda args, e: f"stdout: {args.get('cmd')}",
    )
    reg.register(bash)
    for t in (extra or []):
        reg.register(t)
    adapter = FakeLlmAdapter(tool_call=tool_call, final_text=final)
    loop = AgentLoop(session, adapter, reg, ctx)
    return session, loop, adapter


class TestLoop(unittest.TestCase):
    def test_single_turn_text(self):
        session, loop, _ = _make_env()
        loop.followup("你好")
        self.assertEqual(loop.status, "idle")
        self.assertEqual(turn_balance(session.events), 0)
        self.assertEqual(loop.last_response(), "搞定。")

    def test_turn_is_durable_balanced(self):
        session, loop, _ = _make_env()
        loop.followup("你好")
        types = [e["type"] for e in session.events]
        # inbox 入队先落 durable agent/inbox/spliced，turn/start 随认领后开
        self.assertEqual(types[0], "agent/inbox/spliced")
        self.assertEqual(types[-1], "turn/end")
        self.assertIn("step/start", types)
        self.assertIn("step/end", types)
        # 模型可见 ⟺ 已记录：chunk 与请求配置都落日志
        self.assertIn("assistant/chunk", types)
        self.assertIn("request/header", types)
        self.assertEqual(session.events[-1]["data"]["reason"], {"kind": "completed"})

    def test_assistant_message_cites_chunk_seqs(self):
        session, loop, _ = _make_env()
        loop.followup("你好")
        am = [e for e in session.events if e["type"] == "assistant/message"][0]
        chunk_seqs = [e["seq"] for e in session.events if e["type"] == "assistant/chunk"]
        self.assertEqual(am["sourceEventSeqs"], tuple(chunk_seqs))

    def test_tool_call_roundtrip(self):
        session, loop, adapter = _make_env(
            tool_call={"name": "bash", "arguments": {"cmd": "ls"}}
        )
        loop.followup("帮我看看目录")
        types = [e["type"] for e in session.events]
        self.assertIn("tool/call", types)
        self.assertIn("tool/result", types)
        # 同 turn 内模型再次被请求（看到工具结果后给出最终回答）
        self.assertEqual(adapter.calls, 2)
        self.assertEqual(loop.last_response(), "搞定。")
        # 模型历史：assistant 消息带 tool-call 块，工具结果以 role 'user' 的
        # tool-result 块回灌（上游 ToolResultMessage 模型）
        msgs = derive_messages(session.events)
        assistant = [m for m in msgs if m["role"] == "assistant"][0]
        self.assertTrue(any(b["type"] == "tool-call" for b in assistant["content"]))
        tool_msgs = [m for m in msgs if any(b["type"] == "tool-result" for b in m["content"])]
        self.assertEqual(len(tool_msgs), 1)
        self.assertEqual(tool_msgs[0]["role"], "user")
        self.assertEqual(tool_msgs[0]["content"][0]["toolCallId"], "call_0")

    def test_rejected_pre_step_blocked_turn(self):
        session, loop, _ = _make_env()
        loop.ctx.on("agent/pre-step", lambda p, nxt: {"kind": "reject"})
        loop.followup("危险操作")
        types = [e["type"] for e in session.events]
        self.assertIn("turn/start", types)
        self.assertIn("turn/end", types)
        self.assertNotIn("step/start", types)
        # 上游：pre-step 拒绝 → turn 以 {kind:'blocked'} 结束（agent.ts）
        self.assertEqual(session.events[-1]["data"]["reason"], {"kind": "blocked"})
        self.assertEqual(loop.status, "idle")

    def test_reject_after_tool_call_resets_continue(self):
        """pre-step 拒绝必须复位 _continue：先前有工具调用（_continue=True）
        时拒绝即终局，泵循环不得再跑无输入 step（上游 agent.ts:267-269）。"""
        session, loop, adapter = _make_env(
            tool_call={"name": "bash", "arguments": {"cmd": "ls"}}
        )

        def reject_when(p, nxt):
            msgs = p.get("messages") or []
            if msgs and msgs[0]["content"][0]["text"] == "危险操作":
                return {"kind": "reject"}
            return nxt()

        loop.ctx.on("agent/pre-step", reject_when)

        def execute(args, e):
            loop.inject("危险操作")  # 工具执行期间注入 → reject 时 _continue 仍为 True
            return "ok"

        loop.tools.resolve("bash").execute = execute
        loop.followup("先跑个命令")
        # 回合以 blocked 闭合，不再跑无输入 step（修复前会再请求模型一次）
        self.assertEqual(session.events[-1]["data"]["reason"], {"kind": "blocked"})
        self.assertEqual(adapter.calls, 1)
        self.assertEqual(loop.status, "idle")

    def test_unknown_tool_produces_error_result(self):
        session, loop, _ = _make_env(tool_call={"name": "nope", "arguments": {}})
        loop.followup("调用一个不存在的工具")
        calls = [e for e in session.events if e["type"] == "tool/call"]
        # 未知工具同样先落 tool/call 再出 error 结果（上游 appendToolCall 先于派发）
        self.assertEqual([c["data"]["name"] for c in calls], ["nope"])
        last = [e for e in session.events if e["type"] == "tool/result"][-1]
        block = last["data"]["message"]["content"][0]
        self.assertTrue(block["isError"])
        self.assertIn("未知工具", block["content"][0]["text"])

    def test_multiple_followups_multiple_turns(self):
        session, loop, _ = _make_env()
        loop.followup("第一句")
        loop.followup("第二句")
        self.assertEqual(turn_balance(session.events), 0)
        self.assertEqual(
            [e["type"] for e in session.events].count("step/start"), 2
        )

    def test_events_carry_1based_turn_step_numbers(self):
        session, loop, _ = _make_env(tool_call={"name": "bash", "arguments": {"cmd": "ls"}})
        loop.followup("第一句")
        loop.followup("第二句")
        start = [e for e in session.events if e["type"] == "turn/start"]
        # 与上游一致：turn 从 1 起（session/invariant.ts nextTurn: 1）
        self.assertEqual([e["data"]["turn"] for e in start], [1, 2])
        steps = [e for e in session.events if e["type"] == "step/start"]
        # turn1: 带工具的两步；turn2: 一步（每 turn 内 step 重置为 1）
        self.assertEqual([e["data"]["step"] for e in steps], [1, 2, 1])
        # tool/call 的 arguments 是原始 JSON 字符串（与上游字段一致）
        tc = [e for e in session.events if e["type"] == "tool/call"][0]
        self.assertEqual(tc["data"]["callId"], "call_0")
        self.assertIsInstance(tc["data"]["arguments"], str)
        self.assertIn('"cmd"', tc["data"]["arguments"])
        # tool/result 通过 callId 关联
        tr = [e for e in session.events if e["type"] == "tool/result"][0]
        self.assertEqual(tr["data"]["message"]["source"]["callId"], "call_0")

    def test_stream_chunk_protocol_invariants(self):
        adapter = FakeLlmAdapter(tool_call={"name": "bash", "arguments": {}})
        chunks = asyncio.run(_collect(adapter.stream([], [])))
        kinds = [c["type"] for c in chunks]
        self.assertIn("finish", kinds)
        self.assertEqual(kinds[-1], "finish")
        for c in chunks:
            self.assertIsInstance(c, StreamChunk)
        self.assertIn("tool-call-delta", kinds)
        # finish reason 是对象（上游 FinishReasonMap）
        self.assertEqual(chunks[-1]["reason"], {"kind": "tool-calls"})

    def test_adapter_error_closes_turn_in_finally(self):
        class BoomAdapter(FakeLlmAdapter):
            async def stream(self, messages, tools, signal=None):
                raise LlmFailure("RATE_LIMIT", "429 Too Many Requests")
                yield  # pragma: no cover - 使函数成为 async 生成器（首个 __anext__ 即抛）

        session = Session("s1")
        ctx = Context()
        reg = ToolRegistry(ctx)
        loop = AgentLoop(session, BoomAdapter(), reg, ctx)
        with self.assertRaises(LlmFailure):
            loop.followup("你好")
        # 失败回合也闭合：step/end 与 turn/end {kind:'error'} 必定落日志
        types = [e["type"] for e in session.events]
        self.assertEqual(types[-1], "turn/end")
        self.assertEqual(session.events[-1]["data"]["reason"]["kind"], "error")
        self.assertEqual(session.events[-1]["data"]["reason"]["error"]["code"], "RATE_LIMIT")
        self.assertEqual(types.count("step/end"), 1)
        self.assertEqual(turn_balance(session.events), 0)

    def test_max_steps_guard(self):
        # 模型永远调用工具 → 死循环守卫；回合以 error 闭合
        class AlwaysToolAdapter(FakeLlmAdapter):
            async def stream(self, messages, tools, signal=None):
                yield StreamChunk("block-start", index=0, blockType="tool-call")
                yield StreamChunk("tool-call-delta", index=0, id="call_0", name="loop", argumentsDelta="{}")
                yield StreamChunk("block-end", index=0, block={
                    "type": "tool-call", "id": "call_0", "name": "loop", "arguments": "{}",
                })
                yield StreamChunk("finish", reason={"kind": "tool-calls"})

        session = Session("s1")
        ctx = Context()
        reg = ToolRegistry(ctx)
        reg.register(Tool(name="loop", description="d", execute=lambda a, e: "again"))
        loop = AgentLoop(session, AlwaysToolAdapter(), reg, ctx, max_steps=5)
        with self.assertRaises(RuntimeError):
            loop.followup("开始")
        self.assertEqual(session.events[-1]["type"], "turn/end")
        self.assertEqual(turn_balance(session.events), 0)

    def test_max_steps_default_and_env_override(self):
        # 默认解析到 DEFAULT_MAX_STEPS（环境变量未设置时）
        session = Session("s2")
        ctx = Context()
        reg = ToolRegistry(ctx)
        loop_default = AgentLoop(session, FakeLlmAdapter(final_text="ok"), reg, ctx)
        self.assertEqual(loop_default.max_steps, 50)
        # 显式构造参数胜出
        loop_explicit = AgentLoop(session, FakeLlmAdapter(final_text="ok"), reg, ctx, max_steps=7)
        self.assertEqual(loop_explicit.max_steps, 7)
        # 环境变量覆盖默认（不传构造参数时）
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"MINIHARNESS_MAX_STEPS": "123"}):
            loop_env = AgentLoop(Session("s3"), FakeLlmAdapter(final_text="ok"), reg, ctx)
            self.assertEqual(loop_env.max_steps, 123)
        # 显式参数仍优先于环境变量
        with mock.patch.dict(os.environ, {"MINIHARNESS_MAX_STEPS": "123"}):
            loop_both = AgentLoop(Session("s4"), FakeLlmAdapter(final_text="ok"), reg, ctx, max_steps=9)
            self.assertEqual(loop_both.max_steps, 9)


class TestRequestEnvelope(unittest.TestCase):
    """请求信封（request/header + request/context + agent/request waterfall）。

    上游对照：packages/core/agent-loop/src/agent.ts buildRequest +
    packages/core/session/src/request-header.ts canonicalHeader。
    """

    def _header_event(self, session):
        events = [e for e in session.events if e["type"] == "request/header"]
        self.assertEqual(len(events), 1)
        return events[0]["data"]

    def test_header_canonical_shape(self):
        session, loop, _ = _make_env()
        loop.followup("你好")
        data = self._header_event(session)
        self.assertEqual(data["reason"], "initial")
        header = data["header"]
        # canonical 信封：config 必有（provider/model），system/tools 非空才带
        self.assertEqual(header["config"], {"provider": "fake", "model": None})
        self.assertIn("system", header)
        self.assertIn("助手", header["system"])
        self.assertTrue(header["tools"])
        self.assertEqual(header["tools"][0]["name"], "bash")
        # adapter 未显式设 max_tokens → 无 adapterDefaults 字段（canonicalHeader）
        self.assertNotIn("adapterDefaults", header)
        # request/context 在首个请求落一次（provider/model）
        ctx = [e for e in session.events if e["type"] == "request/context"]
        self.assertEqual(ctx[0]["data"], {"provider": "fake", "model": None})

    def test_header_reuse_avoids_change_log(self):
        session, loop, _ = _make_env()
        loop.followup("第一句")
        loop.followup("第二句")
        # 两次请求的 config/system/tools 一致 → 不追加 change（上游 headerEquals）
        self.assertEqual(
            [e["type"] for e in session.events].count("request/header"), 1
        )

    def test_agent_request_waterfall_overrides_model(self):
        session, loop, _ = _make_env()

        def override(cur, nxt):
            return {**cur, "model": "overridden"}

        loop.ctx.on("agent/request", override)
        loop.followup("你好")
        data = self._header_event(session)
        self.assertEqual(data["header"]["config"]["model"], "overridden")
        ctx = [e for e in session.events if e["type"] == "request/context"]
        self.assertEqual(ctx[0]["data"]["model"], "overridden")

    def test_agent_request_without_provider_fails_loud(self):
        session, loop, _ = _make_env()
        loop.ctx.on("agent/request", lambda cur, nxt: {**cur, "provider": None})
        with self.assertRaisesRegex(RuntimeError, "no provider/model"):
            loop.followup("你好")


class AbortRaceTest(unittest.TestCase):
    """asyncio 化重构：_aiter_raced 异步竞速桥契约（保序 / 异常透传 / abort 竞速）。"""

    async def _alines(self, items):
        for item in items:
            yield item

    def test_preserves_order(self):
        async def scenario():
            return [c async for c in _aiter_raced(self._alines(["a", "b", "c"]))]

        self.assertEqual(asyncio.run(scenario()), ["a", "b", "c"])

    def test_propagates_producer_exception(self):
        async def producer():
            yield "a"
            raise ValueError("boom")

        async def scenario():
            return [c async for c in _aiter_raced(producer())]

        with self.assertRaises(ValueError):
            asyncio.run(scenario())

    def test_abort_race_raises_stream_aborted(self):
        # 生产节奏慢于消费者 → 取块间等待竞速（对应真实 SSE chunk 间的网络间隙），
        # abort 置位后下一次取块由竞速判负 → StreamAborted（而非 CancelledError）。
        async def producer():
            for i in range(100):
                await asyncio.sleep(0.05)
                yield i

        async def scenario():
            abort = asyncio.Event()
            collected = []
            async for chunk in _aiter_raced(producer(), abort):
                collected.append(chunk)
                abort.set()  # 首块即触发
            return collected

        with self.assertRaises(StreamAborted):
            asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()