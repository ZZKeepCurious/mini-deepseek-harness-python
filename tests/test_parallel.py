"""阶段 7 验收：真并行工具执行（调度器 + 分类器 + async 管线 + loop async 路径）。"""

import asyncio
import json
import time
import unittest

from miniharness.core.scope import Context
from miniharness.llm import FakeLlmAdapter, LlmAdapter, StreamChunk
from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.agent_loop.tool_calls import (
    DEFAULT_MAX_PARALLEL_TOOL_CALLS,
    TOOL_ABORTED_BEFORE_DISPATCH,
    ParallelBarrier,
    schedule_tool_calls,
)
from miniharness.core.session import Session
from miniharness.core.tools import (
    Tool,
    ToolExec,
    ToolRegistry,
    execution_mode,
    run_pipeline_async,
)


def _env(tools=()):
    session = Session("par")
    ctx = Context()
    reg = ToolRegistry(ctx)
    for t in tools:
        reg.register(t)
    return session, ctx, reg


def _sleeper(name="sleeper", dur=0.15, safe=True, log=None):
    """sleep 工具：记录 [start, end] 时间戳，可注入并发日志。"""
    def execute(args, e):
        tag = f"{name}:{args.get('tag', '')}"
        if log is not None:
            log.append(("start", tag, time.monotonic()))
        time.sleep(args.get("dur", dur))
        if log is not None:
            log.append(("end", tag, time.monotonic()))
        return f"done:{tag}"
    return Tool(name=name, description="sleep",
                parameters={"type": "object", "properties": {"dur": {"type": "number"},
                                                             "tag": {"type": "string"}}},
                execute=execute, is_concurrency_safe=True if safe else False)


def _overlaps(a_start, a_end, b_start, b_end):
    return a_start < b_end and b_start < a_end


def _range(log, tag):
    return (next(ts for s, t, ts in log if s == "start" and t == tag),
            next(ts for s, t, ts in log if s == "end" and t == tag))


class TestExecutionMode(unittest.TestCase):
    def test_undeclared_defaults_exclusive(self):
        t = Tool(name="t", description="d", execute=lambda a, e: 1)
        self.assertEqual(execution_mode(t, {}), "exclusive")

    def test_false_and_true_bool(self):
        self.assertEqual(execution_mode(Tool(name="t", description="d",
                                             execute=lambda a, e: 1,
                                             is_concurrency_safe=False), {}), "exclusive")
        self.assertEqual(execution_mode(Tool(name="t", description="d",
                                             execute=lambda a, e: 1,
                                             is_concurrency_safe=True), {}), "parallel")

    def test_callable_exact_true_only(self):
        t = Tool(name="t", description="d", execute=lambda a, e: 1,
                 is_concurrency_safe=lambda args: args.get("mode") == "read")
        self.assertEqual(execution_mode(t, {"mode": "read"}), "parallel")
        self.assertEqual(execution_mode(t, {"mode": "write"}), "exclusive")

    def test_callable_throwing_or_non_bool_falls_to_exclusive(self):
        def boom(args):
            raise RuntimeError("boom")
        t = Tool(name="t", description="d", execute=lambda a, e: 1,
                 is_concurrency_safe=boom)
        self.assertEqual(execution_mode(t, {}), "exclusive")
        t2 = Tool(name="t2", description="d", execute=lambda a, e: 1,
                  is_concurrency_safe=lambda args: "yes")
        self.assertEqual(execution_mode(t2, {}), "exclusive")

    def test_unknown_tool_exclusive(self):
        self.assertEqual(execution_mode(None, {}), "exclusive")

    def test_mode_not_exposed_to_definitions(self):
        session, ctx, reg = _env([_sleeper(safe=True)])
        loop = AgentLoop(session, FakeLlmAdapter(), reg, ctx)
        defs = loop._tool_definitions()
        self.assertNotIn("isConcurrencySafe", defs[0])


class TestScheduleTools(unittest.TestCase):
    def test_parallel_calls_overlap(self):
        session, ctx, reg = _env([_sleeper("s1", dur=0.2, safe=True),
                                  _sleeper("s2", dur=0.2, safe=True)])
        log = []
        reg.resolve("s1").execute = _sleeper("s1", dur=0.2, safe=True, log=log).execute
        reg.resolve("s2").execute = _sleeper("s2", dur=0.2, safe=True, log=log).execute
        calls = [{"id": "a", "name": "s1", "arguments": "{}"},
                 {"id": "b", "name": "s2", "arguments": "{}"}]
        asyncio.run(schedule_tool_calls(session, ctx, reg, 1, 1, calls, ToolExec()))
        a = _range(log, "s1:")
        b = _range(log, "s2:")
        self.assertTrue(_overlaps(a[0], a[1], b[0], b[1]), "并行工具必须重叠执行")

    def test_results_committed_in_model_order(self):
        session, ctx, reg = _env([_sleeper("slow", dur=0.3, safe=True),
                                  _sleeper("fast", dur=0.05, safe=True)])
        calls = [{"id": "a", "name": "slow", "arguments": "{}"},
                 {"id": "b", "name": "fast", "arguments": "{}"}]
        asyncio.run(schedule_tool_calls(session, ctx, reg, 1, 1, calls, ToolExec()))
        results = [e["data"]["message"]["source"]["callId"]
                   for e in session.events if e["type"] == "tool/result"]
        # fast 先完成，但 tool/result 必须按模型序 [slow, fast]
        self.assertEqual(results, ["a", "b"])
        for e in session.events:
            if e["type"] == "tool/result":
                self.assertIn("sourceEventSeqs", e)

    def test_exclusive_barrier_three_groups(self):
        session, ctx, reg = _env([_sleeper("p1", dur=0.15, safe=True),
                                  _sleeper("ex", dur=0.15, safe=False),
                                  _sleeper("p2", dur=0.15, safe=True)])
        log = []
        for name in ("p1", "ex", "p2"):
            reg.resolve(name).execute = _sleeper(name, dur=0.15, safe=True, log=log).execute
        calls = [{"id": "a", "name": "p1", "arguments": "{}"},
                 {"id": "b", "name": "ex", "arguments": "{}"},
                 {"id": "c", "name": "p2", "arguments": "{}"}]
        asyncio.run(schedule_tool_calls(session, ctx, reg, 1, 1, calls, ToolExec()))
        ex = _range(log, "ex:")
        p2 = _range(log, "p2:")
        p1 = _range(log, "p1:")
        self.assertFalse(_overlaps(p1[0], p1[1], ex[0], ex[1]))
        self.assertFalse(_overlaps(ex[0], ex[1], p2[0], p2[1]))

    def test_bounded_rolling_pool(self):
        session, ctx, reg = _env([])
        state = {"active": 0, "peak": 0}
        for i in range(5):
            name = "t%d" % i
            def make(name=name):
                def execute(args, e):
                    state["active"] += 1
                    state["peak"] = max(state["peak"], state["active"])
                    time.sleep(0.12)
                    state["active"] -= 1
                    return "ok"
                return Tool(name=name, description="d", execute=execute,
                            is_concurrency_safe=True)
            reg.register(make())
        calls = [{"id": "c%d" % i, "name": "t%d" % i, "arguments": "{}"} for i in range(5)]
        asyncio.run(schedule_tool_calls(session, ctx, reg, 1, 1, calls, ToolExec(),
                                        max_parallel=2))
        self.assertEqual(state["peak"], 2, "滚动池峰值必须受 max_parallel 限制")

    def test_reclassification_creates_barrier(self):
        session, ctx, reg = _env([])
        state = {"calls": 0}
        def safe(args):
            state["calls"] += 1
            return state["calls"] == 1   # 组分类时 True，补池时 False → 重分类
        t = Tool(name="flaky", description="d",
                 execute=lambda a, e: time.sleep(0.1) or "ok",
                 is_concurrency_safe=safe)
        reg.register(t)
        log = []
        t.execute = _sleeper("flaky", dur=0.1, safe=True, log=log).execute
        calls = [{"id": "a", "name": "flaky", "arguments": "{}"},
                 {"id": "b", "name": "flaky", "arguments": "{}"}]
        asyncio.run(schedule_tool_calls(session, ctx, reg, 1, 1, calls, ToolExec()))
        a = _range(log, "flaky:")
        b = _range(log, "flaky:")
        # 两次运行 tag 相同，无法区分 —— 改用执行序数
        starts = [ts for s, t, ts in log if s == "start"]
        ends = [ts for s, t, ts in log if s == "end"]
        self.assertFalse(_overlaps(starts[0], ends[0], starts[1], ends[1]),
                         "重分类后第二个调用必须等第一个排空")

    def test_aborted_before_start_all_synthetic(self):
        session, ctx, reg = _env([_sleeper("t", dur=0.05, safe=True)])
        signal = ToolExec()
        signal.signal.set()   # 派发前已中止
        calls = [{"id": "a", "name": "t", "arguments": "{}"},
                 {"id": "b", "name": "t", "arguments": "{}"}]
        aborted = asyncio.run(schedule_tool_calls(session, ctx, reg, 1, 1, calls, signal))
        self.assertTrue(aborted[1])
        for e in session.events:
            if e["type"] == "tool/result":
                self.assertEqual(e["data"]["error"]["code"], TOOL_ABORTED_BEFORE_DISPATCH)

    def test_abort_mid_flight_drains_and_fills_synthetic(self):
        session, ctx, reg = _env([_sleeper("slow", dur=0.3, safe=True),
                                  _sleeper("fast", dur=0.3, safe=True),
                                  _sleeper("pending", dur=0.3, safe=True)])
        signal = ToolExec()

        async def driver():
            task = asyncio.create_task(
                schedule_tool_calls(session, ctx, reg, 1, 1,
                                    [{"id": "a", "name": "slow", "arguments": "{}"},
                                     {"id": "b", "name": "fast", "arguments": "{}"},
                                     {"id": "c", "name": "pending", "arguments": "{}"}],
                                    signal, max_parallel=2))
            await asyncio.sleep(0.05)
            signal.signal.set()
            return await task

        aborted = asyncio.run(driver())
        self.assertTrue(aborted[1])
        results = [e["data"]["message"]["source"]["callId"]
                   for e in session.events if e["type"] == "tool/result"]
        errors = [e["data"]["error"]["code"] for e in session.events
                  if e["type"] == "tool/result" and "error" in e["data"]]
        # 池内已启动的两个排干（真实结果），池外未启动的补合成错误
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0], TOOL_ABORTED_BEFORE_DISPATCH)
        self.assertEqual(len(results), 3)
        # 合成结果也按模型序
        self.assertEqual(results, ["a", "b", "c"])

    def test_scheduler_failure_preserves_calls_without_fabricated_results(self):
        session, ctx, reg = _env([_sleeper("t", dur=0.05, safe=True)])
        ctx.on("tools/pre-execute", lambda p, nxt: (_ for _ in ()).throw(RuntimeError("policy boom")))
        calls = [{"id": "a", "name": "t", "arguments": "{}"}]
        with self.assertRaises(RuntimeError):
            asyncio.run(schedule_tool_calls(session, ctx, reg, 1, 1, calls, ToolExec()))
        types = [e["type"] for e in session.events]
        self.assertIn("tool/call", types)
        self.assertNotIn("tool/result", types)   # 不编造结果

    def test_unknown_tool_in_parallel_group(self):
        session, ctx, reg = _env([_sleeper("t", dur=0.05, safe=True)])
        calls = [{"id": "a", "name": "t", "arguments": "{}"},
                 {"id": "b", "name": "nope", "arguments": "{}"}]
        asyncio.run(schedule_tool_calls(session, ctx, reg, 1, 1, calls, ToolExec()))
        results = [e["data"]["message"]["source"]["callId"]
                   for e in session.events if e["type"] == "tool/result"]
        self.assertEqual(results, ["a", "b"])
        err = [e for e in session.events if e["type"] == "tool/result" and "error" in e["data"]][0]
        self.assertEqual(err["data"]["message"]["content"][0]["isError"], True)

    def test_timeout_isolated_does_not_set_shared_signal_and_drains(self):
        session, ctx, reg = _env([])
        t = Tool(name="hang", description="d",
                 execute=lambda a, e: time.sleep(0.3) or "late",
                 is_concurrency_safe=True, timeout_ms=50)
        reg.register(t)
        signal = ToolExec()
        asyncio.run(schedule_tool_calls(session, ctx, reg, 1, 1,
                                        [{"id": "a", "name": "hang", "arguments": "{}"}],
                                        signal))
        # fuseToolSignals 隔离：单工具超时只中断该工具（熔合信号），
        # 不置位调用方共享 step 信号——并行组内其它工具不受传染
        self.assertFalse(signal.signal.is_set())
        err = [e for e in session.events if e["type"] == "tool/result"][0]
        self.assertTrue(err["data"]["message"]["content"][0]["isError"])
        self.assertIn("timeout", err["data"]["message"]["content"][0]["content"][0]["text"])


class TestPipelineAsync(unittest.TestCase):
    def test_deny_policy_short_circuits_body(self):
        session, ctx, reg = _env([])
        ran = {"hit": False}
        def execute(args, e):
            ran["hit"] = True
            return "never"
        t = Tool(name="t", description="d", execute=execute, is_concurrency_safe=True)
        reg.register(t)
        ctx.on("tools/pre-execute", lambda p, nxt: {"kind": "deny"})
        result = asyncio.run(run_pipeline_async(ctx, t, {"x": 1}))
        self.assertTrue(result.is_error)
        self.assertFalse(ran["hit"])

    def test_normal_roundtrip(self):
        session, ctx, reg = _env([])
        t = Tool(name="t", description="d", execute=lambda a, e: {"ok": a.get("v")})
        reg.register(t)
        result = asyncio.run(run_pipeline_async(ctx, t, {"v": 7}))
        self.assertTrue(result.ok)
        self.assertEqual(result.content, {"ok": 7})

    def test_timeout_and_drain(self):
        session, ctx, reg = _env([])
        t = Tool(name="hang", description="d",
                 execute=lambda a, e: time.sleep(0.2) or "done", timeout_ms=40)
        reg.register(t)
        result = asyncio.run(run_pipeline_async(ctx, t, {}))
        self.assertTrue(result.is_error)
        self.assertIn("timeout", result.error)


class MultiToolAdapter(LlmAdapter):
    """第一轮返回多个 tool-call，之后返回最终文本。"""

    provider = "fake"

    def __init__(self, tool_calls, final_text="搞定。"):
        self._calls = list(tool_calls)
        self._text = final_text
        self.calls = 0

    def stream(self, messages, tools):
        self.calls += 1
        if self._calls and self.calls == 1:
            for i, tc in enumerate(self._calls):
                args = json.dumps(tc.get("arguments", {}), ensure_ascii=False)
                yield StreamChunk("block-start", index=i, blockType="tool-call")
                yield StreamChunk("tool-call-delta", index=i, id="call_%d" % i,
                                  name=tc["name"], argumentsDelta=args)
                yield StreamChunk("block-end", index=i, block={
                    "type": "tool-call", "id": "call_%d" % i, "name": tc["name"],
                    "arguments": args,
                })
            yield StreamChunk("finish", reason={"kind": "tool-calls"})
        else:
            yield StreamChunk("block-start", index=0, blockType="text")
            yield StreamChunk("text-delta", index=0, text=self._text)
            yield StreamChunk("block-end", index=0, block={"type": "text", "text": self._text})
            yield StreamChunk("finish", reason={"kind": "stop"})


class TestLoopAsync(unittest.TestCase):
    def _loop(self, adapter, tools=()):
        session = Session("async")
        ctx = Context()
        reg = ToolRegistry(ctx)
        for t in tools:
            reg.register(t)
        loop = AgentLoop(session, adapter, reg, ctx)
        return session, loop

    def test_run_async_single_turn_equivalent_to_sync(self):
        session, loop = self._loop(FakeLlmAdapter(final_text="好了"))
        text = asyncio.run(loop.run_async("你好"))
        self.assertEqual(text, "好了")
        self.assertEqual(loop.status, "idle")
        # inbox 入队先落 durable agent/inbox/spliced，turn/start 随认领后开
        self.assertEqual([e["type"] for e in session.events][0], "agent/inbox/spliced")
        self.assertEqual([e["type"] for e in session.events][-1], "turn/end")

    def test_run_async_parallel_tool_roundtrip(self):
        session, loop = self._loop(
            MultiToolAdapter([{"name": "s1", "arguments": {}},
                              {"name": "s2", "arguments": {}}]),
            tools=[_sleeper("s1", dur=0.1, safe=True), _sleeper("s2", dur=0.1, safe=True)])
        text = asyncio.run(loop.run_async("并行干活"))
        self.assertEqual(text, "搞定。")
        calls = [e["data"]["name"] for e in session.events if e["type"] == "tool/call"]
        self.assertEqual(calls, ["s1", "s2"])
        # 同 turn 二次请求（看到并行结果后给最终答案）
        self.assertEqual(loop.adapter.calls, 2)
        # 回合括号平衡
        self.assertEqual([e["type"] for e in session.events].count("step/start"),
                         [e["type"] for e in session.events].count("step/end"))

    def test_cancel_during_parallel_dispatch(self):
        session, loop = self._loop(
            MultiToolAdapter([{"name": "s1", "arguments": {}},
                              {"name": "s2", "arguments": {}},
                              {"name": "s3", "arguments": {}},
                              {"name": "s4", "arguments": {}}]),
            tools=[_sleeper("s1", dur=0.3, safe=True),
                   _sleeper("s2", dur=0.3, safe=True),
                   _sleeper("s3", dur=0.3, safe=True),
                   _sleeper("s4", dur=0.3, safe=True)])
        loop.max_parallel_tool_calls = 2   # 池内 2 个在飞，池外 2 个未启动

        async def driver():
            task = asyncio.create_task(loop.run_async("干活"))
            await asyncio.sleep(0.08)
            loop.cancel()          # 并行在飞时取消
            await task

        asyncio.run(driver())
        self.assertEqual(session.events[-1]["data"]["reason"], {"kind": "aborted", "reason": {"kind": "user"}})
        results = [e["data"]["message"]["source"]["callId"]
                   for e in session.events if e["type"] == "tool/result"]
        errors = [e["data"]["error"]["code"] for e in session.events
                  if e["type"] == "tool/result" and "error" in e["data"]]
        # 池内已启动的排干（真实结果），池外未启动的补合成错误
        self.assertEqual(len(results), 4)
        self.assertEqual(len(errors), 2)
        for code in errors:
            self.assertEqual(code, TOOL_ABORTED_BEFORE_DISPATCH)

    def test_run_async_rejects_pre_step(self):
        session, loop = self._loop(FakeLlmAdapter(final_text="x"))
        loop.ctx.on("agent/pre-step", lambda p, nxt: {"kind": "reject"})
        asyncio.run(loop.run_async("危险"))
        self.assertEqual(session.events[-1]["data"]["reason"], {"kind": "blocked"})

    def test_run_async_max_steps_guard(self):
        class InfiniteToolAdapter(LlmAdapter):
            provider = "fake"

            def stream(self, messages, tools):
                yield StreamChunk("block-start", index=0, blockType="tool-call")
                yield StreamChunk("tool-call-delta", index=0, id="call_0",
                                  name="s1", argumentsDelta="{}")
                yield StreamChunk("block-end", index=0, block={
                    "type": "tool-call", "id": "call_0", "name": "s1",
                    "arguments": "{}"})
                yield StreamChunk("finish", reason={"kind": "tool-calls"})

        session, loop = self._loop(InfiniteToolAdapter(),
                                   tools=[_sleeper("s1", dur=0.01, safe=True)])
        loop.max_steps = 2
        with self.assertRaises(RuntimeError):
            asyncio.run(loop.run_async("循环"))
        self.assertEqual(session.events[-1]["data"]["reason"]["kind"], "error")


class TestParallelBarrier(unittest.TestCase):
    def test_default_cap(self):
        self.assertEqual(ParallelBarrier().max_parallel, DEFAULT_MAX_PARALLEL_TOOL_CALLS)

    def test_cap_one_is_serial(self):
        self.assertEqual(ParallelBarrier(max_parallel=1).max_parallel, 1)


if __name__ == "__main__":
    unittest.main()