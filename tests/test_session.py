"""第 1 章验收：事件溯源会话。运行：python -m unittest discover -s tests -t ."""

import unittest

from miniharness.session import (
    TOOL_NOT_STARTED,
    TOOL_OUTCOME_UNKNOWN,
    Session,
    create_message,
    derive_messages,
    repair_interrupted_turn,
    text_block,
    tool_call_block,
    turn_balance,
)


class TestSession(unittest.TestCase):
    def test_envelope_and_seq_continuous(self):
        s = Session("s1")
        s.append("user/message", create_message("user", [text_block("hi")]), surfaceOp="append")
        s.append("assistant/message", {
            "message": create_message("assistant", [text_block("hello")]),
        }, surfaceOp="append")
        self.assertEqual([e["seq"] for e in s.events], [0, 1])
        for e in s.events:
            self.assertEqual(set(e.keys()) & {"type", "seq", "time", "data"},
                             {"type", "seq", "time", "data"})
            self.assertEqual(e["seq"], len([x for x in s.events if x["seq"] <= e["seq"]]) - 1)

    def test_unknown_event_rejected(self):
        s = Session("s1")
        with self.assertRaises(ValueError):
            s.append("magic/event")

    def test_surface_without_op_rejected(self):
        s = Session("s1")
        with self.assertRaises(ValueError):
            s.append("user/message", create_message("user", [text_block("hi")]))

    def test_nonsurface_with_op_rejected(self):
        s = Session("s1")
        with self.assertRaises(ValueError):
            s.append("turn/start", {"turn": 1}, surfaceOp="append")

    def test_non_json_rejected(self):
        s = Session("s1")
        with self.assertRaises(TypeError):
            s.append("user/message", create_message("user", [text_block(object())]), surfaceOp="append")

    def test_events_frozen(self):
        s = Session("s1")
        s.append("user/message", create_message("user", [text_block("hi")]), surfaceOp="append")
        with self.assertRaises(TypeError):
            s.events[0]["data"] = "tampered"

    def test_derive_append(self):
        s = Session("s1")
        s.append("user/message", create_message("user", [text_block("hi")]), surfaceOp="append")
        s.append("assistant/message", {
            "message": create_message("assistant", [text_block("yo")]),
        }, surfaceOp="append")
        msgs = derive_messages(s.events)
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertEqual(dict(msgs[0]["content"][0]), {"type": "text", "text": "hi"})
        self.assertIn("id", msgs[0])

    def test_derive_replace_shadows_range(self):
        s = Session("s1")
        s.append("user/message", create_message("user", [text_block("hi")]), surfaceOp="append")
        s.append("assistant/message", {
            "message": create_message("assistant", [text_block("很长的一段旧回答")]),
        }, surfaceOp="append")
        s.append("assistant/message", {
            "message": create_message("assistant", [text_block("压缩后的摘要")]),
        }, surfaceOp={"op": "replace", "start": 1, "end": 1})
        msgs = derive_messages(s.events)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(dict(msgs[-1]["content"][0]), {"type": "text", "text": "压缩后的摘要"})

    def test_derive_empty_assistant_dropped(self):
        s = Session("s1")
        s.append("assistant/message", {
            "message": create_message("assistant", []),
        }, surfaceOp="append")
        self.assertEqual(derive_messages(s.events), [])

    def test_derive_tool_result_message(self):
        s = Session("s1")
        s.append("user/message", create_message("user", [text_block("列目录")]), surfaceOp="append")
        msg = create_message("user", [
            {"type": "tool-result", "toolCallId": "call_1",
             "content": [text_block("a.txt")]},
        ], {"kind": "tool", "callId": "call_1"})
        s.append("tool/result", {"turn": 1, "step": 1, "message": msg}, surfaceOp="append")
        msgs = derive_messages(s.events)
        # ToolResultMessage 的 role 是 'user'（上游 llm/src/message.ts）
        self.assertEqual(msgs[1]["role"], "user")
        block = msgs[1]["content"][0]
        self.assertEqual(block["type"], "tool-result")
        self.assertEqual(block["toolCallId"], "call_1")
        self.assertEqual(block["content"][0]["text"], "a.txt")

    def test_turn_balance_and_repair(self):
        s = Session("s1")
        s.append("turn/start", {"turn": 1})
        s.append("user/message", create_message("user", [text_block("hi")]), surfaceOp="append")
        self.assertEqual(turn_balance(s.events), 1)
        closers = repair_interrupted_turn(s.events)
        repaired = list(s.events) + closers
        self.assertEqual(turn_balance(repaired), 0)
        self.assertEqual(repaired[-1]["type"], "turn/end")
        self.assertEqual(repaired[-1]["data"]["reason"], {"kind": "interrupted"})
        # 时间戳复用最后真实事件
        self.assertEqual(repaired[-1]["time"], s.events[-1]["time"])

    def test_unbalanced_negative_raises(self):
        s = Session("s1")
        s.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
        with self.assertRaises(ValueError):
            turn_balance(s.events)

    def test_repair_closes_open_step_and_tool_calls(self):
        # 崩溃发生在工具执行中途：tool/call 已记录但 tool/result 未落盘
        s = Session("s1")
        s.append("turn/start", {"turn": 1})
        s.append("step/start", {"turn": 1, "step": 1})
        assistant = create_message("assistant", [
            tool_call_block("call_1", "bash", '{"cmd":"ls"}'),
        ])
        s.append("assistant/message", {"turn": 1, "step": 1, "message": assistant}, surfaceOp="append")
        s.append("tool/call", {"turn": 1, "step": 1, "callId": "call_1", "name": "bash", "arguments": '{"cmd":"ls"}'})
        closers = repair_interrupted_turn(s.events)
        self.assertEqual([c["type"] for c in closers], ["tool/result", "step/end", "turn/end"])
        # 已记录开始 → TOOL_OUTCOME_UNKNOWN
        result = closers[0]
        self.assertTrue(result["data"]["message"]["content"][0]["isError"])
        self.assertEqual(result["data"]["error"]["code"], TOOL_OUTCOME_UNKNOWN)
        self.assertEqual(result["data"]["message"]["source"]["callId"], "call_1")
        self.assertEqual(closers[0]["data"]["message"]["content"][0]["toolCallId"], "call_1")
        self.assertEqual(closers[-1]["data"]["reason"], {"kind": "interrupted"})

    def test_repair_not_started_call(self):
        # 模型请求了工具但 tool/call 尚未记录 → TOOL_NOT_STARTED
        s = Session("s1")
        s.append("turn/start", {"turn": 1})
        s.append("step/start", {"turn": 1, "step": 1})
        assistant = create_message("assistant", [
            tool_call_block("call_9", "bash", "{}"),
        ])
        s.append("assistant/message", {"turn": 1, "step": 1, "message": assistant}, surfaceOp="append")
        closers = repair_interrupted_turn(s.events)
        self.assertEqual(closers[0]["data"]["error"]["code"], TOOL_NOT_STARTED)
        self.assertNotIn("sourceEventSeqs", closers[0])

    def test_repair_balanced_log_returns_empty(self):
        s = Session("s1")
        s.append("turn/start", {"turn": 1})
        s.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
        self.assertEqual(repair_interrupted_turn(s.events), [])

    def test_seed_replay_and_end_seed_marker(self):
        s1 = Session("s1")
        s1.append("turn/start", {"turn": 1})
        s1.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
        s2 = Session("s2", seed=list(s1.events))
        self.assertEqual([e["seq"] for e in s2.events], [0, 1, 2])
        self.assertEqual(s2.events[-1]["type"], "session/end-seed")

    def test_seed_seq_contiguity_enforced(self):
        bad = [{"type": "turn/start", "seq": 5, "time": 1, "data": {"turn": 1}}]
        with self.assertRaises(ValueError):
            Session("s2", seed=bad)


if __name__ == "__main__":
    unittest.main()