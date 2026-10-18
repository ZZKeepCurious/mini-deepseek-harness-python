"""released 跨事件关系状态机专项（relationships.py 直测）。

直接驱动 `assert_released_artifact_relationships(artifact, extensions)`：真实事件
字典 + 逐字异常断言，无 mock / monkeypatch。覆盖：

  * 合法吞吐：turn/step 开闭、surface 追加/替换、工具 advertise→call→result、
    TOOL_NOT_STARTED 修复、compaction start/summary/prune/end、llm/retry 链、
    command 配对、PTC 根系、标题来源、delivery marker、继承孤儿 compaction；
  * 每条错误分支（消息逐字取自源码）。
"""
import re
import unittest

from miniharness.core.session.released.helpers import js_stringify
from miniharness.core.session.released.relationships import (
    assert_released_artifact_relationships,
)

INTERRUPTED_TEXT = ("The tool call was interrupted before the Harness recorded it as "
                    "started. Retry it if it is still needed.")


def ev(seq, time, etype, data, **extra):
    return {"type": etype, "seq": seq, "time": time, "data": data, **extra}


def text_block(text):
    return {"type": "text", "text": text}


def tool_call_block(call_id="c1", name="read", arguments="{}"):
    return {"type": "tool-call", "id": call_id, "name": name, "arguments": arguments}


def model_source(provider="fake", model="fake", **extra):
    return {"kind": "model", "provider": provider, "model": model, **extra}


def artifact(events, version=1, inherited=0, parent=None, session_id="s"):
    header = {"version": version, "id": session_id, "createdAt": 1,
              "isSeeded": False, "delegationDepth": 0}
    if parent is not None:
        header["parentSession"] = parent
    return {"header": header, "inherited_event_count": inherited, "events": events}


def user_message(seq, text="hi", ident=None, source=None, **extra):
    data = {"id": ident or f"m{seq}", "role": "user",
            "content": [text_block(text)], "source": source or {"kind": "user"}}
    extra.setdefault("surfaceOp", "append")
    return ev(seq, 100 + seq, "user/message", data, **extra)


def assistant_message(seq, content=None, **extra):
    data = {"turn": 1, "step": 1,
            "message": {"id": f"am{seq}", "role": "assistant",
                        "content": [] if content is None else content,
                        "source": model_source()}}
    extra.setdefault("surfaceOp", "append")
    return ev(seq, 100 + seq, "assistant/message", data, **extra)


def tool_result(seq, call_id="c1", **extra):
    data = {"turn": 1, "step": 1,
            "message": {"id": f"r{seq}", "role": "user",
                        "content": [{"type": "tool-result", "toolCallId": call_id,
                                     "content": [text_block("out")]}],
                        "source": {"kind": "tool", "callId": call_id}}}
    extra.setdefault("surfaceOp", "append")
    return ev(seq, 100 + seq, "tool/result", data, **extra)


def repair_result(seq, call_id="c1", text=INTERRUPTED_TEXT, ident=None, **extra):
    data = {"turn": 1, "step": 1,
            "error": {"name": "ToolNotStartedError", "code": "TOOL_NOT_STARTED"},
            "message": {"id": ident or f"interrupted-tool-result-{call_id}-{seq}",
                        "role": "user",
                        "content": [{"type": "tool-result", "toolCallId": call_id,
                                     "isError": True,
                                     "content": [text_block(text)]}],
                        "source": {"kind": "tool", "callId": call_id}}}
    extra.setdefault("surfaceOp", "append")
    return ev(seq, 100 + seq, "tool/result", data, **extra)


class ValidFlowsTest(unittest.TestCase):
    def test_full_lifecycle_passes(self):
        events = [
            user_message(0),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            ev(3, 103, "request/header",
               {"header": {"config": {"provider": "fake"}}, "reason": "initial"}),
            ev(4, 104, "request/context", {"provider": "fake", "model": "m"}),
            ev(5, 105, "assistant/chunk",
               {"turn": 1, "step": 1,
                "chunk": {"type": "text-delta", "index": 0, "text": "hi"}}),
            assistant_message(6, content=[tool_call_block()]),
            ev(7, 107, "tool/call", {"turn": 1, "step": 1, "callId": "c1",
                                     "name": "read", "arguments": "{}"}),
            tool_result(8),
            ev(9, 109, "llm/retry",
               {"retryId": "r1", "turn": 1, "step": 1, "provider": "fake",
                "mode": "normal", "policyKey": "default", "retry": 1, "delayMs": 0,
                "failure": {"message": "x", "code": "SERVER"}}),
            ev(10, 110, "llm/retry-started",
               {"retryId": "r1", "turn": 1, "step": 1, "retry": 1}),
            ev(11, 111, "step/end", {"turn": 1, "step": 1}),
            ev(12, 112, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
            ev(13, 113, "command/run",
               {"commandId": "c1", "name": "inspect", "source": {"kind": "user"}}),
            ev(14, 114, "command/done",
               {"commandId": "c1", "kind": "success", "sourceEventSeq": 5}),
        ]
        assert_released_artifact_relationships(artifact(events))

    def test_tool_not_started_repair_passes(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            assistant_message(2, content=[tool_call_block()]),
            repair_result(3),
            ev(4, 104, "step/end", {"turn": 1, "step": 1}),
            ev(5, 105, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        assert_released_artifact_relationships(artifact(events))

    def test_attempt_extension_inside_step(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "assistant/attempt", {"turn": 1, "step": 1, "stream": []}),
            ev(3, 103, "step/end", {"turn": 1, "step": 1}),
            ev(4, 104, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        assert_released_artifact_relationships(
            artifact(events), {"stepEvents": frozenset({"assistant/attempt"})})

    def test_surface_replace_head_node_zero(self):
        events = [
            user_message(0),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            user_message(3, text="replacement",
                         surfaceOp={"op": "replace", "start": 0, "end": 0},
                         sourceEventSeqs=[0]),
            ev(4, 104, "step/end", {"turn": 1, "step": 1}),
            ev(5, 105, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        assert_released_artifact_relationships(artifact(events))

    def test_compaction_summary_prune_end_passes(self):
        events = [
            user_message(0),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            ev(3, 103, "compaction/start", {"compactionId": "k1", "turn": 1}),
            ev(4, 104, "compaction/summary",
               {"compactionId": "k1", "summary": [],
                "shadowedRange": {"start": 0, "end": 0},
                "shadowedSeqs": [0], "shadowedTokenCount": 0,
                "provider": "fake", "model": "m"}),
            ev(5, 105, "compaction/prune",
               {"shadowedRange": {"start": 0, "end": 0}, "shadowedSeqs": [0],
                "shadowedTokenCount": 0}),
            ev(6, 106, "compaction/end", {"compactionId": "k1", "turn": 1}),
            ev(7, 107, "step/end", {"turn": 1, "step": 1}),
            ev(8, 108, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        assert_released_artifact_relationships(artifact(events))

    def test_compaction_end_with_error_needs_no_summary(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "compaction/start", {"compactionId": "k1", "turn": 1}),
            ev(3, 103, "compaction/end",
               {"compactionId": "k1", "turn": 1,
                "error": {"message": "x", "code": "E"}}),
            ev(4, 104, "step/end", {"turn": 1, "step": 1}),
            ev(5, 105, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        assert_released_artifact_relationships(artifact(events))

    def test_inherited_orphan_compaction_start_then_end_seed(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "compaction/start", {"compactionId": "k1", "turn": 1}),
            ev(3, 103, "session/end-seed", {"inherited": True}),
            ev(4, 104, "step/end", {"turn": 1, "step": 1}),
            ev(5, 105, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        assert_released_artifact_relationships(artifact(events))

    def test_ptc_dispatch_passes(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "tool/code-dispatch-start",
               {"rootCallId": "r", "parentCallId": "r", "subCallId": "s",
                "name": "run", "arguments": "{}"}),
            ev(3, 103, "tool/code-dispatch",
               {"rootCallId": "r", "parentCallId": "r", "subCallId": "s",
                "name": "run", "arguments": "{}", "isError": False, "content": []}),
            ev(4, 104, "step/end", {"turn": 1, "step": 1}),
            ev(5, 105, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        assert_released_artifact_relationships(artifact(events))

    def test_session_title_model_source_passes(self):
        events = [
            user_message(0),
            ev(1, 101, "session/title",
               {"title": "T", "messageSeqs": [0], "source": {"kind": "model"}}),
        ]
        assert_released_artifact_relationships(artifact(events))

    def test_session_title_user_source_empty_seqs_passes(self):
        events = [
            ev(0, 100, "session/title",
               {"title": "T", "messageSeqs": [], "source": {"kind": "user"}}),
        ]
        assert_released_artifact_relationships(artifact(events))

    def test_title_llm_request_passes(self):
        selected = [{"seq": 0, "text": "hi"}]
        expected = ("Generate the session title from this JSON array of human messages:\n"
                    + js_stringify(selected))
        events = [
            user_message(0),
            ev(1, 101, "session/title-llm-request",
               {"titleProvider": "p", "messageSeqs": [0], "route": "r",
                "system": "s",
                "messages": [{"role": "user", "content": [text_block(expected)],
                              "source": {"kind": "plugin",
                                         "plugin": "dsh-session-title-llm"}}],
                "maxTokens": 10}),
        ]
        assert_released_artifact_relationships(artifact(events))

    def test_title_llm_request_preserved_text_skips_framing(self):
        events = [
            user_message(0),
            ev(1, 101, "session/title-llm-request",
               {"titleProvider": "p", "messageSeqs": [0], "route": "r",
                "system": "s",
                "messages": [{"role": "user", "content": [text_block("anything")],
                              "source": {"kind": "plugin",
                                         "plugin": "dsh-session-title-llm"}}],
                "maxTokens": 10}),
        ]
        assert_released_artifact_relationships(
            artifact(events), {"preservedSourceTitleRequestText": True})

    def test_delivery_marker_current_session_passes(self):
        events = [
            ev(0, 100, "session-log-deepseek/delivery-accepted",
               {"sessionId": "s", "throughSeq": 0, "sessionFormatVersion": 1}),
        ]
        assert_released_artifact_relationships(artifact(events, version=1))

    def test_delivery_marker_inherited_prefix_skips(self):
        events = [
            ev(0, 100, "session-log-deepseek/delivery-accepted",
               {"sessionId": "other", "throughSeq": 0, "sessionFormatVersion": 1}),
        ]
        assert_released_artifact_relationships(
            artifact(events, version=1, inherited=1, parent="p"))

    def test_delivery_marker_other_generation_skips(self):
        events = [
            ev(0, 100, "session-log-deepseek/delivery-accepted",
               {"sessionId": "other", "throughSeq": 0, "sessionFormatVersion": 9}),
        ]
        assert_released_artifact_relationships(artifact(events, version=1))


class TurnStepErrorsTest(unittest.TestCase):
    def test_turn_start_nested(self):
        events = [ev(0, 100, "turn/start", {"turn": 1}),
                  ev(1, 101, "turn/start", {"turn": 1})]
        self.assertRaisesRegex(
            Exception, re.escape("turn/start 1 does not open expected turn 1"),
            assert_released_artifact_relationships, artifact(events))

    def test_turn_start_wrong_expected(self):
        events = [ev(0, 100, "turn/start", {"turn": 2})]
        self.assertRaisesRegex(
            Exception, re.escape("turn/start 2 does not open expected turn 1"),
            assert_released_artifact_relationships, artifact(events))

    def test_turn_end_no_matching(self):
        events = [ev(0, 100, "turn/end",
                     {"turn": 1, "reason": {"kind": "completed"}})]
        self.assertRaisesRegex(
            Exception, re.escape("turn/end 1 has no matching open turn"),
            assert_released_artifact_relationships, artifact(events))

    def test_turn_end_crosses_open_step(self):
        events = [ev(0, 100, "turn/start", {"turn": 1}),
                  ev(1, 101, "step/start", {"turn": 1, "step": 1}),
                  ev(2, 102, "turn/end",
                     {"turn": 1, "reason": {"kind": "completed"}})]
        self.assertRaisesRegex(
            Exception, re.escape("turn/end 1 crosses an open step"),
            assert_released_artifact_relationships, artifact(events))

    def test_turn_end_leaves_unresolved_tool(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            assistant_message(2, content=[tool_call_block()]),
            ev(3, 103, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        self.assertRaisesRegex(
            Exception, re.escape("turn/end leaves unresolved tool call c1"),
            assert_released_artifact_relationships, artifact(events))

    def test_step_start_mismatch(self):
        events = [ev(0, 100, "turn/start", {"turn": 1}),
                  ev(1, 101, "step/start", {"turn": 1, "step": 2})]
        self.assertRaisesRegex(
            Exception,
            re.escape("step/start does not match the open turn and next step"),
            assert_released_artifact_relationships, artifact(events))

    def test_step_end_not_open(self):
        events = [ev(0, 100, "turn/start", {"turn": 1}),
                  ev(1, 101, "step/end", {"turn": 1, "step": 1})]
        self.assertRaisesRegex(
            Exception, re.escape("step/end does not match an open turn and step"),
            assert_released_artifact_relationships, artifact(events))

    def test_step_end_leaves_unresolved_tool(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            assistant_message(2, content=[tool_call_block()]),
            ev(3, 103, "step/end", {"turn": 1, "step": 1}),
        ]
        self.assertRaisesRegex(
            Exception, re.escape("step/end leaves unresolved tool call c1"),
            assert_released_artifact_relationships, artifact(events))

    def test_assistant_chunk_outside_step(self):
        events = [ev(0, 100, "turn/start", {"turn": 1}),
                  ev(1, 101, "assistant/chunk",
                     {"turn": 1, "step": 1,
                      "chunk": {"type": "text-delta", "index": 0, "text": "x"}})]
        self.assertRaisesRegex(
            Exception,
            re.escape("assistant/chunk does not match an open turn and step"),
            assert_released_artifact_relationships, artifact(events))

    def test_assistant_message_outside_step(self):
        events = [ev(0, 100, "turn/start", {"turn": 1}),
                  assistant_message(1)]
        self.assertRaisesRegex(
            Exception,
            re.escape("assistant/message does not match an open turn and step"),
            assert_released_artifact_relationships, artifact(events))

    def test_attempt_extension_outside_step(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "assistant/attempt", {"turn": 1, "step": 1, "stream": []}),
        ]
        self.assertRaisesRegex(
            Exception,
            re.escape("assistant/attempt does not match an open turn and step"),
            assert_released_artifact_relationships, artifact(events),
            {"stepEvents": frozenset({"assistant/attempt"})})

    def test_outer_events_outside_turn(self):
        for etype, data in (
            ("request/header",
             {"header": {"config": {"provider": "fake"}}, "reason": "initial"}),
            ("request/context", {"provider": "p", "model": "m"}),
        ):
            self.assertRaisesRegex(
                Exception, re.escape(f"{etype} is outside an open turn"),
                assert_released_artifact_relationships,
                artifact([ev(0, 100, etype, data)]))


class ToolLifecycleErrorsTest(unittest.TestCase):
    def _open(self):
        return [ev(0, 100, "turn/start", {"turn": 1}),
                ev(1, 101, "step/start", {"turn": 1, "step": 1})]

    def test_assistant_message_repeats_advertised(self):
        events = [*self._open(),
                  assistant_message(2, content=[tool_call_block(),
                                                tool_call_block()])]
        self.assertRaisesRegex(
            Exception,
            re.escape("assistant/message repeats advertised tool call c1"),
            assert_released_artifact_relationships, artifact(events))

    def test_tool_call_without_advertise(self):
        events = [*self._open(),
                  ev(2, 102, "tool/call",
                     {"turn": 1, "step": 1, "callId": "c1",
                      "name": "read", "arguments": "{}"})]
        self.assertRaisesRegex(
            Exception,
            re.escape("tool/call c1 does not match one advertised tool call"),
            assert_released_artifact_relationships, artifact(events))

    def test_tool_call_name_mismatch(self):
        events = [*self._open(),
                  assistant_message(2, content=[tool_call_block("c1", "read",
                                                                "{}")]),
                  ev(3, 103, "tool/call",
                     {"turn": 1, "step": 1, "callId": "c1",
                      "name": "write", "arguments": "{}"})]
        self.assertRaisesRegex(
            Exception,
            re.escape("tool/call c1 does not match one advertised tool call"),
            assert_released_artifact_relationships, artifact(events))

    def test_tool_result_without_lifecycle(self):
        events = [*self._open(), tool_result(2)]
        self.assertRaisesRegex(
            Exception, re.escape("tool/result c1 has no advertised tool lifecycle"),
            assert_released_artifact_relationships, artifact(events))

    def test_tool_result_not_exact_repair(self):
        events = [*self._open(),
                  assistant_message(2, content=[tool_call_block()]),
                  repair_result(3, text="not the repair text")]
        self.assertRaisesRegex(
            Exception,
            re.escape("tool/result c1 is not the exact TOOL_NOT_STARTED repair"),
            assert_released_artifact_relationships, artifact(events))

    def test_tool_result_replacement_outside_open_turn(self):
        events = [
            user_message(0),
            tool_result(1, surfaceOp={"op": "replace", "start": 0, "end": 0},
                        sourceEventSeqs=[0]),
        ]
        self.assertRaisesRegex(
            Exception,
            re.escape("tool/result replacement is outside an open turn"),
            assert_released_artifact_relationships, artifact(events))


class SurfaceErrorsTest(unittest.TestCase):
    def test_surface_requires_marker(self):
        events = [{"type": "user/message", "seq": 0, "time": 100,
                   "data": {"id": "m0", "role": "user", "content": [],
                            "source": {"kind": "user"}}}]
        self.assertRaisesRegex(
            Exception, re.escape("user/message requires a surfaceOp marker"),
            assert_released_artifact_relationships, artifact(events))

    def test_replacement_range_off_surface(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            user_message(2, surfaceOp={"op": "replace", "start": 0, "end": 5},
                         sourceEventSeqs=[0]),
        ]
        self.assertRaisesRegex(
            Exception,
            re.escape("user/message replacement range is not on the current surface"),
            assert_released_artifact_relationships, artifact(events))

    def test_replacement_omits_shadowed_node(self):
        events = [
            user_message(0),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            assistant_message(3, content=[]),
            user_message(4, surfaceOp={"op": "replace", "start": 0, "end": 3},
                         sourceEventSeqs=[0]),
        ]
        self.assertRaisesRegex(
            Exception,
            re.escape("user/message replacement sourceEventSeqs omit a shadowed "
                      "surface node"),
            assert_released_artifact_relationships, artifact(events))


class CompactionErrorsTest(unittest.TestCase):
    def test_start_overlaps(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "compaction/start", {"compactionId": "k1", "turn": 1}),
            ev(3, 103, "compaction/start", {"compactionId": "k2", "turn": 1}),
        ]
        self.assertRaisesRegex(
            Exception, re.escape("compaction/start overlaps an open compaction"),
            assert_released_artifact_relationships, artifact(events))

    def test_start_mismatch_open_turn(self):
        events = [ev(0, 100, "compaction/start",
                     {"compactionId": "k1", "turn": 1})]
        self.assertRaisesRegex(
            Exception, re.escape("compaction/start does not match the open turn"),
            assert_released_artifact_relationships, artifact(events))

    def test_turn_end_crosses_open_compaction(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "compaction/start", {"compactionId": "k1", "turn": 1}),
            ev(3, 103, "step/end", {"turn": 1, "step": 1}),
            ev(4, 104, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        self.assertRaisesRegex(
            Exception, re.escape("turn/end crosses an open compaction"),
            assert_released_artifact_relationships, artifact(events))

    def test_summary_without_owner(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "compaction/summary",
               {"compactionId": "k1", "summary": [],
                "shadowedRange": {"start": 0, "end": 0},
                "shadowedSeqs": [], "shadowedTokenCount": 0,
                "provider": "fake", "model": "m"}),
        ]
        self.assertRaisesRegex(
            Exception, re.escape("compaction/summary has no matching compaction/start"),
            assert_released_artifact_relationships, artifact(events))

    def test_summary_source_command_mismatch(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "compaction/start", {"compactionId": "k1", "turn": 1}),
            ev(3, 103, "compaction/summary",
               {"compactionId": "k1", "summary": [], "sourceCommandId": "x",
                "shadowedRange": {"start": 0, "end": 0},
                "shadowedSeqs": [], "shadowedTokenCount": 0,
                "provider": "fake", "model": "m"}),
        ]
        self.assertRaisesRegex(
            Exception, re.escape("compaction/summary has no matching compaction/start"),
            assert_released_artifact_relationships, artifact(events))

    def test_summary_repeats(self):
        events = [
            user_message(0),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            ev(3, 103, "compaction/start", {"compactionId": "k1", "turn": 1}),
            ev(4, 104, "compaction/summary",
               {"compactionId": "k1", "summary": [],
                "shadowedRange": {"start": 0, "end": 0},
                "shadowedSeqs": [0], "shadowedTokenCount": 0,
                "provider": "fake", "model": "m"}),
            ev(5, 105, "compaction/summary",
               {"compactionId": "k1", "summary": [],
                "shadowedRange": {"start": 0, "end": 0},
                "shadowedSeqs": [0], "shadowedTokenCount": 0,
                "provider": "fake", "model": "m"}),
        ]
        self.assertRaisesRegex(
            Exception, re.escape("compaction/summary repeats"),
            assert_released_artifact_relationships, artifact(events))

    def test_summary_span_mismatch(self):
        events = [
            user_message(0),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            ev(3, 103, "compaction/start", {"compactionId": "k1", "turn": 1}),
            ev(4, 104, "compaction/summary",
               {"compactionId": "k1", "summary": [],
                "shadowedRange": {"start": 0, "end": 0},
                "shadowedSeqs": [], "shadowedTokenCount": 0,
                "provider": "fake", "model": "m"}),
        ]
        self.assertRaisesRegex(
            Exception,
            re.escape("compaction/summary shadowedSeqs do not name an exact current "
                      "surface span"),
            assert_released_artifact_relationships, artifact(events))

    def test_end_without_owner(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "compaction/end", {"compactionId": "k1", "turn": 1}),
        ]
        self.assertRaisesRegex(
            Exception, re.escape("compaction/end has no matching compaction/start"),
            assert_released_artifact_relationships, artifact(events))

    def test_end_changes_owner_turn(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "compaction/start", {"compactionId": "k1", "turn": 1}),
            ev(3, 103, "compaction/end", {"compactionId": "k1", "turn": 2}),
        ]
        self.assertRaisesRegex(
            Exception, re.escape("compaction/end changes its owner turn"),
            assert_released_artifact_relationships, artifact(events))

    def test_successful_end_requires_summary(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "compaction/start", {"compactionId": "k1", "turn": 1}),
            ev(3, 103, "compaction/end", {"compactionId": "k1", "turn": 1}),
        ]
        self.assertRaisesRegex(
            Exception,
            re.escape("successful compaction/end requires one summary"),
            assert_released_artifact_relationships, artifact(events))

    def test_prune_span_mismatch(self):
        events = [
            user_message(0),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            ev(3, 103, "compaction/prune",
               {"shadowedRange": {"start": 0, "end": 0}, "shadowedSeqs": [9],
                "shadowedTokenCount": 0}),
        ]
        self.assertRaisesRegex(
            Exception,
            re.escape("compaction/prune shadowedSeqs do not name an exact current "
                      "surface span"),
            assert_released_artifact_relationships, artifact(events))

    def test_checkpoint_without_owner(self):
        events = [
            user_message(0),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            user_message(3, source={"kind": "plugin", "plugin": "compact"},
                         surfaceOp={"op": "replace", "start": 0, "end": 0},
                         sourceEventSeqs=[0]),
        ]
        self.assertRaisesRegex(
            Exception,
            re.escape("compaction checkpoint at seq 3 has no matching compaction/start"),
            assert_released_artifact_relationships, artifact(events))


class RetryErrorsTest(unittest.TestCase):
    def _retry(self, seq, **overrides):
        data = {"retryId": "r1", "turn": 1, "step": 1, "provider": "fake",
                "mode": "normal", "policyKey": "default", "retry": 1,
                "delayMs": 0, "failure": {"message": "x", "code": "SERVER"}}
        data.update(overrides)
        return ev(seq, 100 + seq, "llm/retry", data)

    def _open_step(self):
        return [ev(0, 100, "turn/start", {"turn": 1}),
                ev(1, 101, "step/start", {"turn": 1, "step": 1})]

    def test_provider_without_matching_header(self):
        events = [*self._open_step(), self._retry(2)]
        self.assertRaisesRegex(
            Exception,
            re.escape("llm/retry provider does not match the open request/header"),
            assert_released_artifact_relationships, artifact(events))

    def test_first_retry_must_be_one(self):
        events = [*self._open_step(),
                  ev(2, 102, "request/header",
                     {"header": {"config": {"provider": "fake"}},
                      "reason": "initial"}),
                  self._retry(3, retry=2)]
        self.assertRaisesRegex(
            Exception, re.escape("llm/retry must use retry 1"),
            assert_released_artifact_relationships, artifact(events))

    def test_must_preserve_retry_id(self):
        events = [*self._open_step(),
                  ev(2, 102, "request/header",
                     {"header": {"config": {"provider": "fake"}},
                      "reason": "initial"}),
                  self._retry(3, retryId="r1", retry=1),
                  self._retry(4, retryId="r2", retry=2)]
        self.assertRaisesRegex(
            Exception,
            re.escape("llm/retry must preserve retryId across one policy chain"),
            assert_released_artifact_relationships, artifact(events))

    def test_reuses_retry_id_across_policy_chains(self):
        events = [*self._open_step(),
                  ev(2, 102, "request/header",
                     {"header": {"config": {"provider": "fake"}},
                      "reason": "initial"}),
                  self._retry(3, retryId="r1", policyKey="a", retry=1),
                  self._retry(4, retryId="r1", policyKey="b", retry=1)]
        self.assertRaisesRegex(
            Exception,
            re.escape('llm/retry reuses retryId "r1" across policy chains'),
            assert_released_artifact_relationships, artifact(events))

    def test_retry_started_without_scheduled(self):
        events = [*self._open_step(),
                  ev(2, 102, "llm/retry-started",
                     {"retryId": "rx", "turn": 1, "step": 1, "retry": 1})]
        self.assertRaisesRegex(
            Exception,
            re.escape("llm/retry-started pairs no prior scheduled attempt"),
            assert_released_artifact_relationships, artifact(events))

    def test_retry_started_turn_step_mismatch(self):
        events = [*self._open_step(),
                  ev(2, 102, "request/header",
                     {"header": {"config": {"provider": "fake"}},
                      "reason": "initial"}),
                  self._retry(3),
                  ev(4, 104, "llm/retry-started",
                     {"retryId": "r1", "turn": 2, "step": 1, "retry": 1})]
        self.assertRaisesRegex(
            Exception,
            re.escape("llm/retry-started does not match its scheduled turn and step"),
            assert_released_artifact_relationships, artifact(events))

    def test_retry_started_repeats(self):
        events = [*self._open_step(),
                  ev(2, 102, "request/header",
                     {"header": {"config": {"provider": "fake"}},
                      "reason": "initial"}),
                  self._retry(3),
                  ev(4, 104, "llm/retry-started",
                     {"retryId": "r1", "turn": 1, "step": 1, "retry": 1}),
                  ev(5, 105, "llm/retry-started",
                     {"retryId": "r1", "turn": 1, "step": 1, "retry": 1})]
        self.assertRaisesRegex(
            Exception,
            re.escape("llm/retry-started repeats one scheduled attempt"),
            assert_released_artifact_relationships, artifact(events))


class PtcErrorsTest(unittest.TestCase):
    def _open_step(self):
        return [ev(0, 100, "turn/start", {"turn": 1}),
                ev(1, 101, "step/start", {"turn": 1, "step": 1})]

    def test_start_outside_turn(self):
        events = [ev(0, 100, "tool/code-dispatch-start",
                     {"rootCallId": "r", "parentCallId": "r", "subCallId": "s",
                      "name": "run", "arguments": "{}"})]
        self.assertRaisesRegex(
            Exception, re.escape("tool/code-dispatch-start is outside an open turn"),
            assert_released_artifact_relationships, artifact(events))

    def test_changes_root(self):
        events = [*self._open_step(),
                  ev(2, 102, "tool/code-dispatch-start",
                     {"rootCallId": "r", "parentCallId": "r", "subCallId": "s",
                      "name": "run", "arguments": "{}"}),
                  ev(3, 103, "tool/code-dispatch-start",
                     {"rootCallId": "r2", "parentCallId": "r2", "subCallId": "s",
                      "name": "run", "arguments": "{}"})]
        self.assertRaisesRegex(
            Exception, re.escape("tool/code-dispatch-start changes its rootCallId"),
            assert_released_artifact_relationships, artifact(events))

    def test_parent_not_in_root(self):
        events = [*self._open_step(),
                  ev(2, 102, "tool/code-dispatch-start",
                     {"rootCallId": "r", "parentCallId": "p", "subCallId": "s",
                      "name": "run", "arguments": "{}"})]
        self.assertRaisesRegex(
            Exception,
            re.escape("tool/code-dispatch-start parentCallId does not belong to "
                      "rootCallId"),
            assert_released_artifact_relationships, artifact(events))

    def test_repeats_sub_call_id(self):
        events = [*self._open_step(),
                  ev(2, 102, "tool/code-dispatch-start",
                     {"rootCallId": "r", "parentCallId": "r", "subCallId": "s",
                      "name": "run", "arguments": "{}"}),
                  ev(3, 103, "tool/code-dispatch-start",
                     {"rootCallId": "r", "parentCallId": "r", "subCallId": "s",
                      "name": "run", "arguments": "{}"})]
        self.assertRaisesRegex(
            Exception,
            re.escape("tool/code-dispatch-start repeats subCallId"),
            assert_released_artifact_relationships, artifact(events))

    def test_dispatch_without_start(self):
        events = [*self._open_step(),
                  ev(2, 102, "tool/code-dispatch",
                     {"rootCallId": "r", "parentCallId": "r", "subCallId": "s",
                      "name": "run", "arguments": "{}", "isError": False,
                      "content": []})]
        self.assertRaisesRegex(
            Exception, re.escape("tool/code-dispatch has no unique start"),
            assert_released_artifact_relationships, artifact(events))

    def test_dispatch_mismatch_start(self):
        events = [*self._open_step(),
                  ev(2, 102, "tool/code-dispatch-start",
                     {"rootCallId": "r", "parentCallId": "r", "subCallId": "s",
                      "name": "run", "arguments": "{}"}),
                  ev(3, 103, "tool/code-dispatch",
                     {"rootCallId": "r", "parentCallId": "r", "subCallId": "s",
                      "name": "other", "arguments": "{}", "isError": False,
                      "content": []})]
        self.assertRaisesRegex(
            Exception, re.escape("tool/code-dispatch does not match its start"),
            assert_released_artifact_relationships, artifact(events))

    def test_dispatch_repeat_after_settle(self):
        events = [*self._open_step(),
                  ev(2, 102, "tool/code-dispatch-start",
                     {"rootCallId": "r", "parentCallId": "r", "subCallId": "s",
                      "name": "run", "arguments": "{}"}),
                  ev(3, 103, "tool/code-dispatch",
                     {"rootCallId": "r", "parentCallId": "r", "subCallId": "s",
                      "name": "run", "arguments": "{}", "isError": False,
                      "content": []}),
                  ev(4, 104, "tool/code-dispatch",
                     {"rootCallId": "r", "parentCallId": "r", "subCallId": "s",
                      "name": "run", "arguments": "{}", "isError": False,
                      "content": []})]
        self.assertRaisesRegex(
            Exception, re.escape("tool/code-dispatch has no unique start"),
            assert_released_artifact_relationships, artifact(events))


class CommandErrorsTest(unittest.TestCase):
    def test_run_repeats(self):
        events = [
            ev(0, 100, "command/run",
               {"commandId": "c1", "name": "inspect", "source": {"kind": "user"}}),
            ev(1, 101, "command/run",
               {"commandId": "c1", "name": "inspect", "source": {"kind": "user"}}),
        ]
        self.assertRaisesRegex(
            Exception, re.escape("command/run repeats commandId c1"),
            assert_released_artifact_relationships, artifact(events))

    def test_done_without_run(self):
        events = [ev(0, 100, "command/done", {"commandId": "cx", "kind": "success"})]
        self.assertRaisesRegex(
            Exception, re.escape("command/done cx has no prior command/run"),
            assert_released_artifact_relationships, artifact(events))

    def test_done_invalid_source_event_seq(self):
        events = [
            ev(0, 100, "command/run",
               {"commandId": "c1", "name": "inspect", "source": {"kind": "user"}}),
            ev(1, 101, "command/done",
               {"commandId": "c1", "kind": "success", "sourceEventSeq": 0}),
        ]
        self.assertRaisesRegex(
            Exception, re.escape("command/done c1 has invalid sourceEventSeq"),
            assert_released_artifact_relationships, artifact(events))

    def test_done_invalid_kind(self):
        events = [
            user_message(0),
            ev(1, 101, "command/run",
               {"commandId": "c1", "name": "inspect", "source": {"kind": "user"}}),
            ev(2, 102, "command/done",
               {"commandId": "c1", "kind": "error", "sourceEventSeq": 0}),
        ]
        self.assertRaisesRegex(
            Exception, re.escape("command/done c1 has invalid sourceEventSeq"),
            assert_released_artifact_relationships, artifact(events))


class TitleErrorsTest(unittest.TestCase):
    def test_user_title_with_seqs(self):
        events = [
            user_message(0),
            ev(1, 101, "session/title",
               {"title": "T", "messageSeqs": [0], "source": {"kind": "user"}}),
        ]
        self.assertRaisesRegex(
            Exception,
            re.escape("session/title 1 messageSeqs must be empty exactly for a user "
                      "title"),
            assert_released_artifact_relationships, artifact(events))

    def test_title_source_must_be_json_object(self):
        events = [
            ev(0, 100, "session/title",
               {"title": "T", "messageSeqs": [], "source": "nope"}),
        ]
        self.assertRaisesRegex(
            Exception, re.escape("session/title 0 source must be a JSON object"),
            assert_released_artifact_relationships, artifact(events))

    def test_message_seqs_must_cite_user_message(self):
        events = [
            ev(0, 100, "feedback/record", {"text": "x"}),
            ev(1, 101, "session/title",
               {"title": "T", "messageSeqs": [0], "source": {"kind": "model"}}),
        ]
        self.assertRaisesRegex(
            Exception,
            re.escape("session/title 1 messageSeqs must cite earlier human "
                      "user/message events"),
            assert_released_artifact_relationships, artifact(events))

    def test_message_seqs_must_cite_human_source(self):
        events = [
            user_message(0, source={"kind": "plugin", "plugin": "p"}),
            ev(1, 101, "session/title",
               {"title": "T", "messageSeqs": [0], "source": {"kind": "model"}}),
        ]
        self.assertRaisesRegex(
            Exception,
            re.escape("session/title 1 messageSeqs must cite earlier human "
                      "user/message events"),
            assert_released_artifact_relationships, artifact(events))

    def test_title_llm_request_shape_mismatch(self):
        events = [
            user_message(0),
            ev(1, 101, "session/title-llm-request",
               {"titleProvider": "p", "messageSeqs": [0], "route": "r",
                "system": "s",
                "messages": [{"role": "user", "content": [text_block("x")],
                              "source": {"kind": "model"}}],
                "maxTokens": 10}),
        ]
        self.assertRaisesRegex(
            Exception,
            re.escape("session/title-llm-request messages do not represent messageSeqs"),
            assert_released_artifact_relationships, artifact(events))

    def test_title_llm_request_wrong_framing(self):
        events = [
            user_message(0),
            ev(1, 101, "session/title-llm-request",
               {"titleProvider": "p", "messageSeqs": [0], "route": "r",
                "system": "s",
                "messages": [{"role": "user", "content": [text_block("wrong")],
                              "source": {"kind": "plugin",
                                         "plugin": "dsh-session-title-llm"}}],
                "maxTokens": 10}),
        ]
        self.assertRaisesRegex(
            Exception,
            re.escape("session/title-llm-request messages do not represent messageSeqs"),
            assert_released_artifact_relationships, artifact(events))


class DeliveryAndDataErrorsTest(unittest.TestCase):
    def test_delivery_marker_wrong_session(self):
        events = [
            ev(0, 100, "session-log-deepseek/delivery-accepted",
               {"sessionId": "other", "throughSeq": 0, "sessionFormatVersion": 1}),
        ]
        self.assertRaisesRegex(
            Exception,
            re.escape("current-generation delivery marker names the wrong Session"),
            assert_released_artifact_relationships, artifact(events, version=1))

    def test_data_must_be_json_object(self):
        events = [ev(0, 100, "feedback/record", "not-an-object")]
        self.assertRaisesRegex(
            Exception, re.escape("feedback/record 0 data must be a JSON object"),
            assert_released_artifact_relationships, artifact(events))


class ResidualBranchTest(unittest.TestCase):
    def test_unknown_event_skipped(self):
        events = [ev(0, 100, "mystery/event", {})]
        assert_released_artifact_relationships(artifact(events))

    def test_assistant_message_text_only(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            assistant_message(2, content=[text_block("hi")]),
            ev(3, 103, "step/end", {"turn": 1, "step": 1}),
            ev(4, 104, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        assert_released_artifact_relationships(artifact(events))

    def test_compaction_source_command_id_round_trip(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "compaction/start",
               {"compactionId": "k1", "turn": 1, "sourceCommandId": "cmd"}),
            ev(3, 103, "compaction/end",
               {"compactionId": "k1", "turn": 1, "sourceCommandId": "cmd",
                "error": {"message": "x", "code": "E"}}),
            ev(4, 104, "step/end", {"turn": 1, "step": 1}),
            ev(5, 105, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        assert_released_artifact_relationships(artifact(events))

    def test_compaction_start_without_turn_inside_turn(self):
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "compaction/start", {"compactionId": "k1"}),
        ]
        self.assertRaisesRegex(
            Exception, re.escape("compaction/start does not match the open turn"),
            assert_released_artifact_relationships, artifact(events))

    def test_title_llm_request_empty_messages(self):
        events = [
            user_message(0),
            ev(1, 101, "session/title-llm-request",
               {"titleProvider": "p", "messageSeqs": [0], "route": "r",
                "system": "s", "messages": [], "maxTokens": 10}),
        ]
        self.assertRaisesRegex(
            Exception,
            re.escape("session/title-llm-request messages do not represent messageSeqs"),
            assert_released_artifact_relationships, artifact(events))


if __name__ == "__main__":
    unittest.main()


