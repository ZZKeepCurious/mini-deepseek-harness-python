"""第 4 章验收：Agent Loop 状态机 + LLM 流式。运行：python -m unittest discover -s tests -t ."""

import unittest

from miniharness.bus import Context
from miniharness.llm import FakeLlmAdapter, StreamChunk
from miniharness.loop import AgentLoop
from miniharness.session import Session, derive_messages, turn_balance
from miniharness.tools import Tool, ToolRegistry


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
        self.assertEqual(types[0], "turn/start")
        self.assertEqual(types[-1], "turn/end")
        self.assertIn("step/start", types)
        self.assertIn("step/end", types)

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
        # 模型历史包含工具结果消息
        roles = [m["role"] for m in derive_messages(session.events)]
        self.assertIn("tool", roles)

    def test_rejected_pre_step_zero_step_turn(self):
        session, loop, _ = _make_env()
        loop.ctx.on("agent/pre-step", lambda p, nxt: {"verdict": "reject"})
        loop.followup("危险操作")
        types = [e["type"] for e in session.events]
        self.assertIn("turn/start", types)
        self.assertIn("turn/end", types)
        self.assertNotIn("step/start", types)
        self.assertEqual(loop.status, "idle")

    def test_unknown_tool_produces_error_result(self):
        session, loop, _ = _make_env(tool_call={"name": "nope", "arguments": {}})
        loop.followup("调用一个不存在的工具")
        last = [e for e in session.events if e["type"] == "tool/result"][-1]
        self.assertTrue(last["isError"])

    def test_multiple_user_messages_one_turn(self):
        session, loop, _ = _make_env()
        loop.followup("第一句")
        loop.followup("第二句")
        self.assertEqual(turn_balance(session.events), 0)
        # 一个 turn 里有两个 step（两次模型请求）
        self.assertEqual(
            [e["type"] for e in session.events].count("step/start"), 2
        )

    def test_stream_chunk_protocol_invariants(self):
        adapter = FakeLlmAdapter(tool_call={"name": "bash", "arguments": {}})
        chunks = list(adapter.stream([], []))
        kinds = [c["kind"] for c in chunks]
        self.assertIn("finish", kinds)
        self.assertEqual(kinds[-1], "finish")
        for c in chunks:
            self.assertIsInstance(c, StreamChunk)
        self.assertIn("tool-call-delta", kinds)

    def test_max_steps_guard(self):
        # 模型永远调用工具 → 死循环守卫

        class AlwaysToolAdapter(FakeLlmAdapter):
            def stream(self, messages, tools):
                yield StreamChunk("block-start", index=0, block_kind="assistant")
                yield StreamChunk("tool-call-delta", index=0, name="loop", arguments={})
                yield StreamChunk("block-end", index=0, block={"role": "assistant"})
                yield StreamChunk("finish", finish_reason="tool_calls")

        session = Session("s1")
        ctx = Context()
        reg = ToolRegistry(ctx)
        reg.register(Tool(name="loop", description="d", execute=lambda a, e: "again"))
        loop = AgentLoop(session, AlwaysToolAdapter(), reg, ctx, max_steps=5)
        with self.assertRaises(RuntimeError):
            loop.followup("开始")


if __name__ == "__main__":
    unittest.main()