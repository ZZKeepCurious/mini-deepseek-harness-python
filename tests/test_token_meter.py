"""M1：token 计量 —— 启发式定价 + 增量 fold + usage 锚定 + surface replace。

上游对照：packages/llm/token-meter/src（index.ts + estimate.ts + surface-fold.ts）。
"""
import unittest

from miniharness.core.session import (
    Session,
    create_message,
    derive_messages,
    text_block,
    tool_result_block,
)
from miniharness.llm.token_meter import (
    BLOCK_OVERHEAD,
    CHARS_PER_TOKEN,
    ROLE_OVERHEAD,
    TokenMeter,
    estimate_content,
    estimate_header,
    estimate_message,
    estimate_system_tokens,
    estimate_tools_tokens,
)


def _text_message(role: str, text: str) -> dict:
    return create_message(role, [text_block(text)], {})


class EstimateTest(unittest.TestCase):
    def test_text_block_pricing(self):
        self.assertEqual(estimate_content([text_block("hello")]),
                         2 + BLOCK_OVERHEAD)  # 5 字符 → ceil(5/4)=2
        self.assertEqual(estimate_content([text_block("a")]), 1 + BLOCK_OVERHEAD)

    def test_tool_call_and_result(self):
        blocks = [
            {"type": "tool-call", "id": "c1", "name": "bash", "arguments": "{}"},
            tool_result_block("c1", [text_block("out")], is_error=False),
        ]
        expected = (1 + 1 + BLOCK_OVERHEAD) \
            + (estimate_content([text_block("out")]) + BLOCK_OVERHEAD)
        self.assertEqual(estimate_content(blocks), expected)

    def test_message_includes_role_overhead(self):
        msg = _text_message("user", "hi")
        self.assertEqual(estimate_message(msg), estimate_content(msg["content"]) + ROLE_OVERHEAD)

    def test_header_prices_system_and_tools(self):
        # canonical 信封：config 不计价，system/tools 启发式定价（上游 estimate.ts）
        self.assertEqual(estimate_header(None), 0)
        self.assertEqual(estimate_header({"config": {"provider": "p", "model": "m"}}), 0)
        header = {"config": {"provider": "p", "model": "m"}, "system": "你好", "tools": []}
        self.assertEqual(estimate_system_tokens(header),
                         1 + ROLE_OVERHEAD)  # 2 字符 → ceil(2/4)=1
        self.assertEqual(estimate_tools_tokens(header), 0)  # 空 tools 不计价
        tools = [{"name": "bash", "description": "d", "parameters": {"type": "object"}}]
        header["tools"] = tools
        import json
        expected_tools = len(json.dumps(tools, ensure_ascii=False,
                                        sort_keys=True)) // CHARS_PER_TOKEN + BLOCK_OVERHEAD
        self.assertEqual(estimate_tools_tokens(header), expected_tools)
        self.assertEqual(estimate_header(header),
                         estimate_system_tokens(header) + estimate_tools_tokens(header))


def _append_text(session: Session, text: str, role: str = "user") -> dict:
    message = _text_message(role, text)
    if role == "assistant":
        return session.append("assistant/message",
                              {"turn": 1, "step": 1, "message": message},
                              surfaceOp="append")
    return session.append("user/message", message, surfaceOp="append")


class TokenMeterFoldTest(unittest.TestCase):
    def setUp(self):
        self.meter = TokenMeter()
        self.session = Session("m1")

    def test_empty_session(self):
        m = self.meter.measure(self.session)
        self.assertEqual(m["logRevision"], 0)  # 空会话无 end-seed（重放模式才补记）
        self.assertEqual(m["baseline"]["kind"], "none")
        self.assertEqual(m["totalTokens"], 0)
        self.assertEqual(m["surfaceTokens"], 0)
        self.assertEqual(m["nodes"], [])

    def test_append_increments_surface(self):
        self.session.append("request/header",
                            {"header": {"config": {"provider": "p", "model": "m"}}, "reason": "initial"})
        ev = _append_text(self.session, "你好世界，这是第一句话。")
        m = self.meter.measure(self.session)
        expected = estimate_message(ev["data"])
        self.assertEqual(m["surfaceTokens"], expected)
        self.assertEqual(m["baseline"]["kind"], "estimated")
        self.assertEqual(m["totalTokens"], expected)

    def test_idempotent_remeasure(self):
        _append_text(self.session, "a")
        m1 = self.meter.measure(self.session)
        m2 = self.meter.measure(self.session)
        self.assertEqual(m1["totalTokens"], m2["totalTokens"])

    def test_replace_shadows_range(self):
        first = _append_text(self.session, "很长的一段旧内容。")
        self.session.append("step/start", {"turn": 1, "step": 1})
        second = _append_text(self.session, "另一段内容。", role="assistant")
        self.session.append("step/end", {"turn": 1, "step": 1})
        old_surface = self.meter.measure(self.session)["surfaceTokens"]
        summary = _text_message("user", "压缩后的摘要。")
        self.session.append("user/message", summary, surfaceOp={
            "op": "replace", "start": first["seq"], "end": second["seq"],
        }, sourceEventSeqs=[first["seq"], second["seq"]])
        m = self.meter.measure(self.session)
        expected = estimate_message(summary)
        self.assertEqual(m["surfaceTokens"], expected)
        checkpoint_seq = self.session.events[-1]["seq"]
        self.assertEqual(m["nodes"], [{"seq": checkpoint_seq, "tokens": expected}])
        self.assertLess(m["surfaceTokens"], old_surface)

    def test_usage_anchor_adopted(self):
        self.session.append("request/header",
                            {"header": {"config": {"provider": "p", "model": "m"}}, "reason": "initial"})
        ev = _append_text(self.session, "user 输入")
        self.session.append("step/start", {"turn": 1, "step": 1})
        assistant = create_message("assistant", [text_block("输出")], {"kind": "model"})
        self.session.append("assistant/message", {
            "turn": 1, "step": 1, "message": assistant,
            "usage": {"inputTokens": 100, "outputTokens": 50,
                      "cacheReadTokens": 30, "cacheWriteTokens": 20},
        }, surfaceOp="append")
        self.session.append("step/end", {"turn": 1, "step": 1})
        m = self.meter.measure(self.session)
        self.assertEqual(m["baseline"]["kind"], "usage")
        self.assertEqual(m["baseline"]["tokens"], 200)
        self.assertEqual(m["totalTokens"], 200)
        _ = ev  # surface anchor surfaceTokens 用于后续断言

    def test_usage_anchor_rejected_when_too_small(self):
        self.session.append("request/header",
                            {"header": {"config": {"provider": "p", "model": "m"}}, "reason": "initial"})
        _append_text(self.session, "输入内容")
        self.session.append("step/start", {"turn": 1, "step": 1})
        assistant = create_message("assistant", [text_block("输出")], {"kind": "model"})
        self.session.append("assistant/message", {
            "turn": 1, "step": 1, "message": assistant,
            "usage": {"inputTokens": 1, "outputTokens": 1},
        }, surfaceOp="append")
        self.session.append("step/end", {"turn": 1, "step": 1})
        m = self.meter.measure(self.session)
        self.assertEqual(m["baseline"]["kind"], "estimated")

    def test_step_validation(self):
        self.session.append("step/start", {"turn": 1, "step": 1})
        self.session.append("step/start", {"turn": 1, "step": 2})  # 前一个未结束
        with self.assertRaises(ValueError):
            self.meter.measure(self.session)

    def test_assistant_message_requires_step(self):
        self.session.append("assistant/message", {
            "turn": 1, "step": 1, "message": create_message("assistant", [text_block("x")], {}),
        }, surfaceOp="append")
        with self.assertRaises(ValueError):
            self.meter.measure(self.session)

    def test_source_event_seqs_validation(self):
        self.session.append("request/header",
                            {"header": {"config": {"provider": "p", "model": "m"}}, "reason": "initial"})
        _append_text(self.session, "输入")
        self.session.append("step/start", {"turn": 1, "step": 1})
        assistant = create_message("assistant", [text_block("输出")], {"kind": "model"})
        # 引用未来 seq 的 sourceEventSeqs 在 append 层即被拒绝（上游 assertProvenance
        # 要求全部早于事件 seq，fail-closed 在日志边界，meter 无需再校验）
        with self.assertRaises(ValueError):
            self.session.append("assistant/message", {
                "turn": 1, "step": 1, "message": assistant,
                "usage": {"inputTokens": 10, "outputTokens": 5},
            }, surfaceOp="append", sourceEventSeqs=[999])


class TokenMeterReplayTest(unittest.TestCase):
    """跨 Session 实例的增量 fold（crash + replay 场景）。"""

    def test_replay_continues_from_consumed(self):
        s1 = Session("r1")
        s1.append("request/header", {"header": {"config": {"provider": "p", "model": "m"}},
                                     "reason": "initial"})
        s1.append("user/message", _text_message("user", "旧输入"), surfaceOp="append")
        meter = TokenMeter()
        first = meter.measure(s1)
        # 恢复模式：seed 重放后补 end-seed
        s2 = Session("r1", seed=list(s1.events))
        m2 = meter.measure(s2)
        self.assertEqual(m2["logRevision"], len(s2.events))
        self.assertEqual(m2["surfaceTokens"], first["surfaceTokens"])
        self.assertEqual(derive_messages(s2.events), derive_messages(s1.events))


if __name__ == "__main__":
    unittest.main()
