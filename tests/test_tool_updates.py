"""动态工具更新 + 失败步恢复验收（上游 rc.2：ToolHistory / projectToolUpdates /
ToolCallRecovery）。运行：python -m unittest discover -s tests -t ."""

import asyncio
import unittest

from miniharness.core.scope import Context
from miniharness.llm import FakeLlmAdapter
from miniharness.llm.content import project_tool_updates
from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.session import (
    Session,
    TOOL_NOT_STARTED,
    TOOL_OUTCOME_UNKNOWN,
    ToolCallRecovery,
    ToolHistoryProjection,
    create_message,
    developer_message,
    tool_addition_block,
    tool_call_block,
    tool_removal_block,
    turn_balance,
)
from miniharness.core.tools import Tool, ToolRegistry


def _tool(name):
    return {"name": name, "description": f"{name} tool",
            "parameters": {"type": "object", "properties": {}}}


def _header_event(seq, tools, reason="initial", starts_series=False):
    data = {"header": {"config": {}, "tools": tools}, "reason": reason}
    if starts_series:
        data["startsSeries"] = True
    return {"type": "request/header", "seq": seq, "time": 1, "data": data}


def _developer_event(seq, message_id, content, header_seq=None):
    data = {"turn": 1, "step": 1,
            "message": {"id": message_id, "role": "developer",
                        "content": content, "source": {"kind": "tool-registry"}}}
    if header_seq is not None:
        data["headerSeq"] = header_seq
    return {"type": "developer/message", "seq": seq, "time": 1, "data": data}


class TestToolHistoryProjection(unittest.TestCase):
    def test_folds_additions_from_referenced_header(self):
        proj = ToolHistoryProjection()
        proj.apply(_header_event(0, [_tool("a"), {**_tool("b"), "deferLoading": True}]))
        proj.apply(_developer_event(
            1, "m1", [tool_addition_block("b")], header_seq=0))
        snap = proj.snapshot()
        # tools 保持 baseline 声明（含延后的 b），additions 按序解析
        self.assertEqual([t["name"] for t in snap["tools"]], ["a", "b"])
        self.assertEqual(len(snap["updates"]), 1)
        self.assertEqual(snap["updates"][0]["messageId"], "m1")
        self.assertEqual([t["name"] for t in snap["updates"][0]["additions"]], ["b"])

    def test_snapshot_is_stable_across_new_events(self):
        proj = ToolHistoryProjection()
        proj.apply(_header_event(0, [_tool("a"), {**_tool("b"), "deferLoading": True}]))
        first = proj.snapshot()
        proj.apply(_developer_event(1, "m1", [tool_addition_block("b")], header_seq=0))
        # 之前返回的快照不被后续事件改写
        self.assertEqual(first["updates"], [])

    def test_reset_on_series_or_starts_series(self):
        proj = ToolHistoryProjection()
        proj.apply(_header_event(0, [_tool("a"), {**_tool("b"), "deferLoading": True}]))
        proj.apply(_developer_event(1, "m1", [tool_addition_block("b")], header_seq=0))
        proj.apply(_header_event(2, [_tool("a"), _tool("b")], reason="series"))
        snap = proj.snapshot()
        self.assertEqual([t["name"] for t in snap["tools"]], ["a", "b"])
        self.assertEqual(snap["updates"], [])

    def test_redeclared_name_resets_series(self):
        proj = ToolHistoryProjection()
        proj.apply(_header_event(0, [_tool("a")]))
        proj.apply(_header_event(1, [{"name": "a", "description": "changed",
                                      "parameters": {"type": "object", "properties": {}}}]))
        snap = proj.snapshot()
        self.assertEqual(snap["tools"][0]["description"], "changed")
        self.assertEqual(snap["updates"], [])

    def test_missing_definition_raises(self):
        proj = ToolHistoryProjection()
        proj.apply(_header_event(0, [_tool("a")]))
        with self.assertRaises(ValueError):
            proj.apply(_developer_event(1, "m1", [tool_addition_block("zzz")], header_seq=0))

    def test_fallback_when_active_has_no_update_record(self):
        # header 增加了 b 但没有匹配的 developer 更新 → 回退为当前声明、无更新
        proj = ToolHistoryProjection()
        proj.apply(_header_event(0, [_tool("a")]))
        proj.apply(_header_event(1, [_tool("a"), _tool("b")]))
        snap = proj.snapshot()
        self.assertEqual([t["name"] for t in snap["tools"]], ["a", "b"])
        self.assertEqual(snap["updates"], [])


class TestProjectToolUpdates(unittest.TestCase):
    def _messages(self):
        dev = developer_message([tool_addition_block("b")])
        dev["id"] = "m1"
        return [
            {"id": "u1", "role": "user", "content": [{"type": "text", "text": "hi"}]},
            dev,
            {"id": "x1", "role": "user", "content": [{"type": "text", "text": "next"}]},
        ]

    def _history(self):
        return {"tools": [_tool("a")],
                "updates": [{"messageId": "m1", "additions": [_tool("b")]}]}

    def test_absent_mode_strips_developer_and_deferloading(self):
        tools = [{"name": "a", "description": "d", "parameters": {},
                  "deferLoading": True}]
        out = project_tool_updates(self._messages(), tools, None)
        self.assertEqual([m["role"] for m in out["messages"]], ["user", "user"])
        self.assertNotIn("deferLoading", out["tools"][0])

    def test_missing_history_strips_developer(self):
        out = project_tool_updates(self._messages(), [_tool("a")], "in-history", None)
        self.assertEqual([m["role"] for m in out["messages"]], ["user", "user"])

    def test_prefix_missing_update_id_strips_developer(self):
        messages = [self._messages()[0]]  # 只有 user，无 developer
        out = project_tool_updates(messages, [_tool("a")], "addition-only", self._history())
        self.assertEqual([m["role"] for m in out["messages"]], ["user"])

    def test_in_history_keeps_additions_and_removals(self):
        dev = developer_message([tool_addition_block("b")])
        dev["id"] = "m1"
        messages = [dev]
        history = self._history()
        out = project_tool_updates(messages, [_tool("a")], "in-history", history)
        # addition 保留，声明表含 a + 延后的 b
        self.assertEqual(len(out["messages"]), 1)
        names = [t["name"] for t in out["tools"]]
        self.assertIn("b", names)
        # a 是 baseline 非延后

    def test_addition_only_drops_removal_and_inactive(self):
        dev = developer_message([tool_removal_block("a")])
        dev["id"] = "m1"
        history = {"tools": [_tool("a"), _tool("b")], "updates": []}
        out = project_tool_updates([dev], [_tool("b")], "addition-only", history)
        # removal 被丢弃 → developer 消息无内容 → 整条去掉
        self.assertEqual(out["messages"], [])
        # addition-only：声明表只留当前活动工具 b
        self.assertEqual([t["name"] for t in out["tools"]], ["b"])

    def test_unknown_mode_raises(self):
        with self.assertRaises(ValueError):
            project_tool_updates([], [], "bogus", {"tools": [], "updates": []})


class TestSessionToolHistory(unittest.TestCase):
    def test_session_folds_live_events(self):
        s = Session("s1")
        s.append("request/header", {"header": {"config": {}, "tools": [
            _tool("a"), {**_tool("b"), "deferLoading": True}]}, "reason": "initial"})
        snap = s.tool_history()
        self.assertEqual([t["name"] for t in snap["tools"]], ["a", "b"])
        dev = developer_message([tool_addition_block("b")])
        s.append("developer/message", {"turn": 1, "step": 1, "message": dev,
                                       "headerSeq": 0}, surfaceOp="append")
        snap2 = s.tool_history()
        self.assertEqual(len(snap2["updates"]), 1)
        self.assertEqual([t["name"] for t in snap2["updates"][0]["additions"]], ["b"])


class TestToolCallRecovery(unittest.TestCase):
    def _events(self):
        s = Session("s1")
        s.append("turn/start", {"turn": 1})
        s.append("step/start", {"turn": 1, "step": 1})
        assistant = create_message("assistant", [tool_call_block("c1", "bash", "{}")])
        s.append("assistant/message", {"turn": 1, "step": 1, "message": assistant},
                 surfaceOp="append")
        s.append("tool/call", {"turn": 1, "step": 1, "callId": "c1", "name": "bash",
                               "arguments": "{}"})
        return s

    def test_results_started_call_is_outcome_unknown(self):
        s = self._events()
        rec = ToolCallRecovery()
        for e in s.events:
            rec.observe(e)
        results = rec.results()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["data"]["error"]["code"], TOOL_OUTCOME_UNKNOWN)
        self.assertEqual(results[0]["sourceEventSeqs"], [3])

    def test_results_not_started_call(self):
        s = Session("s1")
        s.append("turn/start", {"turn": 1})
        s.append("step/start", {"turn": 1, "step": 1})
        assistant = create_message("assistant", [tool_call_block("c9", "bash", "{}")])
        s.append("assistant/message", {"turn": 1, "step": 1, "message": assistant},
                 surfaceOp="append")
        rec = ToolCallRecovery()
        for e in s.events:
            rec.observe(e)
        results = rec.results()
        self.assertEqual(results[0]["data"]["error"]["code"], TOOL_NOT_STARTED)
        self.assertNotIn("sourceEventSeqs", results[0])

    def test_results_is_idempotent(self):
        s = self._events()
        rec = ToolCallRecovery()
        for e in s.events:
            rec.observe(e)
        self.assertEqual(len(rec.results()), 1)
        self.assertEqual(len(rec.results()), 1)

    def test_other_step_result_does_not_acknowledge(self):
        s = self._events()
        # 同 id 的结果落在另一个 step：不确认（上游 repair.ts:139-140）
        s.append("step/end", {"turn": 1, "step": 1})
        s.append("step/start", {"turn": 1, "step": 2})
        s.append("tool/result", {"turn": 1, "step": 2,
                                 "message": {"id": "x", "role": "tool", "toolCallId": "c1",
                                             "content": [{"type": "text", "text": "ok"}],
                                             "source": {"kind": "tool", "callId": "c1"}}},
                 surfaceOp="append", sourceEventSeqs=[3])
        rec = ToolCallRecovery()
        for e in s.events:
            rec.observe(e)
        # step/end 已经清空 pending，恢复无结果
        self.assertEqual(rec.results(), [])


class TestLiveToolUpdates(unittest.TestCase):
    def _env(self, extra_tool_holder):
        session = Session("s1")
        ctx = Context()
        reg = ToolRegistry(ctx)
        extra = Tool(name="extra", description="Extra tool.",
                     parameters={"type": "object", "properties": {}},
                     execute=lambda a, e: "ok")

        def bash_exec(args, e):
            reg.register(extra)
            extra_tool_holder.append(extra)
            return "stdout"

        reg.register(Tool(name="bash", description="Run.",
                          parameters={"type": "object",
                                      "properties": {"cmd": {"type": "string"}}},
                          execute=bash_exec))
        adapter = FakeLlmAdapter(tool_call={"name": "bash", "arguments": {"cmd": "ls"}},
                                 final_text="done")
        loop = AgentLoop(session, adapter, reg, ctx)
        return session, loop

    def test_developer_message_emitted_on_tool_change(self):
        holder = []
        session, loop = self._env(holder)
        loop.followup("go")
        devs = [e for e in session.events if e["type"] == "developer/message"]
        self.assertEqual(len(devs), 1)
        content = devs[0]["data"]["message"]["content"]
        self.assertEqual([b["type"] for b in content], ["tool-addition"])
        self.assertEqual([b["toolName"] for b in content], ["extra"])
        # headerSeq 指向一条更早的 request/header
        header_seq = devs[0]["data"]["headerSeq"]
        self.assertEqual(session.event_at(header_seq)["type"], "request/header")
        # 工具历史折叠出该新增
        hist = session.tool_history()
        self.assertEqual([t["name"] for t in hist["tools"]], ["bash"])
        self.assertEqual(len(hist["updates"]), 1)


class TestLiveStepRecovery(unittest.TestCase):
    def test_failed_step_records_pending_tool_results(self):
        session = Session("s1")
        ctx = Context()
        reg = ToolRegistry(ctx)
        reg.register(Tool(name="bash", description="Run.",
                          parameters={"type": "object",
                                      "properties": {"cmd": {"type": "string"}}},
                          execute=lambda a, e: "stdout"))
        # 政策段抛错 → 调度器失败（tool/call 已记录、无 tool/result）
        ctx.on("tools/pre-execute", lambda p, nxt: (_ for _ in ()).throw(
            RuntimeError("policy boom")))
        adapter = FakeLlmAdapter(tool_call={"name": "bash", "arguments": {"cmd": "ls"}},
                                 final_text="done")
        loop = AgentLoop(session, adapter, reg, ctx)
        with self.assertRaises(Exception):
            loop.followup("go")
        types = [e["type"] for e in session.events]
        self.assertIn("tool/call", types)
        # 恢复补出保守 error 结果（已记录开始 → TOOL_OUTCOME_UNKNOWN）
        results = [e for e in session.events if e["type"] == "tool/result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["data"]["error"]["code"], TOOL_OUTCOME_UNKNOWN)
        # 恢复结果先于 step/end
        self.assertLess(types.index("tool/result"), types.index("step/end"))
        self.assertEqual(types[-1], "turn/end")
        self.assertEqual(session.events[-1]["data"]["reason"]["kind"], "error")
        self.assertEqual(turn_balance(session.events), 0)


if __name__ == "__main__":
    unittest.main()
