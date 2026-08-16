"""headless 入口测试：一次性任务语义（上游 bundle/headless 契约）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from miniharness.core.scope import Context
from miniharness.cli.headless import run_headless, summarize
from miniharness.llm import FakeLlmAdapter, LlmFailure
from miniharness.core.session.persistence import JsonlPersistence
from miniharness.core.session import Session, create_message, text_block, tool_call_block
from miniharness.core.tools import Tool, ToolRegistry


class _IO:
    """上游 HeadlessIo 的 write(chunk) 形态：stdout/stderr 是对象，exit 是函数。"""

    def __init__(self):
        self.stdout = []
        self.stderr = []
        self.exit_codes = []

    class _Sink:
        def __init__(self, target):
            self._target = target

        def write(self, chunk):
            self._target.append(chunk)

    @property
    def out(self):
        return self._Sink(self.stdout)

    @property
    def err(self):
        return self._Sink(self.stderr)

    def exit(self, code):
        self.exit_codes.append(code)


def _tools(ctx):
    reg = ToolRegistry(ctx)
    reg.register(Tool(
        name="bash",
        description="Run a shell command.",
        parameters={
            "type": "object",
            "properties": {"cmd": {"type": "string", "description": "command to run"}},
            "required": ["cmd"],
        },
        execute=lambda args, e: f"stdout: {args['cmd']}",
    ))
    return reg


class TestSummarize(unittest.TestCase):
    def test_keeps_last_nonempty_text(self):
        events = [
            {"seq": 0, "time": 1, "type": "turn/start", "data": {"turn": 1}},
            {"seq": 1, "time": 2, "type": "assistant/message", "data": {"message": {
                "id": "m1", "role": "assistant", "source": {"kind": "model"},
                "content": [{"type": "text", "text": "第一版回答"}],
            }}},
            {"seq": 2, "time": 3, "type": "assistant/message", "data": {"message": {
                "id": "m2", "role": "assistant", "source": {"kind": "model"},
                "content": [{"type": "text", "text": ""}],
            }}},
            {"seq": 3, "time": 4, "type": "assistant/message", "data": {"message": {
                "id": "m3", "role": "assistant", "source": {"kind": "model"},
                "content": [{"type": "text", "text": "最终回答"}],
            }}},
            {"seq": 4, "time": 5, "type": "turn/end",
             "data": {"turn": 1, "reason": {"kind": "completed"}}},
        ]
        text, reason = summarize(events, 0)
        # 只取 text 块、空文本不覆盖、最后一条非空胜出（上游 summarize 语义）
        self.assertEqual(text, "最终回答")
        self.assertEqual(reason, {"kind": "completed"})

    def test_ignores_events_before_first_seq(self):
        events = [
            {"seq": 0, "time": 1, "type": "turn/start", "data": {"turn": 1}},
            {"seq": 1, "time": 2, "type": "assistant/message", "data": {"message": {
                "id": "m1", "role": "assistant", "source": {"kind": "model"},
                "content": [{"type": "text", "text": "不该算"}],
            }}},
            {"seq": 2, "time": 3, "type": "turn/end",
             "data": {"turn": 1, "reason": {"kind": "completed"}}},
        ]
        text, reason = summarize(events, 3)
        # runner 持有区间之外的事件（会话打开前已存在的历史）不影响输出
        self.assertEqual(text, "")
        self.assertEqual(reason, None)


class TestHeadlessRun(unittest.TestCase):
    def test_success_prints_final_text_exit_zero(self):
        io = _IO()
        ctx = Context(name="headless")
        run_headless(
            "帮我看看目录",
            adapter=FakeLlmAdapter(final_text="任务完成。"),
            tools=_tools(ctx), ctx=ctx,
            stdout=io.out, stderr=io.err, exit_fn=io.exit,
        )
        self.assertEqual(io.stdout, ["任务完成。\n"])
        self.assertEqual(io.stderr, [])
        self.assertEqual(io.exit_codes, [0])

    def test_tool_roundtrip_prints_final_text(self):
        io = _IO()
        ctx = Context(name="headless")
        run_headless(
            "列出目录",
            adapter=FakeLlmAdapter(
                tool_call={"name": "bash", "arguments": {"cmd": "ls"}},
                final_text="执行完毕。",
            ),
            tools=_tools(ctx), ctx=ctx,
            stdout=io.out, stderr=io.err, exit_fn=io.exit,
        )
        self.assertEqual(io.stdout, ["执行完毕。\n"])
        self.assertEqual(io.exit_codes, [0])

    def test_error_reason_writes_stderr_exit_one(self):
        class BoomAdapter(FakeLlmAdapter):
            def stream(self, messages, tools):
                raise LlmFailure("RATE_LIMIT", "429 Too Many Requests")

        io = _IO()
        ctx = Context(name="headless")
        run_headless(
            "你好",
            adapter=BoomAdapter(),
            tools=_tools(ctx), ctx=ctx,
            stdout=io.out, stderr=io.err, exit_fn=io.exit,
        )
        # 上游 run().catch(fail)：意外失败 stderr 一行（仅 message，不写 code），退出 1；
        # turn/end {kind:'error'} 已由 loop 的 finally 落盘
        self.assertEqual(io.stderr, ["dsh: 429 Too Many Requests\n"])
        self.assertEqual(io.exit_codes, [1])

    def test_blocked_reason_exit_one(self):
        io = _IO()
        ctx = Context(name="headless")

        def reject(payload, next=None):
            return {"verdict": "reject"}

        ctx.on("agent/pre-step", reject)
        run_headless(
            "被拒绝的输入",
            adapter=FakeLlmAdapter(final_text="不会输出"),
            tools=_tools(ctx), ctx=ctx,
            stdout=io.out, stderr=io.err, exit_fn=io.exit,
        )
        # pre-step 拒绝 → {kind:'blocked'} → 退出 1；无 assistant 文本
        self.assertEqual(io.exit_codes, [1])
        self.assertEqual(io.stdout, ["\n"])

    def test_persists_before_exit(self):
        io = _IO()
        ctx = Context(name="headless")
        with tempfile.TemporaryDirectory() as tmp:
            pers = JsonlPersistence(Path(tmp))
            run_headless(
                "持久化验证",
                adapter=FakeLlmAdapter(final_text="写盘完成。"),
                tools=_tools(ctx), ctx=ctx,
                persistence=pers,
                stdout=io.out, stderr=io.err, exit_fn=io.exit,
            )
            files = list(Path(tmp).glob("*.jsonl"))
            self.assertEqual(len(files), 1)
            events = pers.load(files[0].stem)
            # flush 在输出与退出之前：turn/end 已落盘（上游先 flush 再汇总）
            self.assertEqual(events[-1]["type"], "turn/end")

    def test_empty_task_rejected(self):
        with self.assertRaises(ValueError):
            run_headless("   ", adapter=FakeLlmAdapter(final_text="x"))

    def test_last_response_matches_summarize(self):
        # 多文本时 stdout 应与 AgentLoop.last_response 一致（同一投影语义）
        io = _IO()
        ctx = Context(name="headless")
        run_headless(
            "x", adapter=FakeLlmAdapter(final_text="仅此一条"),
            tools=_tools(ctx), ctx=ctx,
            stdout=io.out, stderr=io.err, exit_fn=io.exit,
        )
        self.assertEqual(io.stdout, ["仅此一条\n"])


if __name__ == "__main__":
    unittest.main()
