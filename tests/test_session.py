"""第 1 章验收：事件溯源会话。运行：python -m unittest discover -s tests -t ."""

import unittest

from miniharness.core.session import (
    TOOL_NOT_STARTED,
    TOOL_OUTCOME_UNKNOWN,
    Session,
    create_message,
    derive_messages,
    repair_interrupted_turn,
    text_block,
    tool_call_block,
    tool_result_block,
    turn_balance,
)
from miniharness.llm import AssistantStreamAccumulator, BlockAssembler


def _embedded_assistant_data(text: str) -> dict:
    """构造 V2 自洽的 assistant/message data：message.content 与内嵌 stream
    逐块一致（seed 恢复边界 assertCurrentAssistantStream 校验三事实）。"""
    accumulator = AssistantStreamAccumulator()
    assembler = BlockAssembler()
    chunks = [
        {"type": "block-start", "index": 0, "blockType": "text"},
        {"type": "text-delta", "index": 0, "text": text},
        {"type": "block-end", "index": 0, "block": {"type": "text", "text": text}},
        {"type": "finish", "reason": {"kind": "stop"}},
    ]
    for time, chunk in enumerate(chunks, start=1):
        accumulator.push_chunk_time(time, chunk)
        assembler.push(chunk)
    return {
        "turn": 1, "step": 1,
        "message": create_message("assistant", assembler.blocks, {"kind": "model"}),
        "stream": accumulator.snapshot(),
    }


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
        # V2：assistant/message 内嵌源流、不可携带 sourceEventSeqs；压缩检查点
        # 改由 user/message 承载（上游 compaction region.ts 检查点形状）
        s.append("user/message", create_message("user", [text_block("压缩后的摘要")]),
                 surfaceOp={"op": "replace", "start": 1, "end": 1}, sourceEventSeqs=[1])
        msgs = derive_messages(s.events)
        self.assertEqual(len(msgs), 2)
        self.assertEqual([m["role"] for m in msgs], ["user", "user"])
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

    def test_seed_seq_rejects_negative_zero_float(self):
        # rc.1: seq 必须是整数严谨等序；-0.0 与 0 相等但非法
        bad = [{"type": "turn/start", "seq": -0.0, "time": 1, "data": {"turn": 1}}]
        with self.assertRaises(ValueError):
            Session("s2", seed=bad)

    def test_seed_assistant_stream_missing_is_corrupt(self):
        # V2: assistant/message 必须内嵌 stream；缺失 = 损坏制品（上游
        # expandAssistantStream(undefined) 抛错同形；§2.22 读端严格性收口）
        ev = {"type": "assistant/message", "seq": 0, "time": 1, "surfaceOp": "append",
              "data": {"turn": 1, "step": 1, "message": {
                  "id": "m1", "role": "assistant", "content": [],
                  "source": {"kind": "model", "provider": "fake", "model": "fake"}}}}
        with self.assertRaises(ValueError):
            Session("s2", seed=[ev])

    def test_seed_assistant_stream_empty_list_is_legal(self):
        ev = {"type": "assistant/message", "seq": 0, "time": 1, "surfaceOp": "append",
              "data": {"turn": 1, "step": 1, "stream": [], "message": {
                  "id": "m1", "role": "assistant", "content": [],
                  "source": {"kind": "model", "provider": "fake", "model": "fake"}}}}
        s2 = Session("s2", seed=[ev])
        self.assertEqual(s2.events[0]["type"], "assistant/message")

    def test_thaw_descends_fresh_lists_holding_frozen_items(self):
        # 冻结结构 list→tuple、dict→mappingproxy；但实时代码会在冻结值外层套
        # 新建 list（web 流把 splice 的 inserted 重投影进新 items 数组），
        # thaw 必须能下钻普通 list 解内层冻结项（回归：曾漏掉 list 分支）。
        import json

        from miniharness.core.session import deep_freeze, thaw

        frozen = deep_freeze({"items": [{"message": {"content": [{"type": "text", "text": "hi"}],
                                          "source": {"kind": "user"}}}]})
        frame = {"type": "session/queue", "items": list(frozen["items"])}
        plain = thaw(frame)
        self.assertEqual(plain, {"type": "session/queue", "items": [
            {"message": {"content": [{"type": "text", "text": "hi"}], "source": {"kind": "user"}}}]})
        json.dumps(plain, ensure_ascii=False)


class TestSurfaceValidation(unittest.TestCase):
    """T1-1：surface 校验深度（上游 surface.ts surfaceOpOf / assertProvenance /
    assertToolResultRewrite）。"""

    def _session(self):
        s = Session("sv")
        s.append("user/message", create_message("user", [text_block("hi")]), surfaceOp="append")
        return s

    def test_replace_without_source_event_seqs_rejected(self):
        s = self._session()
        with self.assertRaises(ValueError):
            s.append("assistant/message", {
                "message": create_message("assistant", [text_block("sum")]),
            }, surfaceOp={"op": "replace", "start": 0, "end": 0})

    def test_replace_missing_shadowed_seq_rejected(self):
        s = self._session()
        s.append("assistant/message", {
            "message": create_message("assistant", [text_block("a")]),
        }, surfaceOp="append")
        # V2：assistant/message 禁带 sourceEventSeqs，血统校验改经 user/message
        # sourceEventSeqs 未覆盖被遮蔽的 seq 0
        with self.assertRaises(ValueError):
            s.append("user/message", create_message("user", [text_block("sum")]),
                     surfaceOp={"op": "replace", "start": 0, "end": 1}, sourceEventSeqs=[1])

    def test_replace_source_event_seqs_earlier_required(self):
        s = self._session()
        with self.assertRaises(ValueError):
            s.append("user/message", create_message("user", [text_block("sum")]),
                     surfaceOp={"op": "replace", "start": 0, "end": 0},
                     sourceEventSeqs=[0, 3])  # seq 3 >= 当前 seq 1

    def test_replace_source_event_seqs_duplicate_rejected(self):
        s = self._session()
        with self.assertRaises(ValueError):
            s.append("user/message", create_message("user", [text_block("sum")]),
                     surfaceOp={"op": "replace", "start": 0, "end": 0},
                     sourceEventSeqs=[0, 0])

    def test_replace_op_must_be_exact_three_keys(self):
        s = self._session()
        with self.assertRaises(ValueError):
            s.append("user/message", create_message("user", [text_block("sum")]),
                     surfaceOp={"op": "replace", "start": 0, "end": 0, "extra": 1},
                     sourceEventSeqs=[0])

    def test_replace_op_negative_seq_rejected(self):
        s = self._session()
        with self.assertRaises(ValueError):
            s.append("user/message", create_message("user", [text_block("sum")]),
                     surfaceOp={"op": "replace", "start": -1, "end": 0},
                     sourceEventSeqs=[0])

    def test_nonsurface_with_source_event_seqs_rejected(self):
        s = self._session()
        with self.assertRaises(ValueError):
            s.append("turn/start", {"turn": 1}, sourceEventSeqs=[0])

    def test_append_source_event_seqs_empty_rejected(self):
        s = self._session()
        with self.assertRaises(ValueError):
            s.append("tool/result", {
                "turn": 1, "step": 1,
                "message": create_message("user", [tool_result_block("c", [text_block("x")])]),
            }, surfaceOp="append", sourceEventSeqs=[])

    def test_append_source_event_seqs_must_be_earlier(self):
        s = self._session()
        with self.assertRaises(ValueError):
            s.append("tool/result", {
                "turn": 1, "step": 1,
                "message": create_message("user", [tool_result_block("c", [text_block("x")])]),
            }, surfaceOp="append", sourceEventSeqs=[2])  # 当前 seq == 1

    def test_valid_append_source_event_seqs_accepted(self):
        s = self._session()
        msg = create_message("user", [tool_result_block("c", [text_block("x")])])
        ev = s.append("tool/result", {"turn": 1, "step": 1, "message": msg},
                      surfaceOp="append", sourceEventSeqs=[0])
        self.assertEqual(ev["sourceEventSeqs"], (0,))

    def test_tool_result_replace_must_target_tool_result(self):
        s = self._session()
        s.append("assistant/message", {
            "message": create_message("assistant", [text_block("a")]),
        }, surfaceOp="append")
        # 被遮蔽节点 seq 0 是 user/message，不是 tool/result
        with self.assertRaises(ValueError):
            s.append("tool/result", {
                "turn": 1, "step": 1,
                "message": create_message("user", [tool_result_block("c", [text_block("x")])]),
            }, surfaceOp={"op": "replace", "start": 0, "end": 0}, sourceEventSeqs=[0])

    def test_tool_result_replace_multi_node_rejected(self):
        s = Session("sv2")
        msg = create_message("user", [tool_result_block("c1", [text_block("x")])])
        s.append("user/message", create_message("user", [text_block("hi")]), surfaceOp="append")
        s.append("tool/result", {"turn": 1, "step": 1, "message": msg}, surfaceOp="append")
        s.append("assistant/message", {
            "message": create_message("assistant", [text_block("a")]),
        }, surfaceOp="append")
        # 遮蔽 2 个节点 → 违反"恰好一个"
        with self.assertRaises(ValueError):
            s.append("tool/result", {
                "turn": 1, "step": 1,
                "message": create_message("user", [tool_result_block("c2", [text_block("y")])]),
            }, surfaceOp={"op": "replace", "start": 1, "end": 2}, sourceEventSeqs=[1, 2])

    def test_tool_result_replace_only_content_change_allowed(self):
        s = Session("sv3")
        orig = create_message("user", [tool_result_block("c1", [text_block("a.txt")])])
        s.append("user/message", create_message("user", [text_block("hi")]), surfaceOp="append")
        s.append("tool/result", {"turn": 1, "step": 1, "message": orig},
                 surfaceOp="append")
        # 只改 content（复用同一消息 id，其余字段不变）→ 合法
        revised = create_message("user", [tool_result_block("c1", [text_block("b.txt")])])
        revised["id"] = orig["id"]
        s.append("tool/result", {"turn": 1, "step": 1, "message": revised},
                 surfaceOp={"op": "replace", "start": 1, "end": 1},
                 sourceEventSeqs=[1])
        self.assertEqual(s.replace_generation, 1)
        # 改 toolCallId → 非法
        tampered = create_message("user", [tool_result_block("c9", [text_block("c.txt")])])
        tampered["id"] = orig["id"]
        with self.assertRaises(ValueError):
            s.append("tool/result", {"turn": 1, "step": 1, "message": tampered},
                     surfaceOp={"op": "replace", "start": 1, "end": 1},
                     sourceEventSeqs=[1])

    def test_seed_rejects_bad_provenance(self):
        # seed 里的 replace 也必须覆盖被遮蔽节点（fail-closed 加载）
        seed = [
            {"type": "user/message", "seq": 0, "time": 1,
             "data": create_message("user", [text_block("hi")]),
             "surfaceOp": "append"},
            {"type": "assistant/message", "seq": 1, "time": 1,
             "data": {"message": create_message("assistant", [text_block("sum")])},
             "surfaceOp": {"op": "replace", "start": 0, "end": 0}},
        ]
        with self.assertRaises(ValueError):
            Session("sv4", seed=seed)

    def test_seed_accepts_valid_provenance(self):
        # V2：assistant/message 内嵌 stream、不带 sourceEventSeqs（seed 边界
        # assertCurrentAssistantStream 三事实校验通过）；有效血统改由
        # user/message 检查点承载（上游 surface.ts assertProvenance V2）
        seed = [
            {"type": "user/message", "seq": 0, "time": 1,
             "data": create_message("user", [text_block("hi")]),
             "surfaceOp": "append"},
            {"type": "assistant/message", "seq": 1, "time": 1,
             "data": _embedded_assistant_data("sum"),
             "surfaceOp": "append"},
            {"type": "user/message", "seq": 2, "time": 1,
             "data": create_message("user", [text_block("压缩后的摘要")]),
             "surfaceOp": {"op": "replace", "start": 0, "end": 0},
             "sourceEventSeqs": [0]},
        ]
        s = Session("sv4", seed=seed)
        self.assertEqual(s.replace_generation, 1)


if __name__ == "__main__":
    unittest.main()