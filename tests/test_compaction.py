"""M1：上下文压缩 —— 配置解析、surface 区间选择、压缩事务、引擎触发。

上游对照：packages/compaction/compaction-basic/src/{config,region,summarizer}.ts
+ packages/compaction/compaction/src/{index,tool-pairing}.ts。
"""
import asyncio
import unittest

from miniharness.compaction import (
    PRUNE_MARKER,
    TargetPressureConfigError,
    ToolResultPruner,
    compact_surface_region,
    inspect_compaction_entry_state,
    install_compaction,
    install_tool_result_pruner,
    resolve_config,
    resolve_spec,
    select_compactable_range,
)
from miniharness.compaction.engine import CompactionEngine
from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import (
    KNOWN_TYPES,
    Session,
    create_message,
    derive_messages,
    text_block,
    tool_result_block,
)
from miniharness.core.tools import ToolRegistry
from miniharness.llm import LlmAdapter, LlmFailure
from miniharness.llm.retry import apply_retry_planner
from miniharness.llm.token_meter import TokenMeter


# ---------- 配置解析 ----------

class ResolveConfigTest(unittest.TestCase):
    def test_defaults(self):
        c = resolve_config()
        self.assertEqual(c["thresholdRatio"], 0.8)
        self.assertEqual(c["retainRatio"], 0.16)
        self.assertIsNone(c["retainTokens"])
        self.assertEqual(c["maxTokens"], 8192)
        self.assertEqual(c["compactionRetries"], 1)
        self.assertEqual(c["maxOverflowRetries"], 1)
        self.assertTrue(c["auto"])

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            resolve_config({"nope": 1})

    def test_retain_ratio_tokens_mutually_exclusive(self):
        with self.assertRaises(ValueError):
            resolve_config({"retainRatio": 0.1, "retainTokens": 100})

    def test_retain_ratio_must_be_below_threshold(self):
        with self.assertRaises(ValueError):
            resolve_config({"retainRatio": 0.9})
        with self.assertRaises(ValueError):
            resolve_config({"retainRatio": 0.8})

    def test_bad_types(self):
        with self.assertRaises(ValueError):
            resolve_config({"thresholdRatio": 0})      # 必须 (0,1]
        with self.assertRaises(ValueError):
            resolve_config({"maxTokens": -1})
        with self.assertRaises(ValueError):
            resolve_config({"auto": "yes"})

    def test_explicit_retain_tokens(self):
        c = resolve_config({"retainTokens": 2000})
        self.assertEqual(c["retainTokens"], 2000)
        self.assertIsNone(c["retainRatio"])


class ResolveSpecTest(unittest.TestCase):
    def _policy(self, **over):
        p = resolve_config(over)
        return {**p, "target": "p/m"}

    def test_default_conversion(self):
        spec = resolve_spec(self._policy(), 1000)
        self.assertEqual(spec["thresholdTokens"], 800)
        self.assertEqual(spec["retainTokens"], 160)

    def test_invalid_context_window(self):
        with self.assertRaises(TargetPressureConfigError):
            resolve_spec(self._policy(), 0)
        with self.assertRaises(TargetPressureConfigError):
            resolve_spec(self._policy(), 1.5)

    def test_retain_tokens_above_threshold_rejected(self):
        with self.assertRaises(TargetPressureConfigError):
            resolve_spec(self._policy(retainTokens=900), 1000)

    def test_explicit_retain_tokens(self):
        spec = resolve_spec(self._policy(retainTokens=100), 1000)
        self.assertEqual(spec["retainTokens"], 100)


# ---------- 测试夹具 ----------

def _append(session: Session, type_: str, data: dict, **kwargs) -> dict:
    return session.append(type_, data, **kwargs)


def _seed_history(session: Session, n: int = 2) -> int:
    """写 n 个回合（最后一回合保持打开，模拟运行中的会话）。返回首个 user seq。"""
    session.append("request/header",
                   {"header": {"config": {"provider": "fake", "model": "fake-model"}},
                    "reason": "initial"})
    first_seq = None
    for i in range(n):
        session.append("turn/start", {"turn": i + 1})
        msg = create_message("user", [text_block(f"输入 {i}")], {"kind": "user"})
        ev = session.append("user/message", msg, surfaceOp="append")
        if first_seq is None:
            first_seq = ev["seq"]
        session.append("step/start", {"turn": i + 1, "step": 1})
        assistant = create_message("assistant", [text_block(f"回复 {i}")], {"kind": "model"})
        session.append("assistant/message", {
            "turn": i + 1, "step": 1, "message": assistant,
        }, surfaceOp="append")
        session.append("step/end", {"turn": i + 1, "step": 1})
        if i < n - 1:
            session.append("turn/end", {"turn": i + 1, "reason": {"kind": "completed"}})
    return first_seq


class _SummaryAdapter(LlmAdapter):
    """摘要用假适配器：固定文本输出 + 可配 context_window / 故障。"""

    provider = "fake"
    model = "fake-model"
    retry_policy = None

    def __init__(self, text: str = "压缩摘要内容。", context_window: int | None = 1000,
                 fail_times: int = 0, fail_code: str = "CONTEXT_WINDOW_EXCEEDED"):
        self.text = text
        self.context_window = context_window
        self.fail_times = fail_times
        self.fail_code = fail_code
        self.calls = 0

    async def stream(self, messages, tools, signal=None):
        self.calls += 1
        if self.fail_times > 0:
            self.fail_times -= 1
            raise LlmFailure(self.fail_code, f"HTTP 400: {self.fail_code}")
        yield {"type": "block-start", "index": 0, "blockType": "text"}
        yield {"type": "text-delta", "index": 0, "text": self.text}
        yield {"type": "block-end", "index": 0, "block": {"type": "text", "text": self.text}}
        yield {"type": "finish", "reason": {"kind": "stop"}}


def _agent(session: Session, adapter: LlmAdapter, ctx: Context | None = None) -> AgentLoop:
    ctx = ctx or Context(name="compaction-test")
    return AgentLoop(session, adapter, ToolRegistry(ctx), ctx)


# ---------- surface 区间与压缩事务 ----------

class CompactableRangeTest(unittest.TestCase):
    def setUp(self):
        self.session = Session("c1")
        self.meter = TokenMeter()

    def test_empty_surface(self):
        self.assertIsNone(select_compactable_range(self.session,
                                                   self.meter.measure(self.session), 0))

    def test_all_retained_returns_none(self):
        _seed_history(self.session)
        m = self.meter.measure(self.session)
        # retainTokens 覆盖整个 surface → keep_from == 0 → None
        self.assertIsNone(select_compactable_range(self.session, m, 10 ** 9))

    def test_selects_range_keeping_tail(self):
        _seed_history(self.session, n=5)
        m = self.meter.measure(self.session)
        total = m["surfaceTokens"]
        sel = select_compactable_range(self.session, m, total // 2)
        self.assertIsNotNone(sel)
        start = next(i for i, n in enumerate(m["nodes"]) if n["seq"] == sel["start"])
        end = next(i for i, n in enumerate(m["nodes"]) if n["seq"] == sel["end"])
        self.assertLessEqual(start, end)  # start 位置在 end 之前
        self.assertGreaterEqual(len(m["nodes"]) - end - 1, 1)  # 至少保留 1 个尾部节点


class CompactionEntryStateTest(unittest.TestCase):
    def test_empty(self):
        s = Session("c2")
        state = inspect_compaction_entry_state(s.events)
        self.assertIsNone(state["openTurn"])
        self.assertIsNone(state["unmatchedCompactionStart"])
        self.assertIsNone(state["latestEndSeedSeq"])

    def test_open_turn_detected(self):
        s = Session("c2")
        _append(s, "turn/start", {"turn": 1})
        state = inspect_compaction_entry_state(s.events)
        self.assertEqual(state["openTurn"], 1)

    def test_unmatched_start_detected(self):
        s = Session("c2")
        _append(s, "turn/start", {"turn": 1})
        _append(s, "compaction/start", {"compactionId": "x", "turn": 1})
        state = inspect_compaction_entry_state(s.events)
        self.assertIsNotNone(state["unmatchedCompactionStart"])


class CompactSurfaceRegionTest(unittest.TestCase):
    def test_full_transaction(self):
        session = Session("c3")
        first = _seed_history(session, n=20)
        adapter = _SummaryAdapter("浓缩要点。")
        agent = _agent(session, adapter)
        nodes = session.surface_nodes()
        start, end = nodes[0]["seq"], nodes[-2]["seq"]  # 保留最后一段
        result = asyncio.run(compact_surface_region(session, TokenMeter(), agent,
                                                    resolve_config(), start, end))

        types = [e["type"] for e in session.events]
        self.assertIn("compaction/start", types)
        self.assertIn("compaction/summary", types)
        self.assertIn("compaction/end", types)
        # 检查点带 replace surfaceOp，遮蔽原区间
        checkpoint = session.events[-2]
        self.assertEqual(checkpoint["type"], "user/message")
        self.assertEqual(checkpoint["surfaceOp"],
                         {"op": "replace", "start": start, "end": end})
        self.assertEqual(result["startSeq"] < result["summarySeq"] < result["endSeq"], True)

        # 派生消息只含 检查点 + 最后一段
        msgs = derive_messages(session.events)
        self.assertEqual(len(msgs), 2)
        checkpoint_text = "".join(b.get("text", "") for b in msgs[0]["content"])
        self.assertIn("浓缩要点。", checkpoint_text)
        self.assertIn("回复 19", msgs[1]["content"][0]["text"])
        self.assertEqual(result["shadowedSeqs"][0], first)

    def test_requires_open_turn(self):
        session = Session("c3")
        _append(session, "user/message", create_message("user", [text_block("x")], {}),
                surfaceOp="append")
        _append(session, "user/message", create_message("user", [text_block("y")], {}),
                surfaceOp="append")
        # 无 turn/start → 拒绝
        adapter = _SummaryAdapter()
        agent = _agent(session, adapter)
        with self.assertRaises(RuntimeError):
            asyncio.run(compact_surface_region(session, TokenMeter(), agent,
                                               resolve_config(),
                                               session.events[0]["seq"], session.events[1]["seq"]))

    def test_failure_appends_closing_end_with_error(self):
        session = Session("c3")
        _seed_history(session, n=2)
        adapter = _SummaryAdapter(fail_times=1, fail_code="SERVER")
        agent = _agent(session, adapter)
        nodes = session.surface_nodes()
        with self.assertRaises(LlmFailure):
            asyncio.run(compact_surface_region(session, TokenMeter(), agent, resolve_config(),
                                               nodes[0]["seq"], nodes[0]["seq"]))
        last = session.events[-1]
        self.assertEqual(last["type"], "compaction/end")
        self.assertIn("error", last["data"])

    def test_unbalanced_boundary_rejected(self):
        session = Session("c3")
        session.append("turn/start", {"turn": 1})
        session.append("user/message", create_message("user", [text_block("列目录")], {}),
                       surfaceOp="append")
        session.append("step/start", {"turn": 1, "step": 1})
        assistant = create_message("assistant", [
            {"type": "tool-call", "id": "call_1", "name": "bash", "arguments": "{}"},
        ], {"kind": "model"})
        session.append("assistant/message", {"turn": 1, "step": 1, "message": assistant},
                       surfaceOp="append")
        session.append("tool/result", {"turn": 1, "step": 1,
                                       "message": create_message("user", [text_block("out")],
                                                                 {"kind": "tool"})},
                       surfaceOp="append")
        session.append("step/end", {"turn": 1, "step": 1})
        session.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
        nodes = session.surface_nodes()
        # 切在 assistant（tool-call）与 tool/result 之间 → 切散配对 → 非法
        with self.assertRaises(ValueError):
            asyncio.run(compact_surface_region(session, TokenMeter(), _agent(session, _SummaryAdapter()),
                                               resolve_config(), nodes[1]["seq"], nodes[1]["seq"]))


# ---------- 压缩引擎 ----------

class EnginePressureTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        self.session = Session("e1")
        self.adapter = _SummaryAdapter(context_window=100)  # 阈值 80 token
        self.agent = _agent(self.session, self.adapter, self.ctx)
        self.engine = CompactionEngine(self.ctx)

    def test_below_threshold_no_compaction(self):
        _seed_history(self.session, n=2)
        result = asyncio.run(self.engine.compact_if_needed(self.agent, "pressure"))
        self.assertIsNone(result)
        self.assertNotIn("compaction/start",
                         [e["type"] for e in self.session.events])

    def test_above_threshold_compacts(self):
        self.adapter.context_window = 250  # 阈值 200：日志底层开销约 144，压缩后应低于阈值
        _seed_history(self.session, n=20)
        result = asyncio.run(self.engine.compact_if_needed(self.agent, "pressure"))
        self.assertIsNotNone(result)
        self.assertEqual(self.session.replace_generation, 1)
        msgs = derive_messages(self.session.events)
        self.assertIn("压缩摘要内容。",
                      "".join(b.get("text", "") for b in msgs[0]["content"]))

    def test_no_context_window_raises_config_error(self):
        self.adapter.context_window = None
        _seed_history(self.session, n=20)
        with self.assertRaises(TargetPressureConfigError):
            asyncio.run(self.engine.compact_if_needed(self.agent, "pressure"))

    def test_unknown_trigger(self):
        with self.assertRaises(ValueError):
            asyncio.run(self.engine.compact_if_needed(self.agent, "nope"))


class EngineOverflowRecoveryTest(unittest.TestCase):
    def _setup(self, adapter, max_overflow_retries=1, auto=True, n=10):
        ctx = Context(name="root")
        apply_retry_planner(ctx)
        install_compaction(ctx, {"maxOverflowRetries": max_overflow_retries, "auto": auto})
        session = Session("e2")
        # 旧回合足够多，确保被遮蔽区间（~n*2 条消息）大于检查点框架开销（~104 token）
        _seed_history(session, n=n)  # 旧回合：溢出恢复有可压缩的区间
        loop = AgentLoop(session, adapter, ToolRegistry(ctx), ctx)
        return ctx, loop, session

    def test_overflow_compacts_and_retries(self):
        adapter = _SummaryAdapter(fail_times=1)  # 第一次溢出，第二次成功
        ctx, loop, session = self._setup(adapter)
        loop.followup("太长了，压缩。")
        types = [e["type"] for e in session.events]
        self.assertIn("compaction/start", types)
        self.assertIn("compaction/summary", types)
        self.assertIn("compaction/end", types)
        turn_end = next(e for e in session.events if e["type"] == "turn/end")["data"]["reason"]
        self.assertEqual(turn_end["kind"], "completed")

    def test_overflow_retries_capped(self):
        adapter = _SummaryAdapter(fail_times=99)  # 永远溢出
        ctx, loop, session = self._setup(adapter, max_overflow_retries=1)
        with self.assertRaises(LlmFailure) as cm:
            loop.followup("太长了。")
        self.assertEqual(cm.exception.code, "CONTEXT_WINDOW_EXCEEDED")
        starts = [e for e in session.events if e["type"] == "compaction/start"]
        self.assertEqual(len(starts), 1)  # maxOverflowRetries=1 → 仅一次

    def test_reset_after_success_then_again(self):
        # 第一次溢出→压缩→成功；第二次新溢出再触发（计数已复位）
        adapter = _SummaryAdapter(fail_times=1)
        ctx, loop, session = self._setup(adapter, max_overflow_retries=1)
        loop.followup("第一轮。")
        self.assertEqual(loop.status, "idle")
        adapter.fail_times = 1
        loop.followup("第二轮再溢出。")
        starts = [e for e in session.events if e["type"] == "compaction/start"]
        self.assertGreaterEqual(len(starts), 2)

    def test_auto_false_no_automatic_listeners(self):
        adapter = _SummaryAdapter(fail_times=99)
        ctx = Context(name="root")
        install_compaction(ctx, {"auto": False})
        with self.assertRaises(LlmFailure):
            AgentLoop(Session("e3"), adapter, ToolRegistry(ctx), ctx).followup("x")
        # 无监听器 → 无压缩事件
        # 注：无 auto 时引擎不注册监听，无法从外部直接观察；仅验证不炸


class InstallCompactionTest(unittest.TestCase):
    def test_idempotent(self):
        ctx = Context(name="root")
        install_compaction(ctx)
        install_compaction(ctx)  # 第二次 no-op
        self.assertTrue(getattr(ctx, "_miniharness_compaction_installed", False))

    def test_known_types_accept_compaction_events(self):
        for t in ("compaction/start", "compaction/summary", "compaction/end"):
            self.assertIn(t, KNOWN_TYPES)


class LogResultRoutingTest(unittest.TestCase):
    def test_log_result_routes_through_ctx_logger(self):
        # 对齐上游 compaction-basic/src/index.ts:140 `ctx.logger.info(...)`，
        # 不再走 print（P0-2）
        captured = []
        ctx = Context(name="root")
        ctx.logger.exporter({"export": captured.append})
        engine = CompactionEngine(ctx, {})
        result = {
            "shadowedSeqs": [1, 2],
            "shadowedRange": {"start": 1, "end": 2},
            "shadowedTokenCount": 10,
        }
        engine._log_result(result, "step pressure")
        self.assertEqual(len(captured), 1)
        self.assertIn(
            "compaction (step pressure): shadowed 2 surface nodes "
            "(seqs 1-2, ~10 tokens)",
            captured[0]["args"][0],
        )


# ---------- tool-result 模型无关裁剪（P0-3，对齐 compaction-tool-result-pruner）----------


def _seed_tool_result(session: Session, text: str, call_id: str = "a") -> int:
    """写 turn/start → user/message → step/start → tool/call → 大 tool/result。

    返回 tool/call 事件的 seq（供 sourceEventSeqs 引用）。
    """
    session.append("request/header",
                   {"header": {"config": {"provider": "fake", "model": "fake-model"}},
                    "reason": "initial"})
    session.append("turn/start", {"turn": 1})
    session.append("user/message",
                   create_message("user", [text_block("输入")], {"kind": "user"}),
                   surfaceOp="append")
    session.append("step/start", {"turn": 1, "step": 1})
    call = session.append("tool/call",
                          {"turn": 1, "step": 1, "callId": call_id, "name": "ls",
                           "arguments": "{}"})
    msg = create_message(
        "user",
        [tool_result_block(call_id, [text_block(text)], is_error=False)],
        {"kind": "tool", "callId": call_id},
    )
    session.append("tool/result", {"turn": 1, "step": 1, "message": msg},
                  surfaceOp="append", sourceEventSeqs=[call["seq"]])
    return call["seq"]


class ToolResultPrunerUnitTest(unittest.TestCase):
    def setUp(self):
        self.pruner = ToolResultPruner(None, {})

    def test_measure_counts_code_points(self):
        blocks = [{"type": "text", "text": "hello 世界"}, {"type": "image"}]
        # h e l l o (5) + 空格(1) + 世界(2) = 8 码点；image 不计
        self.assertEqual(self.pruner.measure_content(blocks), 8)

    def test_within_budget_returns_none(self):
        blocks = [{"type": "text", "text": "x" * 100}]
        self.assertIsNone(self.pruner.prune_content(blocks))

    def test_over_budget_keeps_head_marker_tail(self):
        blocks = [{"type": "text", "text": "y" * 100000}]
        out = self.pruner.prune_content(blocks)
        self.assertIsNotNone(out)
        text = out[0]["text"]
        # head(4096) + 固定标记 + tail(1024) 三段拼接，且仅一次标记
        self.assertIn(PRUNE_MARKER, text)
        self.assertEqual(text.count(PRUNE_MARKER), 1)
        self.assertTrue(text.startswith("y" * 4096))
        self.assertTrue(text.endswith("y" * 1024))
        self.assertLess(self.pruner.measure_content(out),
                        self.pruner.measure_content(blocks))
        self.assertLessEqual(self.pruner.measure_content(out), 8192)

    def test_prune_splits_across_text_blocks(self):
        # 标记落在中间块；前块整段保留头（未触及 removed 区），后块仅保留尾 1024
        blocks = [
            {"type": "text", "text": "a" * 3000},
            {"type": "text", "text": "b" * 10000},
            {"type": "text", "text": "c" * 3000},
        ]
        out = self.pruner.prune_content(blocks)
        self.assertEqual(out[0]["text"], "a" * 3000)
        self.assertIn(PRUNE_MARKER, out[1]["text"])
        # 中间块头 = 4096-3000 = 1096 个 b，标记后无 b（尾落在第三块）
        self.assertTrue(out[1]["text"].startswith("b" * 1096))
        self.assertTrue(out[1]["text"].endswith(PRUNE_MARKER))
        # 尾 1024 个码点落在第三块：仅保留其最后 1024 个 c
        self.assertEqual(out[2]["text"], "c" * 1024)


class ToolResultPrunerSessionTest(unittest.TestCase):
    def test_prune_session_emits_shadow_and_replace(self):
        session = Session("p1")
        _seed_tool_result(session, "z" * 100000)
        pruner = ToolResultPruner(None, {})
        result = pruner.prune_session(session)
        self.assertEqual(len(result["pruned"]), 1)
        entry = result["pruned"][0]
        self.assertEqual(entry["callId"], "a")
        self.assertGreater(entry["charsBefore"], entry["charsAfter"])
        self.assertEqual(result["charsRemoved"], entry["charsBefore"] - entry["charsAfter"])

        types = [e["type"] for e in session.events]
        # 紧邻的：compaction/prune 阴影计价事件 + tool/result 替换
        self.assertIn("compaction/prune", types)
        prune_ev = next(e for e in session.events if e["type"] == "compaction/prune")
        self.assertEqual(list(prune_ev["data"]["shadowedSeqs"]), [entry["originalSeq"]])
        self.assertGreater(prune_ev["data"]["shadowedTokenCount"], 0)
        replace_ev = session.events[entry["replacementSeq"]]
        self.assertEqual(replace_ev["type"], "tool/result")
        self.assertEqual(replace_ev["surfaceOp"], {"op": "replace", "start": entry["originalSeq"],
                                                    "end": entry["originalSeq"]})
        self.assertEqual(list(replace_ev["sourceEventSeqs"]), [entry["originalSeq"]])
        repl_text = replace_ev["data"]["message"]["content"][0]["content"][0]["text"]
        self.assertIn(PRUNE_MARKER, repl_text)

    def test_prune_session_idempotent_converges(self):
        session = Session("p2")
        _seed_tool_result(session, "z" * 100000)
        pruner = ToolResultPruner(None, {})
        first = pruner.prune_session(session)
        self.assertEqual(len(first["pruned"]), 1)
        # 二次 pass：已裁剪的内容在预算内，不再产生替换
        second = pruner.prune_session(session)
        self.assertEqual(second["pruned"], [])


class ToolResultPrunerEngineWiringTest(unittest.TestCase):
    def test_pressure_prunes_before_threshold_returns_none(self):
        # 安装 pruning，使裁剪后低于阈值 → 不进入摘要（验证模型无关预裁剪）
        session = Session("p3")
        ctx = Context(name="root")
        install_tool_result_pruner(ctx, {})
        # 30000 字符工具输出：裁剪前 ~7500 token，裁剪后 ~1290 token
        _seed_tool_result(session, "y" * 30000)

        class _Adapter:
            provider = "fake"
            model = "fake-model"
            context_window = 2500  # thresholdTokens = 2000

        agent = type("A", (), {"session": session, "adapter": _Adapter()})()
        engine = CompactionEngine(ctx, {})
        result = asyncio.run(engine.compact_if_needed(agent, "pressure"))
        self.assertIsNone(result)
        types = [e["type"] for e in session.events]
        self.assertIn("compaction/prune", types)
        # 未进入摘要（无 compaction/start）
        self.assertNotIn("compaction/start", types)

    def test_no_pruner_installed_skips(self):
        # 未安装 pruning 服务时，引擎不裁剪（_tool_result_pruner 返回 None）
        ctx = Context(name="root")
        engine = CompactionEngine(ctx, {})
        self.assertIsNone(engine._tool_result_pruner())
        ctx2 = Context(name="root2")
        install_tool_result_pruner(ctx2, {})
        engine2 = CompactionEngine(ctx2, {})
        self.assertIsNotNone(engine2._tool_result_pruner())


if __name__ == "__main__":
    unittest.main()
