"""session-turn-outline（turnOutline 投影单元）验收。

对齐上游 `packages/session/session-turn-outline/tests/projection.spec.ts`：
- preview：space-join 文本块 + 空白折叠 + 预算截断省略号 + 逐块有界读取
- fold：turn/start 锚定条目、人类 prompt 首块即胜、draft 缓冲到 turn/end、
  非人类 source 忽略、身份门（unhandled 返回同一引用 / draft-only 保持
  turns 身份）
- schema：严格递增 turn 拒绝、预算上限、恢复校验
"""
import unittest

from miniharness.core.scope import Context
from miniharness.core.session import create_message, text_block
from miniharness.core.session_store import install_sessions
from miniharness.session_projection import install_session_projections
from miniharness.session_turn_outline import (
    PROMPT_PREVIEW_LIMIT,
    RESPONSE_PREVIEW_LIMIT,
    preview,
    turn_outline_projection,
)


def _user_message(text: str, kind: str = "user") -> dict:
    return create_message("user", [text_block(text)], {"kind": kind})


def _assistant_message(text: str) -> dict:
    return {
        "type": "assistant/message",
        "seq": 0,
        "data": {
            "turn": 1, "step": 1,
            "message": create_message("assistant", [text_block(text)]),
            "stream": [],
        },
    }


class TestPreview(unittest.TestCase):
    def test_plain_text(self):
        self.assertEqual(preview([text_block("hello world")], 50), "hello world")

    def test_space_join_and_collapse(self):
        content = [text_block("  a\t\nb  "), text_block("  c d  ")]
        self.assertEqual(preview(content, 50), "a b c d")

    def test_whitespace_only(self):
        self.assertEqual(preview([text_block("   \n\t ")], 50), "")

    def test_non_text_blocks_skipped(self):
        content = [{"type": "image", "attachment": {}}, text_block("hi")]
        self.assertEqual(preview(content, 50), "hi")

    def test_truncation_with_ellipsis(self):
        out = preview([text_block("a" * 200)], 50)
        self.assertEqual(len(out), 50)
        self.assertTrue(out.startswith("a" * 49))
        self.assertEqual(out[-1], "\u2026")

    def test_clipped_block_marks_unread_even_when_short(self):
        # 单块超 2*limit 直接裁剪标记 unread，即使裁剪后文本很短
        content = [text_block("x" * 500)]
        out = preview(content, 10)
        self.assertEqual(out, "x" * 9 + "\u2026")

    def test_single_oversized_block_bound(self):
        content = [text_block("g" * 200)]
        out = preview(content, PROMPT_PREVIEW_LIMIT)
        self.assertEqual(len(out), PROMPT_PREVIEW_LIMIT)
        self.assertTrue(out.startswith("g" * (PROMPT_PREVIEW_LIMIT - 1)))


class TestTurnOutlineProjection(unittest.TestCase):
    def setUp(self):
        self.proj = turn_outline_projection()
        self.state = self.proj.init(None, 0)

    def test_contract(self):
        self.assertEqual(self.proj.key, "turnOutline")
        self.assertEqual(self.proj.state_version, 2)
        self.assertIsNotNone(self.proj.view)
        self.assertIsNotNone(self.proj.state_schema)
        self.assertIsNotNone(self.proj.view_schema)

    def test_empty_before_any_turn(self):
        self.assertEqual(self.proj.view(self.state), [])
        self.assertEqual(self.state, {"turns": [], "draft": ""})

    def test_full_fold(self):
        state = self.proj.apply(self.state, {
            "type": "turn/start", "seq": 0, "data": {"turn": 1}})
        state = self.proj.apply(state, {
            "type": "user/message", "seq": 1, "data": _user_message("hello world")})
        state = self.proj.apply(state, {
            "type": "assistant/message", "seq": 2,
            "data": {"turn": 1, "step": 1,
                     "message": create_message("assistant",
                                               [text_block("final answer of turn one")]),
                     "stream": []}})
        state = self.proj.apply(state, {"type": "turn/end", "seq": 3,
                                        "data": {"turn": 1, "reason": {"kind": "stop"}}})
        # 第二回合（开放中）
        state = self.proj.apply(state, {
            "type": "turn/start", "seq": 4, "data": {"turn": 2}})
        state = self.proj.apply(state, {
            "type": "user/message", "seq": 5, "data": _user_message("second prompt")})
        self.assertEqual(self.proj.view(state), [
            {"turn": 1, "seq": 0, "prompt": "hello world", "response": "final answer of turn one"},
            {"turn": 2, "seq": 4, "prompt": "second prompt", "response": ""},
        ])

    def test_draft_buffers_until_turn_end(self):
        state = self.proj.apply(self.state, {
            "type": "turn/start", "seq": 0, "data": {"turn": 1}})
        state = self.proj.apply(state, {
            "type": "assistant/message", "seq": 1,
            "data": {"turn": 1, "step": 1,
                     "message": create_message("assistant",
                                               [text_block("streamed but unsettled")]),
                     "stream": []}})
        view = self.proj.view(state)
        self.assertEqual(view[0]["response"], "")  # 未落定 response 仍空
        self.assertEqual(state["draft"], "streamed but unsettled")
        state = self.proj.apply(state, {"type": "turn/end", "seq": 2,
                                        "data": {"turn": 1, "reason": {"kind": "stop"}}})
        self.assertEqual(self.proj.view(state)[0]["response"], "streamed but unsettled")

    def test_steering_keeps_first_prompt(self):
        state = self.proj.apply(self.state, {
            "type": "turn/start", "seq": 0, "data": {"turn": 1}})
        state = self.proj.apply(state, {
            "type": "user/message", "seq": 1, "data": _user_message("first prompt")})
        state = self.proj.apply(state, {
            "type": "user/message", "seq": 2, "data": _user_message("steering later")})
        self.assertEqual(self.proj.view(state)[0]["prompt"], "first prompt")

    def test_non_human_source_ignored(self):
        state = self.proj.apply(self.state, {
            "type": "turn/start", "seq": 0, "data": {"turn": 1}})
        state = self.proj.apply(state, {
            "type": "user/message", "seq": 1,
            "data": _user_message("injected context", kind="runtime-context")})
        self.assertEqual(self.proj.view(state), [
            {"turn": 1, "seq": 0, "prompt": "", "response": ""}])

    def test_pre_turn_prompt_ignored(self):
        state = self.proj.apply(self.state, {
            "type": "user/message", "seq": 0, "data": _user_message("orphan prompt")})
        self.assertEqual(self.state, state)

    def test_regressive_boundary_returns_same_reference(self):
        state = self.proj.apply(self.state, {
            "type": "turn/start", "seq": 0, "data": {"turn": 2}})
        again = self.proj.apply(state, {"type": "turn/start", "seq": 5,
                                        "data": {"turn": 2}})
        self.assertIs(state, again)

    def test_unrelated_event_returns_same_reference(self):
        self.assertIs(self.state, self.proj.apply(
            self.state, {"type": "tool/call", "seq": 1, "data": {}}))

    def test_draft_only_keeps_turns_identity(self):
        state = self.proj.apply(self.state, {
            "type": "turn/start", "seq": 0, "data": {"turn": 1}})
        turns_ref = state["turns"]
        state = self.proj.apply(state, {
            "type": "assistant/message", "seq": 1,
            "data": {"turn": 1, "step": 1,
                     "message": create_message("assistant", [text_block("draft one")]),
                     "stream": []}})
        self.assertIs(state["turns"], turns_ref)

    def test_quiet_draftless_end(self):
        state = self.proj.apply(self.state, {
            "type": "turn/start", "seq": 0, "data": {"turn": 1}})
        state = self.proj.apply(state, {"type": "turn/end", "seq": 1,
                                        "data": {"turn": 1, "reason": {"kind": "stop"}}})
        self.assertEqual(self.proj.view(state), [
            {"turn": 1, "seq": 0, "prompt": "", "response": ""}])

    def test_state_schema_rejects_regressive_turns(self):
        with self.assertRaises(ValueError):
            self.proj.state_schema({"turns": [
                {"turn": 1, "seq": 0, "prompt": "a", "response": ""},
                {"turn": 1, "seq": 4, "prompt": "b", "response": ""},
            ], "draft": ""})
        # 严格递增合法
        self.proj.state_schema({"turns": [
            {"turn": 1, "seq": 0, "prompt": "a", "response": ""},
            {"turn": 2, "seq": 4, "prompt": "b", "response": ""},
        ], "draft": ""})


class TestTurnOutlineRegistry(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        self.store = install_sessions(self.ctx)
        self.registry = install_session_projections(self.ctx)
        self.session = self.store.create("s1", {"meta": {}})

    def tearDown(self):
        self.ctx.dispose()

    def test_registered_unit_snapshots(self):
        from miniharness.session_turn_outline import install_turn_outline
        disposer = install_turn_outline(self.ctx)
        snapshot = self.registry.snapshot(self.session)
        self.assertEqual(snapshot["values"]["turnOutline"], [])
        disposer()
        snapshot = self.registry.snapshot(self.session)
        self.assertNotIn("turnOutline", snapshot["values"])

    def test_fold_through_registry(self):
        from miniharness.session_turn_outline import install_turn_outline
        install_turn_outline(self.ctx)
        self.session.append("turn/start", {"turn": 1})
        self.session.append("user/message", _user_message("registry prompt"),
                            surfaceOp="append")
        snapshot = self.registry.snapshot(self.session)
        self.assertEqual(snapshot["values"]["turnOutline"], [
            {"turn": 1, "seq": 0, "prompt": "registry prompt", "response": ""}])

    def test_missing_registry_fails_loud(self):
        from miniharness.session_turn_outline import install_turn_outline
        from miniharness.core.scope import Context as Ctx
        bare = Ctx(name="bare")
        try:
            with self.assertRaises(RuntimeError):
                install_turn_outline(bare)
        finally:
            bare.dispose()


if __name__ == "__main__":
    unittest.main()