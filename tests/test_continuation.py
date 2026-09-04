"""A7/A8 可继续子代理验收：durable 子会话 + 冷恢复 + 结算投递 + 枚举 + 控制工具。

A8：异步事件驱动执行（父有 driver 时 sendMessage 投递即返回、residency 跨回合、
watchSettlement 结算、steer 批内合并、interrupt 缺省 no-op）。

运行：python -m unittest tests.test_continuation -v
"""
import asyncio
import json
import tempfile
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import Session, create_message, text_block
from miniharness.core.session.persistence import JsonlPersistence, SqlitePersistence
from miniharness.core.session_store import install_sessions
from miniharness.core.tools import Tool, ToolExec, ToolRegistry
from miniharness.llm import FakeLlmAdapter, LlmFailure, StreamChunk
from miniharness.seams.subagent.continuation import (
    CONTEXT_SUMMARY_MAX_CHARS,
    SubagentContinuationManager,
    SubagentError,
    bound_context_summary,
    epoch_stop_reason,
    final_assistant_output,
    install_subagent_control_tools,
    settlement_summary,
)
from miniharness.seams.subagent.descriptor import (
    SUBAGENT_DESCRIPTOR_VERSION,
    fold_subagent_descriptor,
    parse_subagent_descriptor,
    seed_descriptor_turn,
    snapshot_subagent_descriptor,
)


def _settlement_notices(session):
    """父会话里的 subagent-settled 结算通知列表。"""
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


def _parent_loop(session_id="parent", adapter=None):
    ctx = Context()
    install_sessions(ctx)
    reg = ToolRegistry(ctx)
    loop = AgentLoop(Session(session_id), adapter or FakeLlmAdapter(final_text="父响应"),
                     reg, ctx, system_prompt="你是父代理。")
    return loop, ctx, reg


class _ScriptedParent(FakeLlmAdapter):
    """父脚本：第一次调用发工具调用，之后固定文本。cid 事后注入。"""

    def __init__(self, tool_name, tool_args=None):
        super().__init__()
        self.tool_name = tool_name
        self.tool_args = tool_args or {}
        self.cid = None

    async def stream(self, messages, tools, signal=None):
        self.calls += 1
        if self.calls == 1:
            text = self._last_user_text(messages)
            arguments = json.dumps(
                {"subagentId": self.cid, "message": text, **self.tool_args},
                ensure_ascii=False)
            yield StreamChunk("block-start", index=0, blockType="tool-call")
            yield StreamChunk("tool-call-delta", index=0, id="call_0",
                              name=self.tool_name, argumentsDelta=arguments)
            yield StreamChunk("block-end", index=0, block={
                "type": "tool-call", "id": "call_0", "name": self.tool_name,
                "arguments": arguments,
            })
            yield StreamChunk("finish", reason={"kind": "tool-calls"})
        else:
            yield StreamChunk("block-start", index=0, blockType="text")
            yield StreamChunk("text-delta", index=0, text="父响应")
            yield StreamChunk("block-end", index=0, block={"type": "text", "text": "父响应"})
            yield StreamChunk("finish", reason={"kind": "stop"})

    @staticmethod
    def _last_user_text(messages):
        for m in reversed(messages):
            if m.get("role") != "user":
                continue
            return "".join(b.get("text", "") for b in m.get("content", [])
                           if b.get("type") == "text")
        return ""


class TestDescriptor(unittest.TestCase):
    # 描述符 schema 对齐上游 descriptor.ts（2026-08-18 对齐清零轮）：
    # {version, mode, provider, label?, agentProvider?, agentModel?, persona?, toolFilter?}

    def _payload(self, **overrides):
        payload = {"mode": "continuable", "provider": "in-process", "label": "研"}
        payload.update(overrides)
        return payload

    def test_snapshot_and_parse_roundtrip(self):
        descriptor = self._payload()
        snapshot = snapshot_subagent_descriptor(descriptor)
        self.assertEqual(snapshot["version"], SUBAGENT_DESCRIPTOR_VERSION)
        self.assertEqual(parse_subagent_descriptor(snapshot), snapshot)

    def test_parse_fail_closed(self):
        self.assertIsNone(parse_subagent_descriptor({}))
        self.assertIsNone(parse_subagent_descriptor(
            {"version": 1, "mode": "continuable", "provider": "p", "label": "x"}))
        # 未知键之外的一切，先查版本（descriptor.ts:210 同序）：
        # 版本不匹配 → 不进入字段校验而直接 None
        self.assertIsNone(parse_subagent_descriptor(
            {"version": 2, "mode": "continuable", "provider": "p", "label": "x"}))
        self.assertIsNone(parse_subagent_descriptor(
            {"version": SUBAGENT_DESCRIPTOR_VERSION,
             "mode": "continuable", "provider": 42, "label": "x"}))
        self.assertIsNone(parse_subagent_descriptor(
            {"version": SUBAGENT_DESCRIPTOR_VERSION,
             "mode": "background", "provider": "p", "label": "x"}))
        self.assertIsNone(parse_subagent_descriptor(
            {"version": SUBAGENT_DESCRIPTOR_VERSION,
             "mode": "continuable", "provider": "p"}))  # continuable 缺 label
        self.assertIsNone(parse_subagent_descriptor(
            {"version": SUBAGENT_DESCRIPTOR_VERSION, "mode": "continuable",
             "provider": "p", "label": "x",
             "toolFilter": "not-a-list"}))
        self.assertIsNone(parse_subagent_descriptor(
            {"version": SUBAGENT_DESCRIPTOR_VERSION, "mode": "continuable",
             "provider": "p", "label": "x",
             "toolFilter": {"allow": "not-a-list"}}))
        self.assertIsNone(parse_subagent_descriptor(
            {"version": SUBAGENT_DESCRIPTOR_VERSION, "mode": "continuable",
             "provider": "p", "label": "x",
             "bogusField": 1}))  # 未知字段拒绝（上游 assertKnownKeys）
        self.assertIsNone(parse_subagent_descriptor("junk"))
        self.assertIsNone(parse_subagent_descriptor(None))

    def test_one_shot_descriptor_parses_but_not_continuable(self):
        payload = {"version": SUBAGENT_DESCRIPTOR_VERSION,
                   "mode": "one-shot", "provider": "p", "label": "once"}
        self.assertEqual(parse_subagent_descriptor(payload), payload)
        self.assertIsNone(parse_subagent_descriptor(
            {"version": SUBAGENT_DESCRIPTOR_VERSION,
             "mode": "one-shot", "provider": "p", "persona": "x"}))  # one-shot 无 persona

    def test_tool_filter_shape(self):
        payload = self._payload(toolFilter={"allow": ["bash"], "deny": ["fs_write"]})
        self.assertEqual(parse_subagent_descriptor(
            snapshot_subagent_descriptor(payload)).get("toolFilter"),
            {"allow": ["bash"], "deny": ["fs_write"]})

    def test_agent_reasoning_effort_roundtrip(self):
        # v3 增量：可选 branded string（descriptor.ts:213-222 同款）；
        # 未提供 → 键整体缺席；非字符串 → 拒绝
        descriptor = self._payload(agentProvider="deepseek-official",
                                   agentModel="deepseek-chat",
                                   agentReasoningEffort="high")
        snapshot = snapshot_subagent_descriptor(descriptor)
        parsed = parse_subagent_descriptor(snapshot)
        self.assertEqual(parsed["version"], SUBAGENT_DESCRIPTOR_VERSION)
        self.assertEqual(parsed["agentProvider"], "deepseek-official")
        self.assertEqual(parsed["agentModel"], "deepseek-chat")
        self.assertEqual(parsed["agentReasoningEffort"], "high")
        bare = parse_subagent_descriptor(snapshot_subagent_descriptor(self._payload()))
        self.assertNotIn("agentProvider", bare)
        self.assertNotIn("agentModel", bare)
        self.assertNotIn("agentReasoningEffort", bare)
        self.assertIsNone(parse_subagent_descriptor(
            {"version": SUBAGENT_DESCRIPTOR_VERSION, "mode": "continuable",
             "provider": "p", "label": "x", "agentReasoningEffort": 13}))

    def test_fold_first_authoritative(self):
        # 首条权威；之后重复的同型事件被无视（上游 find 首条，非损坏）
        session = Session("c1")
        seed_descriptor_turn(session, self._payload(label="a"))
        folded = fold_subagent_descriptor(session.events)
        self.assertEqual(folded["label"], "a")
        self.assertEqual(folded["version"], SUBAGENT_DESCRIPTOR_VERSION)
        self.assertIsNone(fold_subagent_descriptor([]))
        seed_descriptor_turn(session, self._payload(label="b"))
        again = fold_subagent_descriptor(session.events)
        self.assertEqual(again["label"], "a")  # 第二条不能改写首条声明的组合


class TestSettlementHelpers(unittest.TestCase):
    @staticmethod
    def _turn(turn_no, kind):
        """一个进入过 step 的完整 turn 事件序列（fold 记账要求 step/start）。"""
        return [
            {"type": "turn/start", "data": {"turn": turn_no}},
            {"type": "step/start", "data": {"turn": turn_no}},
            {"type": "turn/end", "data": {"turn": turn_no, "reason": {"kind": kind}}},
        ]

    def test_epoch_stop_reason_mapping(self):
        self.assertEqual(epoch_stop_reason(self._turn(1, "completed")), "completed")
        self.assertEqual(epoch_stop_reason(self._turn(1, "aborted")), "aborted")
        self.assertEqual(epoch_stop_reason(self._turn(1, "interrupted")), "aborted")
        self.assertEqual(epoch_stop_reason(self._turn(1, "blocked")), "refusal")
        self.assertEqual(epoch_stop_reason(self._turn(1, "max-tokens")), "max-tokens")
        self.assertEqual(epoch_stop_reason(self._turn(1, "error")), "error")
        self.assertEqual(epoch_stop_reason(self._turn(1, "bogus")), "error")
        self.assertEqual(epoch_stop_reason([]), "completed")

    def test_settlement_summary_wording(self):
        self.assertEqual(
            settlement_summary("completed", "c1"),
            "Background subagent c1 finished and will do no further work unless you send it more.")
        self.assertEqual(settlement_summary("aborted", "c1"),
                         "Background subagent c1 was stopped before it finished.")
        self.assertEqual(settlement_summary("max-tokens", "c1"),
                         "Background subagent c1 ran out of room before it finished.")
        self.assertEqual(settlement_summary("refusal", "c1"),
                         "Background subagent c1 declined the task.")
        self.assertEqual(settlement_summary("error", "c1"),
                         "Background subagent c1 failed before it finished.")
        self.assertEqual(settlement_summary("mystery", "c1"),
                         "Background subagent c1 ended abnormally (mystery) before it finished.")

    def test_final_assistant_output(self):
        def am(text):
            content = [{"type": "text", "text": text}] if text else []
            return {"type": "assistant/message", "data": {
                "message": {"role": "assistant", "content": content}}}
        # 最后非空 assistant 文本胜出
        out = final_assistant_output([am("第一"), am(""), am("第二")])
        self.assertEqual(out, [{"type": "text", "text": "第二"}])
        # 全空 → 首个有内容的 assistant 消息
        out = final_assistant_output([am(""), am("只有这一个")])
        self.assertEqual(out, [{"type": "text", "text": "只有这一个"}])
        # 无 assistant 消息 → None
        self.assertIsNone(final_assistant_output([]))
        self.assertIsNone(final_assistant_output([{"type": "turn/end", "data": {}}]))

    def test_bound_context_summary(self):
        self.assertEqual(bound_context_summary("短"), "短")
        long_text = "x" * (CONTEXT_SUMMARY_MAX_CHARS + 10)
        clipped = bound_context_summary(long_text)
        self.assertTrue(clipped.startswith("x" * CONTEXT_SUMMARY_MAX_CHARS))
        self.assertTrue(clipped.endswith("…"))


class TestPersistenceApi(unittest.TestCase):
    """declare / inspect / list_headers：两个后端一致。"""

    def _check(self, persistence):
        # v2 物理 header 键闭集：label 不再入 header meta（随 descriptor 事件持久化）
        persistence.declare("c1", {"parentSession": "p1", "origin": "subagent",
                                   "delegationDepth": 1})
        persistence.declare("c1", {"parentSession": "p1"})  # 幂等：第二次忽略
        session = Session("c1")
        session.append("subagent/descriptor", {"version": 2, "mode": "continuable",
                                               "provider": "in-process", "label": "研"})
        for ev in session.events:
            persistence.append("c1", ev)
        persistence.flush()
        info = persistence.inspect("c1")
        self.assertEqual(info["meta"]["parentSession"], "p1")
        self.assertNotIn("label", info["meta"])
        descriptor = next(e for e in info["events"] if e["type"] == "subagent/descriptor")
        self.assertEqual(descriptor["data"]["label"], "研")
        self.assertEqual(info["events"][0]["type"], "subagent/descriptor")
        headers = persistence.list_headers()
        self.assertEqual([h["id"] for h in headers], ["c1"])
        self.assertEqual(headers[0]["meta"]["delegationDepth"], 1)
        self.assertIsNone(persistence.inspect("nope")["meta"])
        self.assertEqual(persistence.inspect("nope")["events"], [])
        # 键闭集 fail loud：未知 meta 键在写边界拒绝
        with self.assertRaises(ValueError) as ctx:
            persistence.declare("c2", {"parentSession": "p1", "label": "x"})
        self.assertIn("closed physical header key set", str(ctx.exception))

    def test_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            self._check(JsonlPersistence(tmp))

    def test_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            persistence = SqlitePersistence(tmp)
            try:
                self._check(persistence)
            finally:
                persistence.close()


class TestContinuationManager(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.persistence = JsonlPersistence(self.tmp.name)
        self.parent, self.ctx, self.reg = _parent_loop()

    def _manager(self, **kwargs):
        return SubagentContinuationManager(self.parent, self.persistence, **kwargs)

    def test_start_durable_before_dispatch(self):
        mgr = self._manager()
        cid = mgr.start_continuable(label="研", persona="你是子代理研究员")
        self.assertTrue(cid.startswith("child-"))
        # 创建即落盘：header meta + 描述符事件（无需任何运行）。
        # v2 header 键闭集：label 不入 header，随 descriptor 事件持久化。
        info = self.persistence.inspect(cid)
        self.assertEqual(info["meta"]["parentSession"], "parent")
        self.assertEqual(info["meta"]["origin"], "subagent")
        self.assertEqual(info["meta"]["delegationDepth"], 1)
        descriptor = next(e for e in info["events"] if e["type"] == "subagent/descriptor")
        self.assertEqual(descriptor["data"]["label"], "研")
        # V2：显式空 seed 的子会话携带 {} end-seed 标记（上游 constructor：
        # seed !== undefined 且日志末尾非 end-seed → 补记；不带 inherited 键）
        self.assertEqual([e["type"] for e in info["events"]],
                         ["session/end-seed", "subagent/descriptor"])
        self.assertEqual(info["events"][0]["data"], {})
        self.assertEqual(mgr.activations, {})

    def test_send_message_settles_and_wakes_idle_parent(self):
        mgr = self._manager()
        cid = mgr.start_continuable(label="研")
        mgr.send_message(cid, "查资料")
        # 结算通知唤醒 idle 父：父会话出现 subagent-settled 消息 + 父回应
        user_sources = [e["data"]["source"]["kind"] for e in self.parent.session.events
                        if e["type"] == "user/message"]
        self.assertEqual(user_sources, ["subagent-settled"])
        settled = next(e for e in self.parent.session.events
                       if e["type"] == "user/message" and e["data"]["source"]["kind"] == "subagent-settled")
        src = settled["data"]["source"]
        self.assertEqual(src["form"], "notice")
        self.assertEqual(src["senderSessionId"], cid)
        text = "".join(b["text"] for b in settled["data"]["content"] if b["type"] == "text")
        self.assertIn(f"Background subagent {cid} finished and will do no further work", text)
        self.assertIn("Its closing message:", text)
        self.assertIn("任务完成。", text)
        self.assertEqual(self.parent.last_response(), "父响应")
        self.assertEqual(mgr.state_of(cid)["kind"], "idle")

    def test_child_events_persisted_and_cold_resume(self):
        mgr = self._manager()
        cid = mgr.start_continuable(label="研")
        mgr.send_message(cid, "第一问")
        mgr.send_message(cid, "第二问")
        events = self.persistence.inspect(cid)["events"]
        types = [e["type"] for e in events]
        self.assertEqual(types.count("turn/start"), 2)
        self.assertEqual(types.count("turn/end"), 2)
        # 第二轮可见第一轮历史（冷恢复 seed 含已完成回合）
        user_msgs = [e["data"]["content"][0]["text"] for e in events
                     if e["type"] == "user/message"]
        self.assertEqual(user_msgs, ["第一问", "第二问"])
        # 父收到两条结算通知
        notices = [e for e in self.parent.session.events
                   if e["type"] == "user/message"
                   and e["data"]["source"]["kind"] == "subagent-settled"]
        self.assertEqual(len(notices), 2)

    def test_cold_resume_rebuilds_adapter_via_factory(self):
        factory_calls = []
        def factory(provider, model, reasoning_effort=None):
            factory_calls.append((provider, model, reasoning_effort))
            return FakeLlmAdapter(final_text="重建后的子代理")
        mgr = self._manager(adapter_factory=factory)
        cid = mgr.start_continuable(label="研")
        mgr.send_message(cid, "第一问")
        # 新管理器 + 同一持久化 → 冷恢复；adapter 经 factory 重建
        mgr2 = SubagentContinuationManager(self.parent, self.persistence, adapter_factory=factory)
        mgr2.send_message(cid, "第二问")
        self.assertTrue(any(p == "fake" for p, _, _ in factory_calls))
        events = self.persistence.inspect(cid)["events"]
        user_msgs = [e["data"]["content"][0]["text"] for e in events
                     if e["type"] == "user/message"]
        self.assertEqual(user_msgs, ["第一问", "第二问"])

    def test_unauthorized_parent(self):
        mgr = self._manager()
        cid = mgr.start_continuable(label="研")
        other, _, _ = _parent_loop("other")
        mgr2 = SubagentContinuationManager(other, self.persistence)
        with self.assertRaises(SubagentError) as cm:
            mgr2.send_message(cid, "hi")
        self.assertEqual(cm.exception.code, "UNAUTHORIZED")

    def test_not_resumable_without_descriptor(self):
        session = Session("orphan")
        session.append("user/message", create_message("user", [text_block("x")],
                                                      {"kind": "user"}), surfaceOp="append")
        self.persistence.declare("orphan", {"parentSession": "parent",
                                            "origin": "subagent", "delegationDepth": 1})
        for ev in session.events:
            self.persistence.append("orphan", ev)
        self.persistence.flush()
        mgr = self._manager()
        with self.assertRaises(SubagentError) as cm:
            mgr.send_message("orphan", "hi")
        self.assertEqual(cm.exception.code, "NOT_RESUMABLE")

    def test_resubmit_reuses_existing_activation(self):
        # A8：激活内再投递并入同一 residency（对齐上游 followup 对已有激活
        # 直接 submitAdmitted）；A7 的 ACTIVATION_CLOSING 防御守卫已移除。
        mgr = self._manager()
        cid = mgr.start_continuable(label="研")
        activation = mgr._get_or_resume(cid)
        try:
            self.assertIs(mgr._get_or_resume(cid), activation)
            # stateOf 以 agent 自身状态为准（上游）：仅物化、尚未投递的激活
            # 无在途回合也无 accepted 投递 → settled
            self.assertEqual(mgr.state_of(cid)["kind"], "settled")
        finally:
            activation["ctx"].dispose()
            mgr._activations.pop(cid, None)

    def test_child_error_settles_without_raising(self):
        class BoomAdapter(FakeLlmAdapter):
            async def stream(self, messages, tools, signal=None):
                raise LlmFailure("RATE_LIMIT", "429 Too Many Requests")
                yield  # pragma: no cover - 使函数成为 async 生成器（首个 __anext__ 即抛）

        mgr = self._manager(adapter_factory=lambda p, m, r=None: BoomAdapter())
        cid = mgr.start_continuable(label="研")
        mgr.send_message(cid, "hi")   # 子失败不冒泡
        settled = next(e for e in self.parent.session.events
                       if e["type"] == "user/message"
                       and e["data"]["source"]["kind"] == "subagent-settled")
        text = "".join(b["text"] for b in settled["data"]["content"] if b["type"] == "text")
        self.assertIn("failed before it finished.", text)
        # 子会话自身保留 error turn/end
        events = self.persistence.inspect(cid)["events"]
        self.assertEqual(events[-1]["type"], "turn/end")
        self.assertEqual(events[-1]["data"]["reason"]["kind"], "error")
        self.assertEqual(events[-1]["data"]["reason"]["error"]["code"], "RATE_LIMIT")

    def test_interrupt_self_cancel(self):
        mgr = self._manager()
        cid = mgr.start_continuable(label="研")
        cancel_tool = Tool(
            name="cancel_self", description="d",
            execute=lambda a, e: mgr.interrupt(cid, {"kind": "ancestor", "agent": mgr.parent}))
        # 注入子工具：子注册表复制父全局工具；这里注册到父 → 复制进子
        self.reg.register(cancel_tool)
        child_adapter = FakeLlmAdapter(tool_call={"name": "cancel_self", "arguments": {}})
        mgr = self._manager(adapter_factory=lambda p, m, r=None: child_adapter)
        mgr.send_message(cid, "hi")
        settled = next(e for e in self.parent.session.events
                       if e["type"] == "user/message"
                       and e["data"]["source"]["kind"] == "subagent-settled")
        text = "".join(b["text"] for b in settled["data"]["content"] if b["type"] == "text")
        self.assertIn("was stopped before it finished.", text)
        events = self.persistence.inspect(cid)["events"]
        self.assertEqual(events[-1]["type"], "turn/end")
        self.assertEqual(events[-1]["data"]["reason"]["kind"], "aborted")

    def test_interrupt_inactive_noop(self):
        # A8：缺省目标是接受性 no-op（上游 continuation.ts:517-520 "An absent
        # target is an accepted no-op"），不再抛 NOT_FOUND。
        mgr = self._manager()
        cid = mgr.start_continuable(label="研")
        authority = {"kind": "user", "parentSessionId": self.parent.id}
        self.assertIsNone(mgr.interrupt(cid, authority))
        self.assertEqual(mgr.state_of(cid)["kind"], "idle")

    def test_max_depth(self):
        mgr = self._manager(max_depth=0)
        with self.assertRaises(SubagentError) as cm:
            mgr.start_continuable(label="研")
        self.assertEqual(cm.exception.code, "MAX_DEPTH_EXCEEDED")

    def test_reserved_child_id_honored(self):
        # rc.2 ContinuableStartSpec.childId：预留 id 原样采用
        mgr = self._manager()
        cid = mgr.start_continuable(label="研", child_id="child-fixed001")
        self.assertEqual(cid, "child-fixed001")
        info = self.persistence.inspect(cid)
        self.assertEqual(info["meta"]["parentSession"], "parent")

    def test_duplicate_child_id_rejected(self):
        # 预留 id 已持久化 → DUPLICATE_CHILD（上游 continuation.ts:455 措辞逐字）
        mgr = self._manager()
        mgr.start_continuable(label="研", child_id="child-dup00001")
        with self.assertRaises(SubagentError) as cm:
            mgr.start_continuable(label="又", child_id="child-dup00001")
        self.assertEqual(cm.exception.code, "DUPLICATE_CHILD")
        self.assertEqual(str(cm.exception), 'subagent "child-dup00001" already exists')

    def test_duplicate_child_id_live_registry(self):
        # 活体注册表分支：在世 agent 的 id 同样不可预留
        mgr = self._manager()
        with self.assertRaises(SubagentError) as cm:
            mgr.start_continuable(child_id=self.parent.id)
        self.assertEqual(cm.exception.code, "DUPLICATE_CHILD")

    def test_invalid_report_delivery_config_rejected(self):
        with self.assertRaises(ValueError):
            self._manager(report_delivery="foreground")

    def test_drain_children_requires_exact_live_parent(self):
        mgr = self._manager()
        ghost, _, _ = _parent_loop("ghost")
        with self.assertRaises(SubagentError) as cm:
            mgr.drain_children(ghost, [])
        self.assertEqual(cm.exception.code, "UNAUTHORIZED")
        self.assertEqual(str(cm.exception),
                         "selected child teardown requires the exact live parent agent")

    def test_report_wakeup_and_quiet(self):
        mgr = self._manager()
        cid = mgr.start_continuable(label="研")
        # next-step：父 idle → 唤醒（rc.2 词汇：'wakeup' 改名 'next-step'）
        mgr.report_from(cid, "进展1", delivery="next-step")
        self.assertEqual([e["data"]["source"]["kind"] for e in self.parent.session.events
                          if e["type"] == "user/message"], ["subagent-report"])
        # quiet：不唤醒，等下一次 pump
        mgr.report_from(cid, "进展2", delivery="quiet")
        self.assertEqual(len([e for e in self.parent.session.events if e["type"] == "user/message"]), 1)
        self.parent.followup("再来一轮")
        sources = [e["data"]["source"]["kind"] for e in self.parent.session.events
                   if e["type"] == "user/message"]
        self.assertIn("subagent-report", sources)
        quiet = [e for e in self.parent.session.events if e["type"] == "user/message"
                 and e["data"]["source"]["kind"] == "subagent-report"]
        self.assertEqual(quiet[-1]["data"]["source"]["form"], "relay")

    def test_list_children(self):
        mgr = self._manager()
        cid1 = mgr.start_continuable(label="甲")
        cid2 = mgr.start_continuable(label="乙")
        entries = mgr.list_children()
        self.assertEqual([e["id"] for e in entries], sorted([cid1, cid2]))
        by_id = {e["id"]: e for e in entries}
        self.assertEqual(by_id[cid1]["label"], "甲")
        self.assertEqual(by_id[cid1]["depth"], 1)
        self.assertEqual(by_id[cid1]["status"], "idle")
        self.assertEqual(mgr.list_descendants(), entries)   # 无嵌套时 descendants == children


class TestReportTool(unittest.TestCase):
    """子作用域 report 工具对齐上游 tool-subagent-report（rc.2）：参数仅
    output、投递取部署配置、返回 {messageId} + render 一句话确认。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.persistence = JsonlPersistence(self.tmp.name)
        self.parent, self.ctx, self.reg = _parent_loop()

    def _tool(self, cid, **kwargs):
        mgr = SubagentContinuationManager(self.parent, self.persistence, **kwargs)
        return mgr, mgr._report_tool(cid)

    def test_schema_output_only_no_per_call_delivery(self):
        _, tool = self._tool("child-x")
        self.assertEqual(tool.parameters["required"], ["output"])
        self.assertEqual(set(tool.parameters["properties"]), {"output"})
        self.assertIn("Reporting does not end your turn", tool.description)

    def test_execute_returns_message_id_and_renders_confirmation(self):
        mgr, tool = self._tool("child-y")

        async def scenario():
            self.parent.start_driver()   # 运行循环内 followup 走 driver 路径
            result = await tool.execute({"output": "结论X"}, ToolExec(agent=None))
            await self.parent.when_idle_async()
            return result

        result = asyncio.run(scenario())
        self.assertTrue(result["messageId"])
        render = tool.render(result)
        self.assertEqual(render[0]["text"],
                         f"report accepted by the agent that started you as message {result['messageId']}")
        # next-step 投递唤醒 idle 父：报告作为 user/message 落父会话
        sources = [e["data"]["source"] for e in self.parent.session.events
                   if e["type"] == "user/message"]
        self.assertEqual([s["kind"] for s in sources], ["subagent-report"])
        self.assertEqual(sources[0]["senderSessionId"], "child-y")

    def test_quiet_delivery_from_config_does_not_wake_parent(self):
        mgr, tool = self._tool("child-z", report_delivery="quiet")
        asyncio.run(tool.execute({"output": "静默进展"}, ToolExec(agent=None)))
        self.assertEqual([e for e in self.parent.session.events if e["type"] == "user/message"], [])
        self.assertTrue(self.parent.when_idle())   # 未开回合


class TestControlTools(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.persistence = JsonlPersistence(self.tmp.name)
        self.parent, self.ctx, self.reg = _parent_loop(adapter=_ScriptedParent("send_message"))
        self.mgr = SubagentContinuationManager(self.parent, self.persistence)
        install_subagent_control_tools(self.ctx, self.reg, self.mgr)

    def test_send_message_tool_during_parent_run(self):
        # 父第一步发 send_message → 子跑完 settle → 父 running → 非唤醒入
        # inbox → 父下一步边界消费结算通知并回应
        self.parent.adapter.cid = self.mgr.start_continuable(label="研")
        self.parent.run("派子代理干活")
        sources = [e["data"]["source"]["kind"] for e in self.parent.session.events
                   if e["type"] == "user/message"]
        self.assertEqual(sources, ["user", "subagent-settled"])
        self.assertEqual(self.parent.last_response(), "父响应")
        tool_results = [e for e in self.parent.session.events if e["type"] == "tool/result"]
        self.assertIn("Message sent to subagent",
                      tool_results[0]["data"]["message"]["content"][0]["content"][0]["text"])
        # 子会话已完成回合并持久化
        events = self.persistence.inspect(self.parent.adapter.cid)["events"]
        self.assertEqual(events[-1]["type"], "turn/end")

    def test_list_agents_tool(self):
        self.parent.adapter = _ScriptedParent("list_agents")
        self.parent.adapter.cid = self.mgr.start_continuable(label="研")
        self.parent.run("看看有哪些子代理")
        results = [e for e in self.parent.session.events if e["type"] == "tool/result"]
        self.assertEqual(len(results), 1)
        content = results[0]["data"]["message"]["content"][0]["content"][0]["text"]
        parsed = json.loads(content)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]["kind"], "child")
        self.assertEqual(parsed[0]["label"], "研")
        self.assertEqual(parsed[0]["status"], "idle")

    def test_interrupt_agent_tool_inactive_noop(self):
        # A8：interrupt_agent 对缺省目标返回成功文案而非错误（上游 no-op）。
        self.parent.adapter = _ScriptedParent("interrupt_agent")
        self.parent.adapter.cid = self.mgr.start_continuable(label="研")
        self.parent.run("中断子代理")
        results = [e for e in self.parent.session.events if e["type"] == "tool/result"]
        self.assertEqual(len(results), 1)
        block = results[0]["data"]["message"]["content"][0]
        self.assertIn("Interrupted subagent", block["content"][0]["text"])
        self.assertFalse(block.get("isError"))


class TestAsyncContinuation(unittest.TestCase):
    """A8 异步事件驱动路径：父有 driver 时 sendMessage 投递即返回，
    子 driver 跑回合 + watcher 结算 + 投递回父。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.persistence = JsonlPersistence(self.tmp.name)
        self.parent, self.ctx, self.reg = _parent_loop()
        self.mgr = SubagentContinuationManager(self.parent, self.persistence)

    def test_async_two_submits_one_epoch_one_settlement(self):
        # 两次快速投递并入同一 residency：子跑 2 turns、父恰收 1 条结算。
        async def scenario():
            self.parent.start_driver()
            cid = self.mgr.start_continuable(label="研")
            self.mgr.send_message(cid, "第一回合")
            self.mgr.send_message(cid, "第二回合")
            await _wait_until(lambda: len(_settlement_notices(self.parent.session)) == 1)
            self.assertEqual(self.mgr.state_of(cid)["kind"], "idle")
            await self.parent.when_idle_async()
            events = self.persistence.inspect(cid)["events"]
            # 对齐上游：两次 send_message → 两条 next-turn → 各占一个 turn
            # （claim 每回合只从 next-turn 取一条），每回合 1 step
            self.assertEqual([e["type"] for e in events].count("turn/start"), 2)
            self.assertEqual([e["type"] for e in events].count("step/start"), 2)
            user_texts = [e["data"]["content"][0]["text"] for e in events
                          if e["type"] == "user/message"]
            self.assertEqual(user_texts, ["第一回合", "第二回合"])
        asyncio.run(scenario())

    def test_async_settlement_steer_while_parent_running(self):
        # 父 running（慢工具阻塞）时子结算 → steer 批内合并，下一步边界消费。
        async def scenario():
            self.parent.start_driver()
            cid = self.mgr.start_continuable(label="研")

            async def blocking_send(_args, _exec):
                self.mgr.send_message(cid, "慢着跑，我在忙")
                await asyncio.sleep(0.4)   # 父阻塞窗口：子结算期间父 running
                return "已派发"

            self.reg.register(Tool(name="do_all", description="d",
                                   parameters={"type": "object", "properties": {}, "required": []},
                                   execute=blocking_send))
            self.parent.adapter = _ScriptedParent("do_all")
            await self.parent.run_async("开动")
            self.assertEqual([e["data"]["source"]["kind"]
                              for e in self.parent.session.events if e["type"] == "user/message"],
                             ["user", "subagent-settled"])
            await self.parent.when_idle_async()
            self.assertEqual(self.parent.last_response(), "父响应")
        asyncio.run(scenario())

    def test_async_interrupt_parked_resumes_on_next_send(self):
        # 子 mid-turn 中断（keep_inbox 驻留）→ 不结算；下次 send（waking send）
        # 清 _parked 恢复驻留队列 → 跑完剩余消息 → 单次结算。
        async def scenario():
            self.parent.start_driver()
            child_adapter = FakeLlmAdapter(tool_call={"name": "cancel_self", "arguments": {}})
            mgr = SubagentContinuationManager(
                self.parent, self.persistence, adapter_factory=lambda p, m, r=None: child_adapter)
            cid = mgr.start_continuable(label="研")
            self.reg.register(Tool(
                name="cancel_self", description="d",
                parameters={"type": "object", "properties": {}, "required": []},
                execute=lambda a, e: mgr.interrupt(
                    cid, {"kind": "ancestor", "agent": mgr.parent})))
            mgr.send_message(cid, "开始")
            mgr.send_message(cid, "驻留")
            await asyncio.sleep(0.2)
            # 中断后 accepted 非空（驻留消息未认领）→ 不结算，仍 running
            self.assertEqual(mgr.state_of(cid)["kind"], "running")
            self.assertEqual(len(_settlement_notices(self.parent.session)), 0)
            mgr.send_message(cid, "继续")   # waking send 恢复
            await _wait_until(lambda: len(_settlement_notices(self.parent.session)) == 1)
            events = self.persistence.inspect(cid)["events"]
            user_texts = [e["data"]["content"][0]["text"] for e in events
                          if e["type"] == "user/message"]
            self.assertEqual(user_texts, ["开始", "驻留", "继续"])
            # 整 epoch 最后一次 turn/end 是 completed
            self.assertEqual(events[-1]["type"], "turn/end")
            self.assertEqual(events[-1]["data"]["reason"]["kind"], "completed")
        asyncio.run(scenario())

    def test_async_send_during_disposal_cold_resumes(self):
        # watcher 已结算后 send → 冷恢复新激活重投（不丢消息，新 epoch）。
        async def scenario():
            self.parent.start_driver()
            cid = self.mgr.start_continuable(label="研")
            self.mgr.send_message(cid, "第一回合")
            await _wait_until(lambda: len(_settlement_notices(self.parent.session)) == 1)
            self.mgr.send_message(cid, "第二回合")
            await _wait_until(lambda: len(_settlement_notices(self.parent.session)) == 2)
            self.assertEqual(self.mgr.state_of(cid)["kind"], "idle")
            events = self.persistence.inspect(cid)["events"]
            user_texts = [e["data"]["content"][0]["text"] for e in events
                          if e["type"] == "user/message"]
            self.assertEqual(user_texts, ["第一回合", "第二回合"])
            self.assertEqual(len(_settlement_notices(self.parent.session)), 2)
        asyncio.run(scenario())

    def test_async_drain_children_releases_selected_direct_children(self):
        # rc.2 drainChildren：exact live 父授权、直属校验（非直属 UNAUTHORIZED
        # 且收集先于任何拆除）、非驻留 id 静默跳过、选中目标强制结算。
        async def scenario():
            release = asyncio.Event()

            async def hold(_args, _exec):
                await asyncio.wait_for(release.wait(), 2.0)
                return "released"

            self.reg.register(Tool(name="hold", description="d",
                                   parameters={"type": "object", "properties": {}, "required": []},
                                   execute=hold))
            mgr = SubagentContinuationManager(
                self.parent, self.persistence,
                adapter_factory=lambda p, m, r=None: FakeLlmAdapter(tool_call={"name": "hold", "arguments": {}}))
            self.parent.start_driver()
            cid1 = mgr.start_continuable(label="甲")
            cid2 = mgr.start_continuable(label="乙")
            mgr.send_message(cid1, "跑")
            mgr.send_message(cid2, "也跑")
            await _wait_until(lambda: len(mgr.activations) == 2)
            # 第二根宿主与其直属子（真实部署中根宿主经注册表面登记）
            parent_b, _, _reg_b = _parent_loop("parent-b")
            parent_b.start_driver()
            mgr._live[parent_b.id] = parent_b
            cid_b = mgr.start_continuable(label="外", parent=parent_b)
            mgr.send_message(cid_b, "外跑", parent=parent_b)
            await _wait_until(lambda: cid_b in mgr.activations)

            with self.assertRaises(SubagentError) as cm:
                mgr.drain_children(self.parent, [cid1, cid_b])
            self.assertEqual(cm.exception.code, "UNAUTHORIZED")
            self.assertEqual(
                str(cm.exception),
                f'subagent "{cid_b}" is not a direct child of agent "{self.parent.id}"')
            self.assertIn(cid1, mgr.activations)   # 授权失败时未拆任何目标

            mgr.drain_children(parent_b, [cid_b])   # exact live 直属父放行
            mgr.drain_children(self.parent, [cid1, "child-unknown00", cid2])
            await _wait_until(lambda: not mgr.activations)
            self.assertEqual(mgr.state_of(cid1)["kind"], "idle")
            self.assertEqual(mgr.state_of(cid2)["kind"], "idle")
            self.assertEqual(mgr.state_of(cid_b)["kind"], "idle")
            release.set()
            await asyncio.sleep(0.05)
        asyncio.run(scenario())

    def test_async_child_error_settles(self):
        # 子回合抛 LlmFailure → error turn/end 闭合并结算 "failed before it finished"。
        class BoomAdapter(FakeLlmAdapter):
            async def stream(self, messages, tools, signal=None):
                raise LlmFailure("RATE_LIMIT", "429 Too Many Requests")
                yield  # pragma: no cover - 使函数成为 async 生成器（首个 __anext__ 即抛）

        async def scenario():
            self.parent.start_driver()
            mgr = SubagentContinuationManager(
                self.parent, self.persistence, adapter_factory=lambda p, m, r=None: BoomAdapter())
            cid = mgr.start_continuable(label="研")
            mgr.send_message(cid, "hi")
            await _wait_until(lambda: len(_settlement_notices(self.parent.session)) == 1)
            text = "".join(b["text"] for b in _settlement_notices(self.parent.session)[0]
                           ["data"]["content"] if b["type"] == "text")
            self.assertIn("failed before it finished.", text)
            events = self.persistence.inspect(cid)["events"]
            self.assertEqual(events[-1]["type"], "turn/end")
            self.assertEqual(events[-1]["data"]["reason"]["kind"], "error")
            self.assertEqual(events[-1]["data"]["reason"]["error"]["code"], "RATE_LIMIT")
        asyncio.run(scenario())

    def test_async_when_idle_async_roundtrip(self):
        # driver 起停、idle waiters 结算、无忙循环（超时即失败）。
        async def scenario():
            self.parent.start_driver()
            self.assertTrue(await asyncio.wait_for(self.parent.when_idle_async(), 0.5))
            self.parent.followup("动一动")
            await asyncio.wait_for(self.parent.when_idle_async(), 1.0)
            self.assertTrue(self.parent.when_idle())
            texts = [e["data"]["content"][0]["text"] for e in self.parent.session.events
                     if e["type"] == "user/message"]
            self.assertIn("动一动", texts)
        asyncio.run(scenario())

    def test_async_driver_swallows_turn_error(self):
        # driver 模式回合出错不外抛：error turn/end 仍落日志、when_idle_async 正常返回。
        class BoomAdapter(FakeLlmAdapter):
            async def stream(self, messages, tools, signal=None):
                raise LlmFailure("SERVER", "500 boom")
                yield  # pragma: no cover - 使函数成为 async 生成器（首个 __anext__ 即抛）

        async def scenario():
            parent, _, _ = _parent_loop(adapter=BoomAdapter())
            parent.start_driver()
            parent.followup("会炸的输入")
            await asyncio.wait_for(parent.when_idle_async(), 1.0)
            self.assertTrue(parent.when_idle())
            ends = [e for e in parent.session.events if e["type"] == "turn/end"]
            self.assertEqual(ends[-1]["data"]["reason"]["kind"], "error")
            self.assertEqual(ends[-1]["data"]["reason"]["error"]["code"], "SERVER")
        asyncio.run(scenario())

    def test_on_message_claimed_channel(self):
        # 每条被认领消息（含 id）都经 on_message_claimed 回调。
        async def scenario():
            parent, _, _ = _parent_loop()
            parent.start_driver()
            claimed_ids = []
            parent.on_message_claimed(lambda m: claimed_ids.append(m["id"] if m else None))
            parent.followup("消息一")
            await asyncio.wait_for(parent.when_idle_async(), 1.0)
            self.assertEqual(len(claimed_ids), 1)
            self.assertIsInstance(claimed_ids[0], str)
        asyncio.run(scenario())

    def test_fire_inbox_claimed_user_source_fix(self):
        # {"kind":"user"} 认领触发 ctx 事件 agent/inbox/claimed（对齐上游
        # agent.ts claimed 回调；tool-jobs 预算恢复经父 scope 订阅此事件）。
        async def scenario():
            parent, _, _ = _parent_loop()
            parent.start_driver()
            fired = []
            parent.ctx.on("agent/inbox/claimed", lambda p: fired.append(p))
            parent.followup("用户输入")
            await asyncio.wait_for(parent.when_idle_async(), 1.0)
            self.assertEqual(len(fired), 1)
            self.assertEqual(fired[0]["message"]["source"]["kind"], "user")
        asyncio.run(scenario())

    def test_async_report_foreground_wakeup_idle_parent(self):
        # 异步父 idle 时前台 report 唤醒新回合（_deliver → followup）。
        async def scenario():
            self.parent.start_driver()
            cid = self.mgr.start_continuable(label="研")
            self.mgr.report_from(cid, "进展报告", delivery="next-step")
            await asyncio.wait_for(self.parent.when_idle_async(), 1.0)
            self.assertEqual([e["data"]["source"]["kind"]
                              for e in self.parent.session.events if e["type"] == "user/message"],
                             ["subagent-report"])
            self.assertEqual(self.parent.last_response(), "父响应")
        asyncio.run(scenario())


class TestInjectDict(unittest.TestCase):
    def test_inject_dict_non_wakeup(self):
        loop, _, _ = _parent_loop("p1")
        message = create_message("user", [text_block("注入的预建消息")], {"kind": "user"})
        loop.inject(message)
        self.assertTrue(loop.when_idle())   # 非唤醒：不开 turn
        loop.followup("正常输入")
        texts = [b["text"] for e in loop.session.events if e["type"] == "user/message"
                 for b in e["data"]["content"] if b["type"] == "text"]
        self.assertIn("注入的预建消息", texts)
        self.assertIn("正常输入", texts)


class _DrainFixture(unittest.TestCase):
    """DRAINING / scoped dispatch 公共夹具：顶层父 P + 次级在世父 B。

    注意真实结构：AgentLoop 构造时自铸 scope（agent.py:120-121），loop.ctx 是
    dsh_scope.Scope 包装、内部 ctx 带自动铸造的 agent 标号；self.host_ctx 才是
    未打标的宿主根。"""

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        from miniharness.core.session.persistence import JsonlPersistence
        self.parent, self.host_ctx, reg = _parent_loop("P")
        self.b_loop, _, _ = _parent_loop("B")
        self.persistence = JsonlPersistence(self._tmp.name)
        self.mgr = SubagentContinuationManager(self.parent, self.persistence)
        self.mgr._live["B"] = self.b_loop

    def tearDown(self):
        self._tmp.cleanup()

    def _install_fake_activation(self, child_id, parent_loop):
        """给 durable 子会话装一枚假激活（排水测试不需要真泵）。"""
        from unittest import mock
        loop = mock.MagicMock()
        loop.id = child_id
        loop.session.events = []
        act = {
            "loop": loop, "ctx": mock.MagicMock(), "descriptor": {},
            "run_id": f"run-{child_id}", "parent_loop": parent_loop,
            "announced": False, "accepted": set(), "owned_children": set(),
            "disposal": None, "persisted": 0, "status": "running",
            "label": "", "poke": None,
        }
        self.mgr._activations[child_id] = act
        self.mgr._live[child_id] = loop
        return act


class TestDrainingAdmissionCutoff(_DrainFixture):
    def test_manager_drain_rejects_new_admissions_with_upstream_wording(self):
        cid = self.mgr.start_continuable(label="x")
        self.mgr.drain()
        with self.assertRaises(SubagentError) as caught:
            self.mgr.start_continuable(label="y")
        self.assertEqual(caught.exception.code, "DRAINING")
        self.assertEqual(
            str(caught.exception),
            "continuable subagents are draining; the operation was not admitted")
        with self.assertRaises(SubagentError) as caught_send:
            self.mgr.send_message(cid, "跟进", parent=self.parent)
        self.assertEqual(caught_send.exception.code, "DRAINING")

    def test_drain_disposes_owned_forest_child_first(self):
        parent_act = self._install_fake_activation("cpar", self.parent)
        child_act = self._install_fake_activation("ckid", parent_act["loop"])
        parent_act["owned_children"].add("ckid")
        order = []
        for cid in ("cpar", "ckid"):
            loop = self.mgr._activations[cid]["loop"]
            loop.dispose.side_effect = lambda *a, c=cid: order.append(c)
        self.mgr.drain()
        self.assertEqual(order, ["ckid", "cpar"])  # child-first（上游 disposeRoots）
        self.assertEqual(self.mgr.activations, {})
        self.assertNotIn("cpar", self.mgr._live)

    def test_scoped_drain_closes_only_that_tree_and_keeps_others_admitting(self):
        cid_a = self.mgr.start_continuable(label="a")                    # P 的子
        cid_b = self.mgr.start_continuable(label="b", parent=self.b_loop)  # B 的子
        self._install_fake_activation(cid_a, self.parent)
        self._install_fake_activation(cid_b, self.b_loop)

        self.mgr.drain_descendants([self.b_loop])
        # B 树被拆；P 的子原样保留
        self.assertNotIn(cid_b, self.mgr._activations)
        self.assertIn(cid_a, self.mgr._activations)
        # B 树准入关闭（scoped 措辞含精确根 id）；P 树照常准入
        with self.assertRaises(SubagentError) as caught:
            self.mgr.send_message(cid_b, "再投一条", parent=self.b_loop)
        self.assertEqual(caught.exception.code, "DRAINING")
        self.assertEqual(
            str(caught.exception),
            f'continuable subagents below parent "{self.b_loop.id}" are draining; '
            "the operation was not admitted")
        mid = self.mgr.send_message(cid_a, "P 树不受影响", parent=self.parent)
        self.assertTrue(mid)
        # 精确父离开注册表 → scoped 截止随之失效（上游 closingScopes 语义）
        self.mgr._live.pop("B")
        self.mgr.drain_descendants([self.b_loop])   # roots 为空 → no-op

    def test_scoped_drain_requires_exact_live_root(self):
        stale, _, _ = _parent_loop("stale")
        self.mgr.drain_descendants([stale])   # 不在注册表 → no-op，不误关
        self.assertEqual(self.mgr._closing_scopes, {})


class TestScopedLifecycleDispatch(_DrainFixture):
    def test_run_edges_carry_parent_carrier_filtering_when_tagged(self):
        from miniharness.core.dsh_scope import scope_of
        seen_agent, seen_sibling, seen_untagged = [], [], []
        # 父 loop 自带 agent 标号（构造时 create_scope 自动铸键）
        self.assertIsNotNone(scope_of(self.parent.ctx))
        sibling = self.host_ctx.create_scope("unrelated")   # 同根、无关标号
        sibling.ctx.on("subagent/start",
                       lambda info: seen_sibling.append(info))
        self.host_ctx.on("subagent/start",
                         lambda info: seen_untagged.append(info))  # 未打标 → 接纳
        self.parent.ctx.on("subagent/start",
                           lambda info: seen_agent.append(info))   # 载波键自身 → 接纳
        self.mgr._emit_lifecycle(self.parent, "subagent/start",
                                 {"runId": "r1", "provider": "fake", "id": "c1",
                                  "local": True})
        self.assertEqual(len(seen_agent), 1)
        self.assertEqual(len(seen_untagged), 1)
        self.assertEqual(seen_sibling, [])               # 无关标号 → 排除

    def test_untagged_parent_dispatches_unscoped_along_ancestor_chain(self):
        # 裸上下文父（无任何标号）→ 无载体派发：this_arg=None 走祖先链监听器
        from types import SimpleNamespace
        bare_parent = SimpleNamespace(ctx=self.host_ctx)
        seen_a, seen_b = [], []
        self.host_ctx.on("subagent/end", lambda info: seen_a.append(info))
        self.host_ctx.on("subagent/end", lambda info: seen_b.append(info))
        self.mgr._emit_lifecycle(bare_parent, "subagent/end",
                                 {"runId": "r1", "provider": "fake", "id": "c1",
                                  "local": True, "stopReason": "completed"})
        self.assertEqual(len(seen_a), 1)
        self.assertEqual(len(seen_b), 1)

    def test_listener_exception_is_contained_per_listener(self):
        seen = []
        self.parent.ctx.on("subagent/start", lambda info: 1 / 0)
        self.parent.ctx.on("subagent/start", seen.append)
        self.mgr._emit_lifecycle(self.parent, "subagent/start",
                                 {"runId": "r1", "provider": "fake", "id": "c1",
                                  "local": True})
        self.assertEqual(len(seen), 1)                 # 同侪监听器不被饿死

    def test_provider_removed_edge_fires_unscoped_on_dispose(self):
        removed = []
        self.parent.ctx.on("subagent/provider-removed", removed.append)
        adapter = FakeLlmAdapter(final_text="acme 响应")
        disposer = self.mgr.register_provider("acme", lambda model: adapter)
        resolved = self.mgr._resolve_adapter(
            {"agentProvider": "acme", "agentModel": "m1"})
        self.assertIs(resolved, adapter)               # 注册表面优先于缺省工厂
        disposer()
        self.assertEqual(removed, ["acme"])
        disposer()                                     # 幂等：不重复发布
        self.assertEqual(removed, ["acme"])
        # 注销后回退缺省工厂路径：未知名 fail loud（UNAVAILABLE）
        with self.assertRaises(SubagentError) as caught:
            self.mgr._resolve_adapter(
                {"agentProvider": "acme", "agentModel": "m1"})
        self.assertEqual(caught.exception.code, "UNAVAILABLE")


if __name__ == "__main__":
    unittest.main()
