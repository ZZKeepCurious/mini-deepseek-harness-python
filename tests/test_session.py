"""第 1 章验收：事件溯源会话。运行：python -m unittest discover -s tests -t ."""

import unittest

from miniharness.session import (
    Session,
    derive_messages,
    repair_interrupted_turn,
    turn_balance,
)


class TestSession(unittest.TestCase):
    def test_seq_continuous(self):
        s = Session("s1")
        s.append({"type": "user/message", "content": "hi", "surfaceOp": "append"})
        s.append({"type": "assistant/message", "content": "hello", "surfaceOp": "append"})
        self.assertEqual([e["seq"] for e in s.events], [0, 1])

    def test_unknown_event_rejected(self):
        s = Session("s1")
        with self.assertRaises(ValueError):
            s.append({"type": "magic/event"})

    def test_surface_without_op_rejected(self):
        s = Session("s1")
        with self.assertRaises(ValueError):
            s.append({"type": "user/message", "content": "hi"})

    def test_non_json_rejected(self):
        s = Session("s1")
        with self.assertRaises(TypeError):
            s.append({"type": "user/message", "content": object(), "surfaceOp": "append"})

    def test_events_frozen(self):
        s = Session("s1")
        s.append({"type": "user/message", "content": "hi", "surfaceOp": "append"})
        with self.assertRaises(TypeError):
            s.events[0]["content"] = "tampered"

    def test_derive_append(self):
        s = Session("s1")
        s.append({"type": "user/message", "content": "hi", "surfaceOp": "append"})
        s.append({"type": "assistant/message", "content": "yo", "surfaceOp": "append"})
        self.assertEqual(derive_messages(s.events), [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "yo"},
        ])

    def test_derive_replace_compresses(self):
        s = Session("s1")
        s.append({"type": "user/message", "content": "hi", "surfaceOp": "append"})
        s.append({"type": "assistant/message", "content": "很长的一段旧回答", "surfaceOp": "append"})
        s.append({"type": "assistant/message", "content": "压缩后的摘要", "surfaceOp": "replace"})
        msgs = derive_messages(s.events)
        self.assertEqual(len(msgs), 2)
        self.assertEqual(msgs[-1]["content"], "压缩后的摘要")

    def test_derive_tool_result(self):
        s = Session("s1")
        s.append({"type": "user/message", "content": "列目录", "surfaceOp": "append"})
        s.append({"type": "tool/result", "name": "bash", "content": "a.txt", "isError": False, "surfaceOp": "append"})
        msgs = derive_messages(s.events)
        self.assertEqual(msgs[1]["role"], "tool")
        self.assertIn("a.txt", msgs[1]["content"])

    def test_turn_balance_and_repair(self):
        s = Session("s1")
        s.append({"type": "turn/start"})
        s.append({"type": "user/message", "content": "hi", "surfaceOp": "append"})
        self.assertEqual(turn_balance(s.events), 1)
        repaired = repair_interrupted_turn(s.events)
        self.assertEqual(turn_balance(repaired), 0)
        self.assertEqual(repaired[-1]["type"], "turn/end")
        self.assertEqual(repaired[-1]["reason"], "interrupted")

    def test_unbalanced_negative_raises(self):
        s = Session("s1")
        s.append({"type": "turn/end"})
        with self.assertRaises(ValueError):
            turn_balance(s.events)


if __name__ == "__main__":
    unittest.main()