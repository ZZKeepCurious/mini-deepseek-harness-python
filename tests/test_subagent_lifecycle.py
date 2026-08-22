"""A8 生命周期验收：subagent/start|end 事件对、foldConsumedWork 记账、
interrupt 授权矩阵、初始 prompt、所有权 waiting、best-effort 释放。

上游对照：packages/subagent/subagent/src/lifecycle.ts（epochStopReason）、
packages/core/agent/src/consumed-work.ts（foldConsumedWork）、
continuation.ts（stateOf / interrupt 授权矩阵 / finishDisposal 顺序）。

运行：python -m unittest tests.test_subagent_lifecycle -v
"""
import asyncio
import tempfile
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import Session, create_message, text_block
from miniharness.core.session.persistence import JsonlPersistence
from miniharness.core.session_store import install_sessions
from miniharness.core.tools import Tool, ToolRegistry
from miniharness.llm import FakeLlmAdapter, LlmFailure, StreamChunk
from miniharness.seams.subagent.continuation import (
    SubagentContinuationManager,
    SubagentError,
    epoch_stop_reason,
    fold_consumed_work,
)
from miniharness.seams.subagent.descriptor import CONTINUATION_PROVIDER


def _settlement_notices(session):
    return [e for e in session.events
            if e["type"] == "user/message"
            and e["data"].get("source", {}).get("kind") == "subagent-settled"]


async def _wait_until(predicate, timeout=3.0):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while not predicate():
        if loop.time() > deadline:
            raise TimeoutError("_wait_until timed out")
        await asyncio.sleep(0.005)


class _SilentAdapter(FakeLlmAdapter):
    """不发任何块的适配器：finish 直接 stop，无 assistant 文本。"""

    async def stream(self, messages, tools, signal=None):
        yield StreamChunk("finish", reason={"kind": "stop"})


def _parent_loop(session_id="parent", adapter=None):
    ctx = Context()
    install_sessions(ctx)
    reg = ToolRegistry(ctx)
    loop = AgentLoop(Session(session_id), adapter or FakeLlmAdapter(final_text="父响应"),
                     reg, ctx, system_prompt="你是父代理。")
    return loop, ctx, reg


class TestFoldConsumedWork(unittest.TestCase):
    """fold_consumed_work（上游 consumed-work.ts foldConsumedWork）。"""

    def test_stepped_turn_accountable(self):
        events = [
            {"type": "turn/start", "data": {"turn": 1}},
            {"type": "step/start", "data": {"turn": 1}},
            {"type": "turn/end", "data": {"turn": 1, "reason": {"kind": "completed"}}},
        ]
        folded = fold_consumed_work(events)
        self.assertEqual(folded["end"]["type"], "turn/end")
        self.assertFalse(folded["dropped_unrun"])

    def test_claim_only_counts_when_not_completed(self):
        # 认领过 inbox 输入但干净完成的 no-step turn 不描述工作（上游：
        # claimed && kind !== 'completed' 才交代）
        claim = {"type": "agent/inbox/spliced",
                 "data": {"target": "next-turn", "start": 0, "removedCount": 1,
                          "inserted": [{"id": "m1"}]}}
        turn = [
            {"type": "turn/start", "data": {"turn": 3}},
            dict(claim),
        ]

        def end(kind):
            return {"type": "turn/end", "data": {"turn": 3, "reason": {"kind": kind}}}

        self.assertIsNone(fold_consumed_work(turn + [end("completed")])["end"])
        folded = fold_consumed_work(turn + [end("aborted")])
        self.assertIsNotNone(folded["end"])

    def test_canceled_claim_marks_dropped_unrun(self):
        events = [
            {"type": "agent/inbox/spliced",
             "data": {"target": "next-turn", "start": 0, "removedCount": 2,
                      "inserted": [], "outcome": "canceled"}},
            {"type": "turn/end", "data": {"turn": 1, "reason": {"kind": "completed"}}},
        ]
        folded = fold_consumed_work(events)
        self.assertTrue(folded["dropped_unrun"])
        # 有交代的 turn 会吸收此前的 drop（该 turn 自己的结局已覆盖）
        events.append({"type": "step/start", "data": {"turn": 2}})
        events.append({"type": "turn/end", "data": {"turn": 2, "reason": {"kind": "completed"}}})
        self.assertFalse(fold_consumed_work(events)["dropped_unrun"])

    def test_replacement_keeps_work_pending(self):
        # canceled 但 inserted 非空 → 工作换了身份继续 pending，不算 drop
        events = [
            {"type": "agent/inbox/spliced",
             "data": {"target": "next-turn", "start": 0, "removedCount": 1,
                      "inserted": [{"id": "m2"}], "outcome": "canceled"}},
            {"type": "turn/end", "data": {"turn": 1, "reason": {"kind": "completed"}}},
        ]
        self.assertFalse(fold_consumed_work(events)["dropped_unrun"])


class TestEpochStopReasonDropped(unittest.TestCase):
    def test_dropped_unrun_maps_to_aborted(self):
        # 无记账 turn + 已接受输入被取消未运行 → aborted（非 completed）
        events = [
            {"type": "turn/start", "data": {"turn": 1}},
            {"type": "agent/inbox/spliced",
             "data": {"target": "next-turn", "start": 0, "removedCount": 1,
                      "inserted": [], "outcome": "canceled"}},
            {"type": "turn/end", "data": {"turn": 1, "reason": {"kind": "completed"}}},
        ]
        self.assertEqual(epoch_stop_reason(events), "aborted")

    def test_accountable_turn_wins_over_drop(self):
        events = [
            {"type": "step/start", "data": {"turn": 1}},
            {"type": "turn/end", "data": {"turn": 1, "reason": {"kind": "max-tokens"}}},
        ]
        self.assertEqual(epoch_stop_reason(events), "max-tokens")


class TestSubagentLifecycleEvents(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.persistence = JsonlPersistence(self.tmp.name)
        self.parent, self.ctx, self.reg = _parent_loop()
        self.starts: list[dict] = []
        self.ends: list[dict] = []
        self.ctx.on("subagent/start", lambda info: self.starts.append(info))
        self.ctx.on("subagent/end", lambda info: self.ends.append(info))

    def _manager(self, **kwargs):
        return SubagentContinuationManager(self.parent, self.persistence, **kwargs)

    def test_start_end_pair_payloads_and_ordering(self):
        mgr = self._manager()
        cid = mgr.start_continuable(label="研")
        mgr.send_message(cid, "查资料")
        self.assertEqual(len(self.starts), 1)
        self.assertEqual(len(self.ends), 1)
        start, end = self.starts[0], self.ends[0]
        # SubagentRunInfo：{runId, provider, id, local}
        self.assertEqual(set(start), {"runId", "provider", "id", "local"})
        self.assertEqual(start["provider"], CONTINUATION_PROVIDER)
        self.assertEqual(start["id"], cid)
        self.assertTrue(start["local"])
        # 终局边与 start 配对（同一 runId）+ stopReason + lastAssistantMessage
        self.assertEqual(end["runId"], start["runId"])
        self.assertEqual(end["id"], cid)
        self.assertEqual(end["stopReason"], "completed")
        texts = [b["text"] for b in end["lastAssistantMessage"]]
        self.assertIn("任务完成。", "".join(texts))
        # start 先于 end 发布
        mgr_state = mgr.state_of(cid)
        self.assertEqual(mgr_state["kind"], "idle")

    def test_end_omits_last_assistant_message_when_none(self):
        mgr = self._manager(adapter_factory=lambda p, m: _SilentAdapter())
        cid = mgr.start_continuable(label="研")
        mgr.send_message(cid, "查资料")
        self.assertEqual(self.ends[-1]["stopReason"], "completed")
        self.assertNotIn("lastAssistantMessage", self.ends[-1])

    def test_child_error_stop_reason_and_no_output(self):
        class BoomAdapter(FakeLlmAdapter):
            async def stream(self, messages, tools, signal=None):
                raise LlmFailure("RATE_LIMIT", "429")
                yield  # pragma: no cover

        mgr = self._manager(adapter_factory=lambda p, m: BoomAdapter())
        cid = mgr.start_continuable(label="研")
        mgr.send_message(cid, "查资料")   # 子失败不冒泡
        self.assertEqual(self.ends[-1]["stopReason"], "error")
        self.assertNotIn("lastAssistantMessage", self.ends[-1])

    def test_listener_error_contained(self):
        # 发射器逐监听器收容：坏监听器不饿死同侪、不影响 run 与后续事件边
        seen: list[str] = []
        self.ctx.on("subagent/start", lambda info: 1 / 0)
        self.ctx.on("subagent/start", lambda info: seen.append(info["id"]))
        mgr = self._manager()
        cid = mgr.start_continuable(label="研")
        mgr.send_message(cid, "查资料")
        self.assertEqual(seen, [cid])
        self.assertEqual(len(self.ends), 1)

    def test_flush_failure_still_releases_and_reports_error(self):
        # best-effort final flush（上游 flushFinalState）：持久层失败只告警，
        # teardown 继续；终局转 error 且扣住输出
        mgr = self._manager()
        cid = mgr.start_continuable(label="研")

        def boom(session, start):
            raise OSError("disk on fire")

        mgr._persist_delta = boom
        with self.assertLogs("miniharness.seams.subagent.continuation", level="WARNING"):
            mgr.send_message(cid, "查资料")
        self.assertEqual(self.ends[-1]["stopReason"], "error")
        self.assertNotIn("lastAssistantMessage", self.ends[-1])
        self.assertEqual(mgr.state_of(cid)["kind"], "idle")


class TestInterruptAuthority(unittest.TestCase):
    """interrupt 授权矩阵（上游 continuation.ts interrupt，逐语义对齐）。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.persistence = JsonlPersistence(self.tmp.name)
        self.parent, self.ctx, self.reg = _parent_loop()
        self.mgr = SubagentContinuationManager(self.parent, self.persistence)
        # 第二个在世 agent（另一 manager 注册表成员），用于 stale/外部调用方
        other, _, _ = _parent_loop(session_id="other")
        self.other_manager = SubagentContinuationManager(other, self.persistence)
        self.other = other

    def test_stale_caller_unauthorized_even_when_target_absent(self):
        # 目标完全不存在也先校验调用方——防同 id 探针（上游注释明示）
        with self.assertRaises(SubagentError) as cm:
            self.mgr.interrupt(
                "child-nonexistent", {"kind": "ancestor", "agent": self.other})
        self.assertEqual(cm.exception.code, "UNAUTHORIZED")

    def test_self_interrupt_unauthorized(self):
        with self.assertRaises(SubagentError) as cm:
            self.mgr.interrupt(self.parent.id, {"kind": "ancestor", "agent": self.parent})
        self.assertEqual(cm.exception.code, "UNAUTHORIZED")

    def test_user_authority_parent_session_mismatch(self):
        cid = self.mgr.start_continuable(label="研")
        # 物化激活（send_message 授权所见的同一状态）；create-only 不建激活
        self.mgr._get_or_resume(cid)
        bad = {"kind": "user", "parentSessionId": "someone-else"}
        with self.assertRaises(SubagentError) as cm:
            self.mgr.interrupt(cid, bad)
        self.assertEqual(cm.exception.code, "UNAUTHORIZED")

    def test_absent_target_noop_precedes_user_validation(self):
        # 上游顺序：缺席激活在 user 表单校验之前返回——已结算/未知目标一律
        # 接受性 no-op，即使呈现的 parentSession 地址也不匹配
        authority = {"kind": "user", "parentSessionId": "someone-else"}
        self.assertIsNone(self.mgr.interrupt("child-missing", authority))

    def test_ancestor_outside_lineage_unauthorized(self):
        cid = self.mgr.start_continuable(label="研")
        with self.assertRaises(SubagentError) as cm:
            self.mgr.interrupt(cid, {"kind": "ancestor", "agent": self.other})
        self.assertEqual(cm.exception.code, "UNAUTHORIZED")

    def test_absent_target_accepted_noop(self):
        # 缺席目标 = 接受性 no-op（自然完成竞速/重复请求/未知 id），合法授权
        authority = {"kind": "user", "parentSessionId": self.parent.id}
        self.assertIsNone(self.mgr.interrupt("child-missing", authority))

    def test_user_authority_interrupt_causes_user_abort(self):
        child_adapter = FakeLlmAdapter(tool_call={"name": "client_cancel", "arguments": {}})
        mgr = SubagentContinuationManager(
            self.parent, self.persistence, adapter_factory=lambda p, m: child_adapter)
        cid = mgr.start_continuable(label="研")
        # 子回合内自中断（模拟客户端 user 权限经工具下达）；工具闭包必须绑
        # 定持有激活的同一 manager 实例
        self.reg.register(Tool(
            name="client_cancel", description="d",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=lambda a, e: mgr.interrupt(
                cid, {"kind": "user", "parentSessionId": self.parent.id})))
        mgr.send_message(cid, "开始")
        events = self.persistence.inspect(cid)["events"]
        last_end = next(e for e in reversed(events) if e["type"] == "turn/end")
        self.assertEqual(last_end["data"]["reason"],
                         {"kind": "aborted", "reason": {"kind": "user"}})

    def test_ancestor_authority_interrupt_causes_parent_abort(self):
        child_adapter = FakeLlmAdapter(tool_call={"name": "cancel_self", "arguments": {}})
        mgr = SubagentContinuationManager(
            self.parent, self.persistence, adapter_factory=lambda p, m: child_adapter)
        cid = mgr.start_continuable(label="研")
        self.reg.register(Tool(
            name="cancel_self", description="d",
            parameters={"type": "object", "properties": {}, "required": []},
            execute=lambda a, e: mgr.interrupt(
                cid, {"kind": "ancestor", "agent": self.parent})))
        mgr.send_message(cid, "开始")
        events = self.persistence.inspect(cid)["events"]
        last_end = next(e for e in reversed(events) if e["type"] == "turn/end")
        self.assertEqual(last_end["data"]["reason"],
                         {"kind": "aborted", "reason": {"kind": "parent"}})


class TestInitialPrompt(unittest.TestCase):
    def test_start_with_prompt_returns_ids_and_runs(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        persistence = JsonlPersistence(tmp.name)
        parent, ctx, reg = _parent_loop()
        mgr = SubagentContinuationManager(parent, persistence)
        result = mgr.start_continuable(label="研", prompt="初始委托")
        self.assertEqual(set(result), {"childId", "messageId"})
        cid = result["childId"]
        self.assertIsInstance(result["messageId"], str)
        # 初始 prompt 经同一条投递路径进入子会话并同步跑完首回合
        events = persistence.inspect(cid)["events"]
        user_texts = [e["data"]["content"][0]["text"] for e in events
                      if e["type"] == "user/message"]
        self.assertIn("初始委托", user_texts)
        self.assertEqual(events[-1]["type"], "turn/end")
        self.assertEqual(events[-1]["data"]["reason"]["kind"], "completed")
        # 结算通知照常投递父代理
        self.assertEqual(len(_settlement_notices(parent.session)), 1)


class TestNestedOwnership(unittest.TestCase):
    """嵌套续跑的所有权记账：owned 子代未拆除时父不可结算（waiting）。"""

    def test_waiting_until_owned_child_settles(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        persistence = JsonlPersistence(tmp.name)
        parent, ctx, reg = _parent_loop()
        self.starts: list[dict] = []
        self.ends: list[dict] = []
        ctx.on("subagent/start", lambda info: self.starts.append(info))
        ctx.on("subagent/end", lambda info: self.ends.append(info))
        spawned: list[str] = []
        # 共享脚本适配器：首次调用发 spawn_gc，之后固定文本（子与孙共用；
        # 孙代对 spawn_gc 的调用被工具守卫短路为叶节点）
        shared = FakeLlmAdapter(tool_call={"name": "spawn_gc", "arguments": {}})
        mgr = SubagentContinuationManager(
            parent, persistence, adapter_factory=lambda p, m: shared)
        cid = mgr.start_continuable(label="子")

        async def spawn_gc(args, exec_):
            caller = exec_.agent
            if caller.id != cid:
                return "leaf"       # 孙代误调用：已是叶，不再委托
            gc = mgr.start_continuable(label="孙", prompt="孙任务", parent=caller)
            spawned.append(gc["childId"])
            return gc["childId"]

        reg.register(Tool(name="spawn_gc", description="d",
                          parameters={"type": "object", "properties": {},
                                      "required": []},
                          execute=spawn_gc))

        async def scenario():
            parent.start_driver()
            mgr.send_message(cid, "造一个孙代理")
            await _wait_until(lambda: cid not in mgr._activations, timeout=5.0)
            self.assertEqual(len(spawned), 1)
            gc = spawned[0]
            # 终局边顺序：孙代 end 先于子代 end（releaseOwnership 唤醒后
            # 子才可结算——owned 子未拆完时父停在 waiting）
            ends_by_id = {info["id"]: i for i, info in enumerate(self.ends)}
            self.assertIn(cid, ends_by_id)
            self.assertIn(gc, ends_by_id)
            self.assertLess(ends_by_id[gc], ends_by_id[cid])
            self.assertEqual(self.ends[ends_by_id[gc]]["stopReason"], "completed")
            self.assertEqual(self.ends[ends_by_id[cid]]["stopReason"], "completed")
            # 子的结算通知投顶层父；孙的通知投直属父（子会话）
            top_notices = [n["data"]["source"]["senderSessionId"]
                           for n in _settlement_notices(parent.session)]
            self.assertIn(cid, top_notices)
            child_events = persistence.inspect(cid)["events"]
            self.assertTrue(any(
                e["type"] == "user/message"
                and e["data"].get("source", {}).get("senderSessionId") == gc
                for e in child_events))
            # 枚举含两代后代（沿 meta.parentSession 链 BFS）
            ids = {e["id"] for e in mgr.list_descendants()}
            self.assertEqual(ids, {cid, gc})
            depths = {e["id"]: e["depth"] for e in mgr.list_descendants()}
            self.assertEqual(depths[cid], 1)
            self.assertEqual(depths[gc], 2)

        asyncio.run(scenario())


if __name__ == "__main__":
    unittest.main()
