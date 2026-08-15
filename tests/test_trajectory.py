"""第 10 章测试：轨迹投影折叠引擎。"""
import json
import unittest

from miniharness.bus import Context
from miniharness.llm import FakeLlmAdapter
from miniharness.loop import AgentLoop
from miniharness.session import Session
from miniharness.tools import Tool, ToolRegistry
from miniharness.trajectory import TrajectoryNode, fold_events_json, fold_trajectory


def ev(type_, data, seq, time):
    return {"type": type_, "seq": seq, "time": time, "data": data}


def simple_turn_events():
    """单文本回合：turn/start → user → assistant → step → turn/end。"""
    return [
        ev("turn/start", {"turn": 1}, 1, 1000),
        ev("step/start", {"turn": 1, "step": 1}, 2, 1005),
        ev("user/message", {"turn": 1, "step": 1,
                            "content": [{"type": "text", "text": "你好"}]}, 3, 1006),
        ev("assistant/chunk", {"turn": 1, "step": 1,
                               "chunk": {"kind": "block-start"}}, 4, 1020),
        ev("assistant/message", {"turn": 1, "step": 1, "message": {
            "content": [{"type": "text", "text": "你好，我是助手。"}]}}, 5, 1100),
        ev("step/end", {"turn": 1, "step": 1}, 6, 1105),
        ev("turn/end", {"turn": 1, "reason": {"kind": "completed"}}, 7, 1110),
    ]


def tool_roundtrip_events():
    """工具回合：assistant 带 tool-call → tool/call → tool/result。"""
    return [
        ev("turn/start", {"turn": 1}, 1, 1000),
        ev("step/start", {"turn": 1, "step": 1}, 2, 1005),
        ev("user/message", {"turn": 1, "step": 1,
                            "content": [{"type": "text", "text": "跑命令"}]}, 3, 1006),
        ev("assistant/message", {"turn": 1, "step": 1, "message": {
            "content": [
                {"type": "tool-call", "id": "call_0", "name": "bash", "arguments": "{}"},
            ]}}, 4, 1100),
        ev("tool/call", {"turn": 1, "step": 1, "callId": "call_0",
                         "name": "bash", "arguments": "{}"}, 5, 1101),
        ev("tool/result", {"turn": 1, "step": 1, "message": {
            "content": [{"type": "tool-result", "toolCallId": "call_0",
                         "content": [{"type": "text", "text": "done"}]}],
            "source": {"kind": "tool", "callId": "call_0"}}}, 6, 1150),
        ev("step/end", {"turn": 1, "step": 1}, 7, 1155),
        ev("turn/end", {"turn": 1, "reason": {"kind": "completed"}}, 8, 1160),
    ]


class TestFoldTrajectory(unittest.TestCase):
    def test_simple_turn_structure(self):
        s = fold_trajectory(simple_turn_events())
        self.assertFalse(s.partial)
        kinds = [n.kind for n in s.nodes]
        self.assertIn("turn", kinds)
        self.assertIn("user", kinds)
        self.assertIn("assistant", kinds)
        turn = next(n for n in s.nodes if n.kind == "turn")
        self.assertEqual(turn.duration_ms, 110)

    def test_messages_order_and_text(self):
        s = fold_trajectory(simple_turn_events())
        msgs = s.messages()
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["text"], "你好")
        self.assertEqual(msgs[1]["role"], "assistant")
        self.assertEqual(msgs[1]["text"], "你好，我是助手。")
        self.assertEqual(s.last_assistant_text(), "你好，我是助手。")

    def test_turn_summary_with_ttft(self):
        s = fold_trajectory(simple_turn_events())
        t = s.turns[0]
        self.assertEqual(t["turn"], 1)
        self.assertEqual(t["user_texts"], ["你好"])
        self.assertEqual(t["assistant_texts"], ["你好，我是助手。"])
        self.assertEqual(t["tool_calls"], 0)
        self.assertEqual(t["ttft_ms"], 20)   # 1020 - 1000

    def test_tool_roundtrip_builds_call_tree(self):
        s = fold_trajectory(tool_roundtrip_events())
        call = next(n for n in s.nodes if n.kind == "tool-call")
        self.assertEqual(call.call_id, "call_0")
        results = [n for n in s.nodes if n.kind == "tool-result"]
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].parent_id, call.id)
        self.assertEqual(len(call.children), 1)
        t = s.turns[0]
        self.assertEqual(t["tool_calls"], 1)

    def test_unclosed_turn_is_partial(self):
        events = simple_turn_events()[:-1]   # 去掉 turn/end（崩溃尾部）
        s = fold_trajectory(events)
        self.assertTrue(s.partial)
        turn = next(n for n in s.nodes if n.kind == "turn")
        self.assertIsNone(turn.duration_ms)

    def test_first_seq_filters_prefix(self):
        events = simple_turn_events()
        s = fold_trajectory(events, first_seq=3)   # 跳过 turn/start 与 step/start
        self.assertEqual(s.turns, [])              # 无完整 turn 摘要（turn/start 被过滤）
        # 但 user/assistant 消息仍在
        self.assertGreater(len([n for n in s.nodes if n.kind == "assistant"]), 0)

    def test_fold_events_json_serializes(self):
        s = fold_trajectory(simple_turn_events())
        raw = fold_events_json(simple_turn_events())
        data = json.loads(raw)
        self.assertEqual(data["partial"], s.partial)
        self.assertEqual(len(data["turns"]), 1)
        self.assertEqual(data["turns"][0]["ttft_ms"], 20)


class TestFoldFromRealLoop(unittest.TestCase):
    def test_fold_after_real_turn(self):
        ctx = Context(name="root")
        registry = ToolRegistry(ctx)
        loop = AgentLoop(Session("real"), FakeLlmAdapter(), registry, ctx)
        loop.run("你好")
        s = fold_trajectory(loop.session.events)
        self.assertFalse(s.partial)
        kinds = [n.kind for n in s.nodes]
        self.assertIn("turn", kinds)
        self.assertIn("user", kinds)
        self.assertIn("assistant", kinds)
        self.assertEqual(len(s.turns), 1)
        self.assertGreaterEqual(s.turns[0]["assistant_texts"], ["任务完成。"])

    def test_fold_two_followups_two_turns(self):
        ctx = Context(name="root")
        registry = ToolRegistry(ctx)
        loop = AgentLoop(Session("multi"), FakeLlmAdapter(), registry, ctx)
        loop.followup("第一轮")
        loop.followup("第二轮")
        s = fold_trajectory(loop.session.events)
        self.assertEqual(len(s.turns), 2)


if __name__ == "__main__":
    unittest.main()