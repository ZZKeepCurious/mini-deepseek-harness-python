"""A7/A8 可继续子代理验收：durable 子会话 + 冷恢复 + 结算投递 + 枚举 + 控制工具。

A8：异步事件驱动执行（父有 driver 时 sendMessage 投递即返回、residency 跨回合、
watchSettlement 结算、steer 批内合并、interrupt 缺省 no-op）。

运行：python -m unittest tests.test_continuation -v
"""
import asyncio
import json
import tempfile
import time
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import Session, create_message, text_block
from miniharness.core.session.persistence import JsonlPersistence, SqlitePersistence
from miniharness.core.tools import Tool, ToolRegistry
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

    def stream(self, messages, tools):
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
    def test_snapshot_and_parse_roundtrip(self):
        descriptor = {"kind": "continuable", "mode": "continuable", "label": "研"}
        snapshot = snapshot_subagent_descriptor(descriptor)
        self.assertEqual(snapshot["version"], SUBAGENT_DESCRIPTOR_VERSION)
        self.assertEqual(parse_subagent_descriptor(snapshot), snapshot)

    def test_parse_fail_closed(self):
        self.assertIsNone(parse_subagent_descriptor({}))
        self.assertIsNone(parse_subagent_descriptor(
            {"version": 1, "kind": "continuable", "mode": "continuable"}))
        self.assertIsNone(parse_subagent_descriptor(
            {"version": 2, "kind": "fork", "mode": "continuable"}))
        self.assertIsNone(parse_subagent_descriptor(
            {"version": 2, "kind": "continuable", "mode": "background"}))
        self.assertIsNone(parse_subagent_descriptor(
            {"version": 2, "kind": "continuable", "mode": "continuable",
             "toolFilter": "not-a-list"}))
        self.assertIsNone(parse_subagent_descriptor("junk"))
        self.assertIsNone(parse_subagent_descriptor(None))

    def test_fold_first_authoritative(self):
        session = Session("c1")
        seed_descriptor_turn(session, {"kind": "continuable", "mode": "continuable", "label": "a"})
        folded = fold_subagent_descriptor(session.events)
        self.assertEqual(folded["label"], "a")
        self.assertEqual(folded["version"], SUBAGENT_DESCRIPTOR_VERSION)
        self.assertIsNone(fold_subagent_descriptor([]))
        seed_descriptor_turn(session, {"kind": "continuable", "mode": "continuable", "label": "b"})
        self.assertIsNone(fold_subagent_descriptor(session.events))


class TestSettlementHelpers(unittest.TestCase):
    def test_epoch_stop_reason_mapping(self):
        def turn(kind):
            return {"type": "turn/end", "data": {"reason": {"kind": kind}}}
        self.assertEqual(epoch_stop_reason([turn("completed")]), "completed")
        self.assertEqual(epoch_stop_reason([turn("aborted")]), "aborted")
        self.assertEqual(epoch_stop_reason([turn("interrupted")]), "aborted")
        self.assertEqual(epoch_stop_reason([turn("blocked")]), "refusal")
        self.assertEqual(epoch_stop_reason([turn("max-tokens")]), "max-tokens")
        self.assertEqual(epoch_stop_reason([turn("error")]), "error")
        self.assertEqual(epoch_stop_reason([turn("bogus")]), "error")
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
        persistence.declare("c1", {"parentSession": "p1", "origin": "subagent",
                                   "delegationDepth": 1, "label": "研"})
        persistence.declare("c1", {"parentSession": "p1"})  # 幂等：第二次忽略
        session = Session("c1")
        session.append("subagent/descriptor", {"version": 2, "kind": "continuable",
                                               "mode": "continuable"})
        for ev in session.events:
            persistence.append("c1", ev)
        persistence.flush()
        info = persistence.inspect("c1")
        self.assertEqual(info["meta"]["parentSession"], "p1")
        self.assertEqual(info["meta"]["label"], "研")
        self.assertEqual(info["events"][0]["type"], "subagent/descriptor")
        headers = persistence.list_headers()
        self.assertEqual([h["id"] for h in headers], ["c1"])
        self.assertEqual(headers[0]["meta"]["delegationDepth"], 1)
        self.assertIsNone(persistence.inspect("nope")["meta"])
        self.assertEqual(persistence.inspect("nope")["events"], [])

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
        # 创建即落盘：header meta + 描述符事件（无需任何运行）
        info = self.persistence.inspect(cid)
        self.assertEqual(info["meta"]["parentSession"], "parent")
        self.assertEqual(info["meta"]["origin"], "subagent")
        self.assertEqual(info["meta"]["delegationDepth"], 1)
        self.assertEqual(info["meta"]["label"], "研")
        self.assertEqual([e["type"] for e in info["events"]], ["subagent/descriptor"])
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
        def factory(provider, model):
            factory_calls.append((provider, model))
            return FakeLlmAdapter(final_text="重建后的子代理")
        mgr = self._manager(adapter_factory=factory)
        cid = mgr.start_continuable(label="研")
        mgr.send_message(cid, "第一问")
        # 新管理器 + 同一持久化 → 冷恢复；adapter 经 factory 重建
        mgr2 = SubagentContinuationManager(self.parent, self.persistence, adapter_factory=factory)
        mgr2.send_message(cid, "第二问")
        self.assertTrue(any(p == "fake" for p, _ in factory_calls))
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
            self.assertEqual(mgr.state_of(cid)["kind"], "running")
        finally:
            activation["ctx"].dispose()
            mgr._activations.pop(cid, None)

    def test_child_error_settles_without_raising(self):
        class BoomAdapter(FakeLlmAdapter):
            def stream(self, messages, tools):
                raise LlmFailure("RATE_LIMIT", "429 Too Many Requests")

        mgr = self._manager(adapter_factory=lambda p, m: BoomAdapter())
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
        cancel_tool = Tool(name="cancel_self", description="d",
                           execute=lambda a, e: mgr.interrupt(cid, cause="user"))
        # 注入子工具：子注册表复制父全局工具；这里注册到父 → 复制进子
        self.reg.register(cancel_tool)
        child_adapter = FakeLlmAdapter(tool_call={"name": "cancel_self", "arguments": {}})
        mgr = self._manager(adapter_factory=lambda p, m: child_adapter)
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
        self.assertIsNone(mgr.interrupt(cid))
        self.assertEqual(mgr.state_of(cid)["kind"], "idle")

    def test_max_depth(self):
        mgr = self._manager(max_depth=0)
        with self.assertRaises(SubagentError) as cm:
            mgr.start_continuable(label="研")
        self.assertEqual(cm.exception.code, "MAX_DEPTH_EXCEEDED")

    def test_report_wakeup_and_quiet(self):
        mgr = self._manager()
        cid = mgr.start_continuable(label="研")
        # 前台：父 idle → 唤醒
        mgr.report_from(cid, "进展1", quiet=False)
        self.assertEqual([e["data"]["source"]["kind"] for e in self.parent.session.events
                          if e["type"] == "user/message"], ["subagent-report"])
        # 后台：不唤醒，等下一次 pump
        mgr.report_from(cid, "进展2", quiet=True)
        self.assertEqual(len([e for e in self.parent.session.events if e["type"] == "user/message"]), 1)
        self.parent.followup("再来一轮")
        sources = [e["data"]["source"]["kind"] for e in self.parent.session.events
                   if e["type"] == "user/message"]
        self.assertIn("subagent-report", sources)
        quiet = [e for e in self.parent.session.events if e["type"] == "user/message"
                 and e["data"]["source"]["kind"] == "subagent-report"]
        self.assertEqual(quiet[-1]["data"]["source"]["form"], "background-report")

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
        self.assertEqual(mgr.list_descendants(), entries)   # mini：descendants == children


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

            def blocking_send(_args, _exec):
                self.mgr.send_message(cid, "慢着跑，我在忙")
                time.sleep(0.4)   # 父阻塞窗口：子结算期间父 running
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
                self.parent, self.persistence, adapter_factory=lambda p, m: child_adapter)
            cid = mgr.start_continuable(label="研")
            self.reg.register(Tool(name="cancel_self", description="d",
                                   parameters={"type": "object", "properties": {}, "required": []},
                                   execute=lambda a, e: mgr.interrupt(cid, cause="parent")))
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

    def test_async_child_error_settles(self):
        # 子回合 LlmFailure → error turn/end → 结算 "failed before it finished"。
        class BoomAdapter(FakeLlmAdapter):
            def stream(self, messages, tools):
                raise LlmFailure("RATE_LIMIT", "429 Too Many Requests")

        async def scenario():
            self.parent.start_driver()
            mgr = SubagentContinuationManager(
                self.parent, self.persistence, adapter_factory=lambda p, m: BoomAdapter())
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
        # driver 模式回合错误被吞（error turn/end 在日志），when_idle_async 正常返回。
        class BoomAdapter(FakeLlmAdapter):
            def stream(self, messages, tools):
                raise LlmFailure("SERVER", "500 boom")

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
        # {"kind":"user"} 消息触发零参钩子（A8 修复 source 比较后）。
        async def scenario():
            parent, _, _ = _parent_loop()
            parent.start_driver()
            fired = []
            parent.on_inbox_claimed(lambda _loop: fired.append(True))
            parent.followup("用户输入")
            await asyncio.wait_for(parent.when_idle_async(), 1.0)
            self.assertEqual(fired, [True])
        asyncio.run(scenario())

    def test_async_report_foreground_wakeup_idle_parent(self):
        # 异步父 idle 时前台 report 唤醒新回合（_deliver → followup）。
        async def scenario():
            self.parent.start_driver()
            cid = self.mgr.start_continuable(label="研")
            self.mgr.report_from(cid, "进展报告", quiet=False)
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


if __name__ == "__main__":
    unittest.main()
