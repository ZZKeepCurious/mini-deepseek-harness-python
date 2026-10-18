"""released v3 / v4 深度校验测试（validate_v3.py + validate_v4.py 专项）。

覆盖上游 `session-format-v2-to-v3/src/{validation,payload}.ts` 与
`session-format-v3-to-v4/src/{validation,developer,system-message,retired-syntax,
fork-result}.ts` 的关键行为钉（消息措辞逐字对齐 helpers.py 的 exact_keys 方言）：

  * v3/v4 header 精确逻辑头（field 无引号、missing-first、版本门槛、复用 v2 字段）；
  * v3 信封（surfaceOp 形状/端点、assistant 禁 sources、structural 行、canonical
    载荷）+ 系统头保护（首 surface head、replace 恰指 head、不得遮蔽、compaction 不得
    遮蔽）+ 开步匹配 + marker/cut 双向 + 安装门；
  * v3 关系投影（ptc 改名、废弃 ignorable 转 opaque、system→user、TOOL_NOT_STARTED
    修复 id 后缀换目标 seq）；
  * v4 信封 + 五 surface 类型 + system/developer/tool-result 消息形状 + retired
    tool-result 包裹块拒绝 + developer headerSeq 绑定 + fork 合成 not-started 结果；
  * physical / current / target 三档与 restore 安装门；
  * SURFACE_TYPES_V4 行为。

真实制品 + 逐字断言，无 mock。
"""
import re
import unittest

from miniharness.core.session.released import (
    RELEASED_V3_EVENT_TYPES,
    RELEASED_V4_EVENT_TYPES,
    SURFACE_TYPES_V4,
    SessionFormatError,
    SessionFormatUnsupportedMigrationError,
    assert_released_v3_artifact,
    assert_released_v3_header,
    assert_released_v3_physical_artifact,
    assert_released_v4_artifact,
    assert_released_v4_header,
    assert_released_v4_physical_artifact,
    assert_v4_event,
    restore_released_v3_artifact,
    restore_released_v4_artifact,
)
from miniharness.core.session.released.validate_v3 import (
    SURFACE_TYPES_V3,
    _assert_v3_event_admission as v3_admission,
    _is_repair_identity,
    _relationship_projection as v3_projection,
    assert_v3_event,
)

from miniharness.core.session.released.validate_v4 import (
    _assert_retired_content as v4_retired,
)
from miniharness.core.session.types import KNOWN_TYPES

INTERRUPTED_TEXT = ("The tool call was interrupted before the Harness recorded it as "
                    "started. Retry it if it is still needed.")


def ev(seq, time, etype, data, **extra):
    return {"type": etype, "seq": seq, "time": time, "data": data, **extra}


def text_block(text):
    return {"type": "text", "text": text}


def tool_call_block(call_id, name, arguments):
    return {"type": "tool-call", "id": call_id, "name": name, "arguments": arguments}


def model_source(provider="fake", model="fake", **extra):
    return {"kind": "model", "provider": provider, "model": model, **extra}


def text_stream(time0, text="hello"):
    return [
        {"type": "text-chunks", "time0": time0, "index": 0, "dt": [], "texts": [text]},
        {"type": "chunk", "time": time0 + 10,
         "chunk": {"type": "finish", "reason": {"kind": "stop"}}},
    ]


def tool_stream(time0, call_id="call-1", name="read", arguments="{}"):
    return [
        {"type": "tool-call-chunks", "time0": time0, "index": 0, "dt": [],
         "id": call_id, "name": name, "args": [arguments]},
        {"type": "chunk", "time": time0 + 10,
         "chunk": {"type": "finish", "reason": {"kind": "stop"}}},
    ]


def mixed_stream(time0, call_id="call-1", name="read", arguments="{}", text="hello"):
    return [
        {"type": "text-chunks", "time0": time0, "index": 0, "dt": [], "texts": [text]},
        {"type": "tool-call-chunks", "time0": time0 + 1, "index": 1, "dt": [],
         "id": call_id, "name": name, "args": [arguments]},
        {"type": "chunk", "time": time0 + 2,
         "chunk": {"type": "finish", "reason": {"kind": "stop"}}},
    ]


# ---------------------------------------------------------------- v3 builders

def v3_header(**overrides):
    header = {"version": 3, "id": "s-v3", "createdAt": 1, "isSeeded": False,
              "delegationDepth": 0}
    header.update(overrides)
    return header


def v3_artifact(events, inherited=0, **header_overrides):
    return {"header": v3_header(**header_overrides),
            "inherited_event_count": inherited, "events": events}


def v3_system_data(content=None):
    return {"turn": 1, "step": 1,
            "message": {"id": "sys1", "role": "system",
                        "content": [] if content is None else content,
                        "source": {"kind": "plugin",
                                   "plugin": "@deepseek-ai/dsh-system-prompt"}}}


def v3_user_data(text="hi", ident="u1"):
    return {"id": ident, "role": "user", "content": [text_block(text)],
            "source": {"kind": "user"}}


def v3_assistant_data(seq, content=None, stream=None, usage=None, interrupted=False):
    data = {"turn": 1, "step": 1,
            "message": {"id": f"am{seq}", "role": "assistant",
                        "content": content if content is not None
                        else [text_block("hello"), tool_call_block("call-1", "read", "{}")],
                        "source": model_source()},
            "stream": stream if stream is not None else mixed_stream(100)}
    if usage is not None:
        data["usage"] = usage
    if interrupted:
        data["interrupted"] = True
    return data


def v3_tool_result_data(call_id="call-1", result_text="out", ident=None):
    return {"turn": 1, "step": 1,
            "message": {"id": ident or f"r-{call_id}", "role": "user",
                        "content": [{"type": "tool-result", "toolCallId": call_id,
                                     "content": [text_block(result_text)]}],
                        "source": {"kind": "tool", "callId": call_id}}}


def v3_throughput(inherited=0, is_seeded=False, **header_overrides):
    events = [
        ev(0, 100, "turn/start", {"turn": 1}),
        ev(1, 101, "step/start", {"turn": 1, "step": 1}),
        ev(2, 102, "system/message", v3_system_data(), surfaceOp="append"),
        ev(3, 103, "user/message", v3_user_data(), surfaceOp="append"),
        ev(4, 104, "assistant/message", v3_assistant_data(4), surfaceOp="append"),
        ev(5, 105, "tool/call", {"turn": 1, "step": 1, "callId": "call-1",
                                 "name": "read", "arguments": "{}"}),
        ev(6, 106, "tool/result", v3_tool_result_data(), surfaceOp="append"),
        ev(7, 107, "step/end", {"turn": 1, "step": 1}),
        ev(8, 108, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
    ]
    return {"header": v3_header(isSeeded=is_seeded, **header_overrides),
            "inherited_event_count": inherited, "events": events}


# ---------------------------------------------------------------- v4 builders

def v4_header(**overrides):
    header = {"version": 4, "id": "s-v4", "createdAt": 1, "isSeeded": False,
              "delegationDepth": 0}
    header.update(overrides)
    return header


def v4_artifact(events, inherited=0, **header_overrides):
    return {"header": v4_header(**header_overrides),
            "inherited_event_count": inherited, "events": events}


def v4_system_data(content=None, kind="system-prompt", ident="sys1"):
    return {"turn": 1, "step": 1,
            "message": {"id": ident, "role": "system",
                        "content": [] if content is None else content,
                        "source": {"kind": kind}}}


def v4_user_data(text="hi", ident="u1", source=None):
    return {"id": ident, "role": "user", "content": [text_block(text)],
            "source": source if source is not None else {"kind": "user"}}


def v4_assistant_data(seq, content=None, stream=None):
    return {"turn": 1, "step": 1,
            "message": {"id": f"am{seq}", "role": "assistant",
                        "content": content if content is not None
                        else [text_block("hello"), tool_call_block("call-1", "read", "{}")],
                        "source": model_source()},
            "stream": stream if stream is not None else mixed_stream(100)}


def v4_tool_result_data(call_id="call-1", result_text="out", ident=None):
    return {"turn": 1, "step": 1,
            "message": {"id": ident if ident is not None else f"r-{call_id}", "role": "tool",
                        "toolCallId": call_id,
                        "content": [text_block(result_text)],
                        "source": {"kind": "tool", "callId": call_id}}}


def v4_throughput(inherited=0, is_seeded=False, **header_overrides):
    events = [
        ev(0, 100, "turn/start", {"turn": 1}),
        ev(1, 101, "step/start", {"turn": 1, "step": 1}),
        ev(2, 102, "system/message", v4_system_data(), surfaceOp="append"),
        ev(3, 103, "user/message", v4_user_data(), surfaceOp="append"),
        ev(4, 104, "assistant/message", v4_assistant_data(4), surfaceOp="append"),
        ev(5, 105, "tool/call", {"turn": 1, "step": 1, "callId": "call-1",
                                 "name": "read", "arguments": "{}"}),
        ev(6, 106, "tool/result", v4_tool_result_data(), surfaceOp="append"),
        ev(7, 107, "step/end", {"turn": 1, "step": 1}),
        ev(8, 108, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
    ]
    return {"header": v4_header(isSeeded=is_seeded, **header_overrides),
            "inherited_event_count": inherited, "events": events}


class V3HeaderTest(unittest.TestCase):
    def test_valid_header_passes(self):
        assert_released_v3_header(dict(v3_header()))

    def test_non_object(self):
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v3 header must be an object"),
                               assert_released_v3_header, ["not", "object"])
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v3 header must be an object"),
                               assert_released_v3_header, None)

    def test_missing_required_field(self):
        header = dict(v3_header())
        del header["id"]
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v3 header lacks required field id"),
                               assert_released_v3_header, header)

    def test_unexpected_field(self):
        header = dict(v3_header())
        header["flavor"] = "x"
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v3 header has unexpected field flavor"),
                               assert_released_v3_header, header)

    def test_wrong_version(self):
        header = dict(v3_header())
        header["version"] = 2
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("expected format v3 header"),
                               assert_released_v3_header, header)

    def test_reuses_v2_field_validation(self):
        header = dict(v3_header())
        header["createdAt"] = -1
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v2 header createdAt must be a non-negative safe integer"),
            assert_released_v3_header, header)
        header = dict(v3_header())
        header["isSeeded"] = 1
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v2 header isSeeded must be boolean"),
                               assert_released_v3_header, header)
        header = dict(v3_header())
        header["cwd"] = "relative"
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v2 header cwd must be absolute"),
                               assert_released_v3_header, header)
        header = dict(v3_header())
        header["origin"] = "parent"
        self.assertRaisesRegex(SessionFormatError,
                               re.escape('format v2 header origin must be "subagent"'),
                               assert_released_v3_header, header)


class V3EnvelopeTest(unittest.TestCase):
    def test_event_non_object(self):
        artifact = v3_throughput()
        artifact["events"][0] = "not an event"
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v3 event 0 must be an object"),
                               assert_released_v3_artifact, artifact)

    def test_missing_event_field(self):
        event = ev(0, 100, "turn/start", {"turn": 1})
        del event["data"]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 turn/start at seq 0 lacks required field data"),
            assert_released_v3_artifact, v3_artifact([event]))

    def test_unexpected_event_field(self):
        event = ev(0, 100, "turn/start", {"turn": 1})
        event["extra"] = 1
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 turn/start at seq 0 has unexpected field extra"),
            assert_released_v3_artifact, v3_artifact([event]))

    def test_type_not_string(self):
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v3 7 at seq 0 type must be a string"),
                               assert_v3_event, ev(0, 100, 7, {"turn": 1}))

    def test_seq_not_safe_integer(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 turn/start at seq -1 seq must be a non-negative safe integer"),
            assert_v3_event, ev(-1, 100, "turn/start", {"turn": 1}))

    def test_time_not_safe_integer(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 turn/start at seq 0 time must be a safe integer"),
            assert_v3_event, ev(0, 2 ** 53, "turn/start", {"turn": 1}))

    def test_ignorable_must_be_true(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 turn/start at seq 0 ignorable must be true when present"),
            assert_v3_event, ev(0, 100, "turn/start", {"turn": 1}, ignorable=1))

    def test_surface_requires_surface_op(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 user/message at seq 0 requires a surfaceOp marker"),
            assert_v3_event, ev(0, 100, "user/message", v3_user_data()))

    def test_surface_op_wrong_shape(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 user/message at seq 0 requires exact replace fields "
                      "op/startSeq/endSeq"),
            assert_v3_event,
            ev(0, 100, "user/message", v3_user_data(),
               surfaceOp={"op": "replace", "startSeq": 0}))

    def test_surface_op_endpoint_not_earlier(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 user/message at seq 2 surfaceOp startSeq must be a "
                      "non-negative safe integer"),
            assert_v3_event,
            ev(2, 100, "user/message", v3_user_data(),
               surfaceOp={"op": "replace", "startSeq": "x", "endSeq": 0}))
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 user/message at seq 2 replacement endpoints must "
                      "reference earlier events"),
            assert_v3_event,
            ev(2, 100, "user/message", v3_user_data(),
               surfaceOp={"op": "replace", "startSeq": 0, "endSeq": 2}))

    def test_assistant_message_forbids_sources(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 assistant/message at seq 0 embeds its stream and cannot "
                      "carry sourceEventSeqs"),
            assert_v3_event,
            ev(0, 100, "assistant/message", v3_assistant_data(0),
               surfaceOp="append", sourceEventSeqs=[0]))

    def test_source_event_seqs_must_be_non_empty_array(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 user/message at seq 0 sourceEventSeqs must be a "
                      "non-empty array"),
            assert_v3_event,
            ev(0, 100, "user/message", v3_user_data(), surfaceOp="append",
               sourceEventSeqs=[]))

    def test_source_event_seqs_members_unique_earlier(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 user/message at seq 3 sourceEventSeqs must be unique "
                      "earlier seqs"),
            assert_v3_event,
            ev(3, 100, "user/message", v3_user_data(), surfaceOp="append",
               sourceEventSeqs=[1, 1]))
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 user/message at seq 3 sourceEventSeqs must be unique "
                      "earlier seqs"),
            assert_v3_event,
            ev(3, 100, "user/message", v3_user_data(), surfaceOp="append",
               sourceEventSeqs=[3]))

    def test_request_header_rejects_retired_system(self):
        self.assertRaisesRegex(
            SessionFormatUnsupportedMigrationError,
            re.escape("format v3 request/header rejects retired header.system"),
            assert_v3_event,
            ev(0, 100, "request/header",
               {"header": {"config": {"provider": "fake", "model": "fake"},
                           "system": "legacy"}, "reason": "initial"}))

    def test_request_header_empty_optional_fields(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 request/header at seq 0 empty optional header fields "
                      "must be omitted"),
            assert_v3_event,
            ev(0, 100, "request/header",
               {"header": {"config": {"provider": "fake", "model": "fake"}, "tools": []},
                "reason": "initial"}))

    def test_tool_result_error_metadata_mismatch(self):
        data = v3_tool_result_data()
        data["error"] = {"name": "ToolError", "code": "TOOL_ERROR"}
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 tool/result at seq 0 carries error metadata for a "
                      "non-error tool result"),
            assert_v3_event, ev(0, 100, "tool/result", data, surfaceOp="append"))

    def test_obsolete_required_refused(self):
        self.assertRaisesRegex(
            SessionFormatUnsupportedMigrationError,
            re.escape('format v3 contains unknown event type "tool/code-dispatch" at seq 0'),
            v3_admission, ev(0, 100, "tool/code-dispatch", {}))

    def test_obsolete_ignorable_admitted(self):
        v3_admission(ev(0, 100, "tool/code-dispatch", {}, ignorable=True))


class V3SystemShapeTest(unittest.TestCase):
    def _assert_shape(self, data):
        return assert_v3_event(ev(0, 100, "system/message", data, surfaceOp="append"))

    def test_missing_coordinate(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape('system/message data lacks required member "message"'),
            self._assert_shape, {"turn": 1, "step": 1})

    def test_zero_coordinate(self):
        data = v3_system_data()
        data["step"] = 0
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("system/message data step must be positive"),
            self._assert_shape, data)

    def test_message_not_object(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("system/message data system message must be a JSON object"),
            self._assert_shape, {"turn": 1, "step": 1, "message": "x"})

    def test_message_missing_field(self):
        message = v3_system_data()["message"]
        del message["role"]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape('system/message data system message lacks required member "role"'),
            self._assert_shape, {"turn": 1, "step": 1, "message": message})

    def test_bad_id_or_role(self):
        data = v3_system_data()
        data["message"]["id"] = ""
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("system/message data system message requires an id and system role"),
            self._assert_shape, data)

    def test_source_not_plugin(self):
        data = v3_system_data()
        data["message"]["source"] = {"kind": "user"}
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("system/message data system message requires plugin source"),
            self._assert_shape, data)


class V3HappyPathTest(unittest.TestCase):
    def test_target_passes(self):
        assert_released_v3_artifact(v3_throughput())

    def test_physical_passes(self):
        assert_released_v3_physical_artifact(v3_throughput())

    def test_restore_current_passes(self):
        artifact = v3_throughput()
        self.assertIs(restore_released_v3_artifact(artifact, frozenset(RELEASED_V3_EVENT_TYPES)),
                      artifact)

    def test_attempt_inside_step_passes(self):
        artifact = v3_artifact([
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 110, "assistant/attempt",
               {"turn": 1, "step": 1, "stream": text_stream(102)}),
            ev(3, 120, "step/end", {"turn": 1, "step": 1}),
            ev(4, 130, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        assert_released_v3_artifact(artifact)

    def test_system_head_replace_passes(self):
        artifact = v3_artifact([
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "system/message", v3_system_data(), surfaceOp="append"),
            ev(3, 103, "user/message", v3_user_data(), surfaceOp="append"),
            ev(4, 104, "system/message", v3_system_data(content=[text_block("v2")]),
               surfaceOp={"op": "replace", "startSeq": 2, "endSeq": 2},
               sourceEventSeqs=[2]),
            ev(5, 105, "step/end", {"turn": 1, "step": 1}),
            ev(6, 106, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        assert_released_v3_artifact(artifact)

    def test_seeded_zero_cut_marker_passes(self):
        artifact = v3_artifact([
            ev(0, 100, "session/end-seed", {"inherited": True}),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            ev(3, 103, "user/message", v3_user_data(), surfaceOp="append"),
            ev(4, 104, "assistant/message",
               v3_assistant_data(4, content=[text_block("hello")],
                                 stream=text_stream(102)), surfaceOp="append"),
            ev(5, 105, "step/end", {"turn": 1, "step": 1}),
            ev(6, 106, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ], inherited=0, isSeeded=True)
        assert_released_v3_artifact(artifact)


class V3MarkerCutTest(unittest.TestCase):
    def test_cut_exceeds_events(self):
        artifact = v3_throughput()
        artifact["inherited_event_count"] = 99
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 inherited event count exceeds its events"),
            assert_released_v3_artifact, artifact)

    def test_unseeded_with_inherited_events(self):
        artifact = v3_throughput()
        artifact["inherited_event_count"] = 1
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("unseeded format v3 Session has inherited events"),
            assert_released_v3_artifact, artifact)

    def test_seeded_marker_disagreement(self):
        events = v3_throughput()["events"]
        events = [events[0], ev(1, 101, "session/end-seed", {"inherited": True}),
                  *[{**e, "seq": e["seq"] + 1} for e in events[1:]]]
        artifact = v3_artifact(events, inherited=0, isSeeded=True)
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 seeded header disagrees with its last inherited "
                      "end-seed marker"),
            assert_released_v3_artifact, artifact)

    def test_unseeded_marker_refused(self):
        events = v3_throughput()["events"]
        events = [*events, ev(9, 140, "session/end-seed", {"inherited": True})]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 unseeded Session contains an inherited end-seed marker"),
            assert_released_v3_artifact, v3_artifact(events))

    def test_end_seed_inherited_must_be_true(self):
        events = v3_throughput()["events"]
        events = [events[0], ev(1, 101, "session/end-seed", {"inherited": False}),
                  *[{**e, "seq": e["seq"] + 1} for e in events[1:]]]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("session/end-seed 1 inherited must be true when present"),
            assert_released_v3_artifact, v3_artifact(events))


class V3DenseCoordinateTest(unittest.TestCase):
    def test_non_dense_seq(self):
        artifact = v3_throughput()
        artifact["events"][0]["seq"] = 1
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v3 event 0 is not dense"),
                               assert_released_v3_artifact, artifact)

    def test_time_not_safe_integer(self):
        artifact = v3_throughput()
        artifact["events"][0]["time"] = 2 ** 53
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 turn/start at seq 0 time must be a safe integer"),
            assert_released_v3_artifact, artifact)


class V3SystemHeadTest(unittest.TestCase):
    def test_system_message_must_match_open_step(self):
        artifact = v3_throughput()
        artifact["events"][2]["data"]["step"] = 2
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("system/message does not match an open step"),
                               assert_released_v3_artifact, artifact)

    def test_requires_protected_first_surface_head(self):
        artifact = v3_artifact([
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "user/message", v3_user_data(), surfaceOp="append"),
            ev(3, 103, "system/message", v3_system_data(), surfaceOp="append"),
            ev(4, 104, "step/end", {"turn": 1, "step": 1}),
            ev(5, 105, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("system/message requires a protected first surface head"),
            assert_released_v3_artifact, artifact)

    def test_system_replace_must_match_head_exactly(self):
        artifact = v3_artifact([
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "system/message", v3_system_data(), surfaceOp="append"),
            ev(3, 103, "user/message", v3_user_data(), surfaceOp="append"),
            ev(4, 104, "system/message", v3_system_data(),
               surfaceOp={"op": "replace", "startSeq": 2, "endSeq": 3}),
            ev(5, 105, "step/end", {"turn": 1, "step": 1}),
            ev(6, 106, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("system/message must replace exactly the current system head"),
            assert_released_v3_artifact, artifact)

    def test_surface_replacement_cannot_shadow_head(self):
        artifact = v3_artifact([
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "system/message", v3_system_data(), surfaceOp="append"),
            ev(3, 103, "user/message", v3_user_data(),
               surfaceOp={"op": "replace", "startSeq": 2, "endSeq": 2}),
            ev(4, 104, "step/end", {"turn": 1, "step": 1}),
            ev(5, 105, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("surface replacement cannot shadow the protected system head"),
            assert_released_v3_artifact, artifact)

    def test_compaction_cannot_shadow_head(self):
        artifact = v3_artifact([
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "system/message", v3_system_data(), surfaceOp="append"),
            ev(3, 103, "compaction/prune",
               {"shadowedRange": {"start": 2, "end": 2}, "shadowedSeqs": [2],
                "shadowedTokenCount": 0}),
            ev(4, 104, "step/end", {"turn": 1, "step": 1}),
            ev(5, 105, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("compaction cannot shadow the protected system head"),
            assert_released_v3_artifact, artifact)


class V3PayloadStreamTest(unittest.TestCase):
    def _with_assistant(self, data):
        artifact = v3_throughput()
        artifact["events"][4] = ev(4, 104, "assistant/message", data, surfaceOp="append")
        return artifact

    def test_content_disagreement(self):
        artifact = self._with_assistant(
            v3_assistant_data(4, content=[text_block("different")]))
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/message 4 message content disagrees with its embedded stream"),
            assert_released_v3_artifact, artifact)

    def test_usage_disagreement(self):
        stream = [
            {"type": "text-chunks", "time0": 100, "index": 0, "dt": [], "texts": ["hello"]},
            {"type": "chunk", "time": 110,
             "chunk": {"type": "usage", "usage": {"inputTokens": 10, "outputTokens": 5}}},
            {"type": "chunk", "time": 112,
             "chunk": {"type": "finish", "reason": {"kind": "stop"}}},
        ]
        artifact = self._with_assistant(
            v3_assistant_data(4, content=[text_block("hello")], stream=stream))
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/message 4 usage disagrees with its embedded stream"),
            assert_released_v3_artifact, artifact)

    def test_replay_state_disagreement(self):
        envelope = {"response": {"r": "a"}, "blocks": [{"k": "v"}]}
        stream = [
            {"type": "text-chunks", "time0": 100, "index": 0, "dt": [], "texts": ["hello"]},
            {"type": "chunk", "time": 112,
             "chunk": {"type": "finish", "reason": {"kind": "stop"},
                       "replayState": envelope}},
        ]
        data = v3_assistant_data(4, content=[text_block("hello")], stream=stream)
        data["message"]["source"] = model_source(replayState={"response": {"r": "zzz"},
                                                              "blocks": []})
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/message 4 replay state disagrees with its embedded stream"),
            assert_released_v3_artifact, self._with_assistant(data))

    def test_invalid_embedded_stream(self):
        artifact = self._with_assistant(
            v3_assistant_data(4, content=[text_block("x")],
                              stream=[{"type": "chunk", "time": 110,
                                       "chunk": {"type": "bogus"}}]))
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/message 4 has an invalid embedded stream"),
            assert_released_v3_artifact, artifact)

    def test_assistant_attempt_invalid_stream(self):
        artifact = v3_artifact([
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 110, "assistant/attempt",
               {"turn": 1, "step": 1,
                "stream": [{"type": "chunk", "time": 110,
                            "chunk": {"type": "bogus"}}]}),
            ev(3, 120, "step/end", {"turn": 1, "step": 1}),
            ev(4, 130, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/attempt 2 has an invalid embedded stream"),
            assert_released_v3_artifact, artifact)

    def test_opaque_meta_must_be_lossless(self):
        artifact = v3_throughput()
        artifact["events"][6]["data"]["meta"] = {"x": float("nan")}
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("tool/result 6 opaque meta is not lossless JSON"),
            assert_released_v3_artifact, artifact)


class V3ProjectionTest(unittest.TestCase):
    def test_ptc_rename_projection(self):
        start = ev(0, 100, "tool/ptc-dispatch-start",
                   {"rootCallId": "r", "parentCallId": "r", "subCallId": "c",
                    "name": "run", "arguments": "{}"})
        dispatch = ev(1, 101, "tool/ptc-dispatch",
                      {"rootCallId": "r", "parentCallId": "r", "subCallId": "c",
                       "name": "run", "arguments": "{}", "isError": False,
                       "content": [text_block("x")]})
        self.assertEqual(v3_projection(start, 0)["type"], "tool/code-dispatch-start")
        self.assertEqual(v3_projection(dispatch, 1)["type"], "tool/code-dispatch")

    def test_obsolete_required_projection_refused(self):
        with self.assertRaises(SessionFormatUnsupportedMigrationError):
            v3_projection(ev(0, 100, "tool/code-dispatch", {}), 0)

    def test_obsolete_ignorable_projection(self):
        event = ev(0, 100, "tool/code-dispatch", {}, ignorable=True)
        projected = v3_projection(event, 0)
        self.assertEqual(projected["type"], "v3/opaque-released-event")

    def test_repair_identity(self):
        self.assertTrue(_is_repair_identity("interrupted-tool-result-c-0", "c"))
        self.assertTrue(_is_repair_identity("interrupted-tool-result-c-12", "c"))
        self.assertFalse(_is_repair_identity("interrupted-tool-result-c-01", "c"))
        self.assertFalse(_is_repair_identity("interrupted-tool-result-d-3", "c"))
        self.assertFalse(_is_repair_identity("other-3", "c"))
        self.assertFalse(_is_repair_identity("interrupted-tool-result-c-3", 7))

    def test_tool_not_started_repair_projection(self):
        call_id = "call-1"
        ident = f"interrupted-tool-result-{call_id}-3"
        data = {"turn": 1, "step": 1,
                "error": {"name": "ToolNotStartedError", "code": "TOOL_NOT_STARTED"},
                "message": {"id": ident, "role": "user",
                            "content": [{"type": "tool-result", "toolCallId": call_id,
                                         "isError": True,
                                         "content": [text_block(INTERRUPTED_TEXT)]}],
                            "source": {"kind": "tool", "callId": call_id}}}
        event = ev(3, 103, "tool/result", data, surfaceOp="append")
        self.assertEqual(v3_projection(event, 3)["data"]["message"]["id"], ident)

    def test_repair_artifact_valid(self):
        call_id = "call-1"
        artifact = v3_artifact([
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "assistant/message",
               v3_assistant_data(2, content=[tool_call_block(call_id, "read", "{}")],
                                 stream=tool_stream(100, call_id)), surfaceOp="append"),
            ev(3, 103, "tool/result",
               {"turn": 1, "step": 1,
                "error": {"name": "ToolNotStartedError", "code": "TOOL_NOT_STARTED"},
                "message": {"id": f"interrupted-tool-result-{call_id}-3", "role": "user",
                            "content": [{"type": "tool-result", "toolCallId": call_id,
                                         "isError": True,
                                         "content": [text_block(INTERRUPTED_TEXT)]}],
                            "source": {"kind": "tool", "callId": call_id}}},
               surfaceOp="append"),
            ev(4, 104, "step/end", {"turn": 1, "step": 1}),
            ev(5, 105, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        assert_released_v3_artifact(artifact)

    def test_non_repair_id_refused_by_relationships(self):
        call_id = "call-1"
        artifact = v3_artifact([
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "assistant/message",
               v3_assistant_data(2, content=[tool_call_block(call_id, "read", "{}")],
                                 stream=tool_stream(100, call_id)), surfaceOp="append"),
            ev(3, 103, "tool/result",
               {"turn": 1, "step": 1,
                "error": {"name": "ToolNotStartedError", "code": "TOOL_NOT_STARTED"},
                "message": {"id": f"interrupted-tool-result-{call_id}-00", "role": "user",
                            "content": [{"type": "tool-result", "toolCallId": call_id,
                                         "isError": True,
                                         "content": [text_block(INTERRUPTED_TEXT)]}],
                            "source": {"kind": "tool", "callId": call_id}}},
               surfaceOp="append"),
            ev(4, 104, "step/end", {"turn": 1, "step": 1}),
            ev(5, 105, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("tool/result call-1 is not the exact TOOL_NOT_STARTED repair"),
            assert_released_v3_artifact, artifact)


class V3PhysicalCurrentTest(unittest.TestCase):
    def test_unknown_type_passes_physical(self):
        events = v3_throughput()["events"]
        events = [*events[:7], ev(7, 107, "future/event-type", {"anything": 1}),
                  *[{**e, "seq": e["seq"] + 1} for e in events[7:]]]
        assert_released_v3_physical_artifact(v3_artifact(events))

    def test_invalid_payload_passes_physical(self):
        artifact = v3_throughput()
        artifact["events"][4] = ev(4, 104, "assistant/message",
                                   v3_assistant_data(4, content=[text_block("x")],
                                                     stream=[{"type": "chunk", "time": 1,
                                                              "chunk": {"type": "bogus"}}]),
                                   surfaceOp="append")
        assert_released_v3_physical_artifact(artifact)

    def test_physical_still_checks_dense(self):
        artifact = v3_throughput()
        artifact["events"][0]["seq"] = 1
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v3 event 0 is not dense"),
                               assert_released_v3_physical_artifact, artifact)

    def test_physical_still_checks_time(self):
        artifact = v3_throughput()
        artifact["events"][0]["time"] = 2 ** 53
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v3 event 0 time must be a safe integer"),
            assert_released_v3_physical_artifact, artifact)

    def test_unknown_required_refused_current(self):
        events = v3_throughput()["events"]
        events = [*events[:7], ev(7, 107, "future/event-type", {"anything": 1}),
                  *[{**e, "seq": e["seq"] + 1} for e in events[7:]]]
        self.assertRaisesRegex(
            SessionFormatUnsupportedMigrationError,
            re.escape('format v2 contains unknown event type "future/event-type" at seq 7'),
            restore_released_v3_artifact, v3_artifact(events),
            frozenset(RELEASED_V3_EVENT_TYPES))

    def test_ignorable_unknown_accepted_current(self):
        events = v3_throughput()["events"]
        events = [*events[:7], ev(7, 107, "future/event-type", {"anything": 1},
                                  ignorable=True),
                  *[{**e, "seq": e["seq"] + 1} for e in events[7:]]]
        artifact = v3_artifact(events)
        self.assertIs(restore_released_v3_artifact(artifact,
                                                   frozenset(RELEASED_V3_EVENT_TYPES)),
                      artifact)

    def test_current_skips_payload_stream_check(self):
        artifact = v3_throughput()
        artifact["events"][4] = ev(4, 104, "assistant/message",
                                   v3_assistant_data(4, content=[text_block("x")],
                                                     stream=[{"type": "chunk", "time": 1,
                                                              "chunk": {"type": "bogus"}}]),
                                   surfaceOp="append")
        restore_released_v3_artifact(artifact, frozenset(RELEASED_V3_EVENT_TYPES))


class V4HeaderTest(unittest.TestCase):
    def test_valid_header_passes(self):
        assert_released_v4_header(dict(v4_header()))

    def test_non_object(self):
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v4 header must be an object"),
                               assert_released_v4_header, None)

    def test_missing_required_field(self):
        header = dict(v4_header())
        del header["delegationDepth"]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 header lacks required field delegationDepth"),
            assert_released_v4_header, header)

    def test_unexpected_field(self):
        header = dict(v4_header())
        header["extra"] = True
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 header has unexpected field extra"),
            assert_released_v4_header, header)

    def test_wrong_version(self):
        header = dict(v4_header())
        header["version"] = 3
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("expected format v4 header"),
                               assert_released_v4_header, header)

    def test_reuses_v2_field_validation(self):
        header = dict(v4_header())
        header["id"] = 5
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v2 header id must be a string"),
                               assert_released_v4_header, header)
        header = dict(v4_header())
        header["parentSession"] = None
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v2 header parentSession must be a string"),
                               assert_released_v4_header, header)


class SurfaceTypesV4Test(unittest.TestCase):
    def test_membership(self):
        self.assertIsInstance(SURFACE_TYPES_V4, frozenset)
        self.assertEqual(SURFACE_TYPES_V4, frozenset({
            "system/message", "developer/message", "user/message",
            "assistant/message", "tool/result"}))
        self.assertIn("system/message", SURFACE_TYPES_V3)
        self.assertNotIn("developer/message", SURFACE_TYPES_V3)

    def test_developer_requires_surface_op(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 developer/message at seq 0 requires a surfaceOp marker"),
            assert_v4_event,
            ev(0, 100, "developer/message",
               {"turn": 1, "step": 1,
                "message": {"id": "d1", "role": "developer", "content": [],
                            "source": {"kind": "runtime-context"}}}))


class V4EnvelopeTest(unittest.TestCase):
    def test_event_non_object(self):
        artifact = v4_throughput()
        artifact["events"][0] = 5
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v4 event 0 must be an object"),
                               assert_released_v4_artifact, artifact)

    def test_missing_event_field(self):
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v4 turn/start at seq 0 lacks required field time"),
                               assert_v4_event, {"type": "turn/start", "seq": 0,
                                                 "data": {"turn": 1}})

    def test_type_not_string(self):
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v4 7 at seq 0 type must be a string"),
                               assert_v4_event, ev(0, 100, 7, {"turn": 1}))

    def test_time_not_safe_integer(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 turn/start at seq 0 time must be a safe integer"),
            assert_v4_event, ev(0, 2 ** 53, "turn/start", {"turn": 1}))

    def test_ignorable_must_be_true(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 turn/start at seq 0 ignorable must be true when present"),
            assert_v4_event, ev(0, 100, "turn/start", {"turn": 1}, ignorable=1))

    def test_retired_event_admission(self):
        self.assertRaisesRegex(
            SessionFormatUnsupportedMigrationError,
            re.escape("format v4 rejects retired event type tool/code-dispatch"),
            assert_released_v4_artifact,
            v4_artifact([ev(0, 100, "tool/code-dispatch", {})]))

    def test_obsolete_ignorable_opaque_envelope_passes(self):
        assert_v4_event(ev(0, 100, "tool/code-dispatch", {}, ignorable=True))

    def test_assistant_message_forbids_sources(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 assistant/message at seq 0 embeds its stream and cannot "
                      "carry sourceEventSeqs"),
            assert_v4_event,
            ev(0, 100, "assistant/message", v4_assistant_data(0),
               surfaceOp="append", sourceEventSeqs=[0]))

    def test_surface_op_wrong_shape(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 user/message at seq 0 requires exact replace fields "
                      "op/startSeq/endSeq"),
            assert_v4_event,
            ev(0, 100, "user/message", v4_user_data(),
               surfaceOp={"op": "replace", "startSeq": 0}))

    def test_surface_op_endpoint_errors(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 user/message at seq 2 surfaceOp startSeq must be a "
                      "non-negative safe integer"),
            assert_v4_event,
            ev(2, 100, "user/message", v4_user_data(),
               surfaceOp={"op": "replace", "startSeq": "x", "endSeq": 0}))
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 user/message at seq 2 replacement endpoints must "
                      "reference earlier events"),
            assert_v4_event,
            ev(2, 100, "user/message", v4_user_data(),
               surfaceOp={"op": "replace", "startSeq": 0, "endSeq": 2}))

    def test_source_event_seqs_errors(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 user/message at seq 0 sourceEventSeqs must be a "
                      "non-empty array"),
            assert_v4_event,
            ev(0, 100, "user/message", v4_user_data(), surfaceOp="append",
               sourceEventSeqs=[]))
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 user/message at seq 3 sourceEventSeqs must be unique "
                      "earlier seqs"),
            assert_v4_event,
            ev(3, 100, "user/message", v4_user_data(), surfaceOp="append",
               sourceEventSeqs=[1, 1]))

    def test_surface_op_requires_marker(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 user/message at seq 0 requires a surfaceOp marker"),
            assert_v4_event, ev(0, 100, "user/message", v4_user_data()))

    def test_unknown_required_refused_current(self):
        events = v4_throughput()["events"]
        events = [*events[:7], ev(7, 107, "future/event-type", {"anything": 1}),
                  *[{**e, "seq": e["seq"] + 1} for e in events[7:]]]
        self.assertRaisesRegex(
            SessionFormatUnsupportedMigrationError,
            re.escape("format v4 contains unknown event type 'future/event-type' at seq 7"),
            restore_released_v4_artifact, v4_artifact(events),
            frozenset(RELEASED_V4_EVENT_TYPES))

    def test_ignorable_unknown_accepted_current(self):
        events = v4_throughput()["events"]
        events = [*events[:7], ev(7, 107, "future/event-type", {"anything": 1},
                                  ignorable=True),
                  *[{**e, "seq": e["seq"] + 1} for e in events[7:]]]
        artifact = v4_artifact(events)
        self.assertIs(restore_released_v4_artifact(artifact,
                                                   frozenset(RELEASED_V4_EVENT_TYPES)),
                      artifact)


class V4HappyPathTest(unittest.TestCase):
    def test_target_passes(self):
        assert_released_v4_artifact(v4_throughput())

    def test_physical_passes(self):
        assert_released_v4_physical_artifact(v4_throughput())

    def test_restore_current_passes(self):
        artifact = v4_throughput()
        self.assertIs(restore_released_v4_artifact(artifact,
                                                   frozenset(RELEASED_V4_EVENT_TYPES)),
                      artifact)

    def test_unknown_type_passes_physical(self):
        events = v4_throughput()["events"]
        events = [*events[:7], ev(7, 107, "future/event-type", {"anything": 1}),
                  *[{**e, "seq": e["seq"] + 1} for e in events[7:]]]
        assert_released_v4_physical_artifact(v4_artifact(events))


class V4WorkingDirectoryJobEventsTest(unittest.TestCase):
    """dsh-v0.2.1-rc.2 same-version v4 扩展：working-directory/change +
    job/registered + job/settled 三新事件的可读性（P0-1）。"""

    JOB = {"jobId": "bash-0123456789abcdef", "owner": "s-v4", "kind": "bash",
           "label": "sleep 1", "startedAt": 1000}

    def _artifact(self):
        registered = dict(self.JOB, toolCall={"callId": "call-1", "rootCallId": "call-1"})
        settled = dict(registered, finishedAt=2000, status="completed",
                       cause="producer", producerReported=True, detail="exit code: 0")
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "working-directory/change", {"cwd": "/work/proj"}),
            ev(3, 103, "job/registered", registered),
            ev(4, 104, "job/settled", settled),
            ev(5, 105, "user/message",
               v4_user_data("cwd changed", source={"kind": "working-directory"}),
               surfaceOp="append"),
            ev(6, 106, "step/end", {"turn": 1, "step": 1}),
            ev(7, 107, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        return v4_artifact(events)

    def test_new_types_are_known(self):
        for name in ("working-directory/change", "job/registered", "job/settled"):
            self.assertIn(name, KNOWN_TYPES)
            self.assertIn(name, RELEASED_V4_EVENT_TYPES)

    def test_target_passes(self):
        assert_released_v4_artifact(self._artifact())

    def test_restore_current_passes(self):
        artifact = self._artifact()
        self.assertIs(restore_released_v4_artifact(artifact, KNOWN_TYPES), artifact)

    def test_unknown_type_is_refused_on_current_restore(self):
        artifact = v4_artifact([ev(0, 100, "future/unknown", {"x": 1})])
        with self.assertRaises(SessionFormatUnsupportedMigrationError):
            restore_released_v4_artifact(artifact, KNOWN_TYPES)

    def test_working_directory_requires_cwd(self):
        self.assertRaisesRegex(SessionFormatError, "cwd",
                               assert_released_v4_artifact,
                               v4_artifact([ev(0, 100, "working-directory/change", {})]))

    def test_job_settled_rejects_unknown_status(self):
        settled = dict(self.JOB, finishedAt=2000, status="done",
                       cause="producer", producerReported=True)
        self.assertRaisesRegex(SessionFormatError, "status must be one of",
                               assert_released_v4_artifact,
                               v4_artifact([ev(0, 100, "job/settled", settled)]))

    def test_job_registered_requires_started_at(self):
        missing = {k: v for k, v in self.JOB.items() if k != "startedAt"}
        self.assertRaisesRegex(SessionFormatError, "startedAt",
                               assert_released_v4_artifact,
                               v4_artifact([ev(0, 100, "job/registered", missing)]))


class V4SystemShapeTest(unittest.TestCase):
    def test_valid_system_message(self):
        assert_released_v4_artifact(v4_artifact([
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "system/message", v4_system_data(), surfaceOp="append"),
            ev(3, 103, "step/end", {"turn": 1, "step": 1}),
            ev(4, 104, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]))

    def test_message_missing_field(self):
        message = v4_system_data()["message"]
        del message["content"]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("system/message data system message lacks required field content"),
            assert_v4_event,
            ev(0, 100, "system/message", {"turn": 1, "step": 1, "message": message},
               surfaceOp="append"))

    def test_content_must_be_array(self):
        data = v4_system_data()
        data["message"]["content"] = "x"
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("system/message data system message requires content array"),
            assert_v4_event, ev(0, 100, "system/message", data, surfaceOp="append"))

    def test_plugin_source_refused(self):
        data = v4_system_data(kind="plugin")
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("system/message data system source requires a producer-owned kind"),
            assert_v4_event, ev(0, 100, "system/message", data, surfaceOp="append"))

    def test_message_sources_plugin_kind_refused(self):
        data = v4_system_data()
        data["message"]["source"] = {}
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("system/message data system source requires a producer-owned kind"),
            assert_v4_event, ev(0, 100, "system/message", data, surfaceOp="append"))

    def test_zero_turn_refused(self):
        data = v4_system_data()
        data["turn"] = 0
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("system/message data turn must be positive"),
            assert_v4_event, ev(0, 100, "system/message", data, surfaceOp="append"))

    def test_id_and_role_required(self):
        data = v4_system_data()
        data["message"]["id"] = ""
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("system/message data system message requires an id and system role"),
            assert_v4_event, ev(0, 100, "system/message", data, surfaceOp="append"))
        data = v4_system_data()
        data["message"]["role"] = "user"
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("system/message data system message requires an id and system role"),
            assert_v4_event, ev(0, 100, "system/message", data, surfaceOp="append"))


class V4ToolResultShapeTest(unittest.TestCase):
    def _assert(self, data):
        return assert_v4_event(ev(0, 100, "tool/result", data, surfaceOp="append"))

    def test_role_must_be_tool(self):
        data = v4_tool_result_data()
        data["message"]["role"] = "user"
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("tool/result data requires a tool-role message"),
            self._assert, data)

    def test_id_required(self):
        data = v4_tool_result_data(ident="")
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("tool/result data requires a first-class message with a string id"),
            self._assert, data)

    def test_tool_call_id_must_match_source(self):
        data = v4_tool_result_data()
        data["message"]["toolCallId"] = "other"
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("tool/result data requires toolCallId matching its tool source"),
            self._assert, data)

    def test_content_must_be_array(self):
        data = v4_tool_result_data()
        data["message"]["content"] = "x"
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("tool/result data requires array content"),
            self._assert, data)

    def test_wrapper_block_refused(self):
        data = v4_tool_result_data()
        data["message"]["content"] = [{"type": "tool-result", "toolCallId": "call-1",
                                       "content": []}]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("tool/result data content must not contain a released tool-result wrapper"),
            self._assert, data)

    def test_is_error_must_be_boolean(self):
        data = v4_tool_result_data()
        data["message"]["isError"] = 1
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("tool/result data isError must be boolean when present"),
            self._assert, data)


class V4RetiredContentTest(unittest.TestCase):
    def _wrapper(self):
        return {"type": "tool-result", "toolCallId": "call-1", "content": []}

    def test_user_message_wrapper_refused(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 user/message at seq 0 content must not contain a "
                      "released tool-result wrapper"),
            assert_v4_event,
            ev(0, 100, "user/message",
               {"id": "u1", "role": "user", "content": [self._wrapper()],
                "source": {"kind": "user"}}, surfaceOp="append"))

    def test_developer_message_wrapper_refused(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 developer/message at seq 0 content must not contain a "
                      "released tool-result wrapper"),
            assert_v4_event,
            ev(0, 100, "developer/message",
               {"turn": 1, "step": 1,
                "message": {"id": "d1", "role": "developer", "content": [self._wrapper()],
                            "source": {"kind": "runtime-context"}}},
               surfaceOp="append"))

    def test_compaction_summary_wrapper_refused(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 compaction/summary at seq 0 content must not contain a "
                      "released tool-result wrapper"),
            assert_v4_event,
            ev(0, 100, "compaction/summary",
               {"compactionId": "c", "summary": [self._wrapper()],
                "shadowedRange": {"start": 0, "end": 0}, "shadowedSeqs": [0],
                "shadowedTokenCount": 0, "provider": "p", "model": "m"}))

    def test_inbox_spliced_wrapper_refused(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 agent/inbox/spliced at seq 0 content must not contain a "
                      "released tool-result wrapper"),
            assert_v4_event,
            ev(0, 100, "agent/inbox/spliced",
               {"target": "next-turn", "start": 0,
                "inserted": [{"id": "u1", "role": "user", "content": [self._wrapper()],
                              "source": {"kind": "user"}}]}))

    def test_stream_block_end_wrapper_refused(self):
        data = v4_assistant_data(0)
        data["stream"] = [
            {"type": "chunk", "time": 110,
             "chunk": {"type": "block-end", "index": 0, "block": self._wrapper()}},
        ]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 assistant/message at seq 0 content must not contain a "
                      "released tool-result wrapper"),
            assert_v4_event, ev(0, 100, "assistant/message", data, surfaceOp="append"))

    def test_stream_block_start_wrapper_refused(self):
        data = v4_assistant_data(0)
        data["stream"] = [
            {"type": "chunk", "time": 110,
             "chunk": {"type": "block-start", "index": 0, "blockType": "tool-result"}},
        ]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 assistant/message at seq 0 content must not contain a "
                      "released tool-result wrapper"),
            assert_v4_event, ev(0, 100, "assistant/message", data, surfaceOp="append"))

    def test_ptc_dispatch_wrapper_refused(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 tool/ptc-dispatch at seq 0 content must not contain a "
                      "released tool-result wrapper"),
            v4_retired,
            ev(0, 100, "tool/ptc-dispatch", {"content": [self._wrapper()]}))

    def test_compaction_raw_output_wrapper_refused(self):
        data = {"compactionId": "c", "summary": [],
                "rawOutput": [self._wrapper()],
                "shadowedRange": {"start": 0, "end": 0}, "shadowedSeqs": [0],
                "shadowedTokenCount": 0, "provider": "p", "model": "m"}
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 compaction/summary at seq 0 content must not contain a "
                      "released tool-result wrapper"),
            v4_retired, ev(0, 100, "compaction/summary", data))

    def test_non_dict_stream_entries_tolerated(self):
        data = {"stream": [
            "not-a-dict",
            {"type": "chunk", "time": 1, "chunk": "not-a-dict"},
            {"type": "chunk", "time": 1, "chunk": {"type": "finish",
                                                   "reason": {"kind": "stop"}}},
        ]}
        v4_retired(ev(0, 100, "assistant/message", data))

    def test_non_dict_data_tolerated(self):
        v4_retired(ev(0, 100, "user/message", "not-a-dict"))


class V4MessageSourcesTest(unittest.TestCase):
    def test_empty_kind_refused(self):
        artifact = v4_throughput()
        artifact["events"][3]["data"]["source"] = {"kind": ""}
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 message requires a producer-owned source kind"),
            assert_released_v4_artifact, artifact)

    def test_plugin_literal_kind_refused(self):
        artifact = v4_throughput()
        artifact["events"][3]["data"]["source"] = {"kind": "plugin"}
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 message requires a producer-owned source kind"),
            assert_released_v4_artifact, artifact)


class V4DeveloperTest(unittest.TestCase):
    def _dev_artifact(self, dev_event, header_tools=None):
        tools = header_tools if header_tools is not None else [
            {"name": "read", "description": "d", "parameters": {"type": "object"}}]
        return v4_artifact([
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "request/header",
               {"header": {"config": {"provider": "fake", "model": "fake"},
                           "tools": tools}, "reason": "initial"}),
            dev_event,
            ev(4, 104, "step/end", {"turn": 1, "step": 1}),
            ev(5, 105, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])

    def _dev_event(self, content, header_seq=2, seq=3):
        data = {"turn": 1, "step": 1,
                "message": {"id": "dev1", "role": "developer", "content": content,
                            "source": {"kind": "runtime-context"}}}
        if header_seq is not None:
            data["headerSeq"] = header_seq
        return ev(seq, 103, "developer/message", data, surfaceOp="append")

    def test_valid_developer_message(self):
        event = self._dev_event([{"type": "tool-addition", "toolName": "read"}])
        assert_released_v4_artifact(self._dev_artifact(event))

    def test_valid_developer_without_header(self):
        event = self._dev_event([], header_seq=None)
        assert_released_v4_artifact(self._dev_artifact(event))

    def test_valid_developer_with_text_and_addition(self):
        event = self._dev_event([text_block("note"),
                                 {"type": "tool-addition", "toolName": "read"}])
        assert_released_v4_artifact(self._dev_artifact(event))

    def test_requires_developer_role(self):
        event = self._dev_event([])
        event["data"]["message"]["role"] = "user"
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("developer/message data requires a developer message"),
            assert_v4_event, event)

    def test_requires_message_fields(self):
        event = self._dev_event([])
        event["data"]["message"]["id"] = ""
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 developer message requires id, role, content, and a "
                      "producer-owned source"),
            assert_v4_event, event)

    def test_plugin_source_refused(self):
        event = self._dev_event([])
        event["data"]["message"]["source"] = {"kind": "plugin"}
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 developer message requires id, role, content, and a "
                      "producer-owned source"),
            assert_v4_event, event)

    def test_tool_addition_requires_tool_name(self):
        event = self._dev_event([{"type": "tool-addition", "toolName": ""}])
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 tool-addition requires a nonempty toolName"),
            assert_v4_event, event)

    def test_tool_removal_requires_tool_name(self):
        event = self._dev_event([{"type": "tool-removal", "toolName": ""}])
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 tool-removal requires a nonempty toolName"),
            assert_v4_event, event)

    def test_tool_addition_omits_inline_tool(self):
        event = self._dev_event([{"type": "tool-addition", "toolName": "read",
                                  "tool": {"name": "read"}}])
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 tool-addition must omit inline tool definitions"),
            assert_v4_event, event)

    def test_header_seq_required_with_addition(self):
        event = self._dev_event([{"type": "tool-addition", "toolName": "read"}],
                                header_seq=None)
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("developer/message headerSeq must be a non-negative safe integer"),
            assert_v4_event, event)

    def test_header_seq_omitted_without_addition(self):
        event = self._dev_event([], header_seq=2)
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 developer/message must omit headerSeq without tool additions"),
            assert_v4_event, event)

    def test_developer_data_missing_coordinate(self):
        event = self._dev_event([])
        del event["data"]["message"]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape('developer/message data lacks required member "message"'),
            assert_v4_event, event)

    def test_developer_zero_coordinate(self):
        event = self._dev_event([])
        event["data"]["step"] = 0
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("developer/message data step must be positive"),
            assert_v4_event, event)

    def test_header_binding_missing_header(self):
        event = self._dev_event([{"type": "tool-addition", "toolName": "read"}])
        artifact = self._dev_artifact(event)
        artifact["events"][2] = ev(2, 102, "user/message", v4_user_data(),
                                   surfaceOp="append")
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("developer/message headerSeq must reference an earlier request/header"),
            assert_released_v4_artifact, artifact)

    def test_header_binding_tool_name_ambiguous(self):
        event = self._dev_event([{"type": "tool-addition", "toolName": "read"}])
        artifact = self._dev_artifact(event, header_tools=[
            {"name": "read", "description": "a", "parameters": {}},
            {"name": "read", "description": "b", "parameters": {}}])
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape('developer/message tool-addition "read" must name exactly one tool '
                      "in headerSeq 2"),
            assert_released_v4_artifact, artifact)

    def test_header_binding_requires_complete_definition(self):
        event = self._dev_event([{"type": "tool-addition", "toolName": "read"}])
        artifact = self._dev_artifact(event, header_tools=[
            {"name": "read", "description": "d"}])
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape('developer/message tool-addition "read" requires a complete tool '
                      "definition in headerSeq 2"),
            assert_released_v4_artifact, artifact)

    def test_header_seq_too_large(self):
        event = self._dev_event([{"type": "tool-addition", "toolName": "read"}],
                                header_seq=99)
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("developer/message headerSeq must reference an earlier request/header"),
            assert_released_v4_artifact, self._dev_artifact(event))


class V4ForkResultTest(unittest.TestCase):
    def _fork_artifact(self, ident=None, seq=3, surface_op="append",
                       source_event_seqs=None, error=None, text=INTERRUPTED_TEXT):
        ident = ident or f"forked-tool-result-call-1-{seq}"
        data = {"turn": 1, "step": 1,
                "error": error if error is not None
                else {"name": "ToolNotStartedError", "code": "TOOL_NOT_STARTED"},
                "message": {"id": ident, "role": "tool", "toolCallId": "call-1",
                            "content": [text_block(text)],
                            "source": {"kind": "tool", "callId": "call-1"},
                            "isError": True}}
        extra = {}
        if source_event_seqs is not None:
            extra["sourceEventSeqs"] = source_event_seqs
        return v4_artifact([
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "assistant/message",
               v4_assistant_data(2, content=[tool_call_block("call-1", "read", "{}")],
                                 stream=tool_stream(100)), surfaceOp="append"),
            ev(seq, 103, "tool/result", data, surfaceOp=surface_op, **extra),
            ev(4, 104, "step/end", {"turn": 1, "step": 1}),
            ev(5, 105, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])

    def test_valid_fork_result(self):
        assert_released_v4_artifact(self._fork_artifact())

    def test_valid_fork_result_replacement(self):
        sequence = 2
        ident = f"forked-tool-result-call-1-{sequence}"
        artifact = v4_artifact([
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "user/message", v4_user_data(), surfaceOp="append"),
            ev(3, 103, "tool/result",
               {"turn": 1, "step": 1,
                "error": {"name": "ToolNotStartedError", "code": "TOOL_NOT_STARTED"},
                "message": {"id": ident, "role": "tool", "toolCallId": "call-1",
                            "content": [text_block(INTERRUPTED_TEXT)],
                            "source": {"kind": "tool", "callId": "call-1"},
                            "isError": True}},
               surfaceOp={"op": "replace", "startSeq": 2, "endSeq": 2},
               sourceEventSeqs=[2]),
            ev(4, 104, "step/end", {"turn": 1, "step": 1}),
            ev(5, 105, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        assert_released_v4_artifact(artifact)

    def test_invalid_fork_identity(self):
        artifact = self._fork_artifact(ident="forked-tool-result-other-3")
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("invalid V4 not-started fork result"),
            assert_released_v4_artifact, artifact)

    def test_invalid_fork_sequence(self):
        # append 情形下 suffix 必须等于事件 seq
        artifact = self._fork_artifact(ident="forked-tool-result-call-1-9")
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("invalid V4 not-started fork result"),
            assert_released_v4_artifact, artifact)

    def test_invalid_fork_error_name(self):
        artifact = self._fork_artifact(error={"name": "OtherError",
                                              "code": "TOOL_NOT_STARTED"})
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("invalid V4 not-started fork result"),
            assert_released_v4_artifact, artifact)


class V4CanonicalPayloadTest(unittest.TestCase):
    def test_request_header_empty_tools(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 request/header at seq 0 empty optional header fields "
                      "must be omitted"),
            assert_v4_event,
            ev(0, 100, "request/header",
               {"header": {"config": {"provider": "fake", "model": "fake"}, "tools": []},
                "reason": "initial"}))

    def test_request_header_rejects_system(self):
        self.assertRaisesRegex(
            SessionFormatUnsupportedMigrationError,
            re.escape("format v4 request/header rejects retired header.system"),
            assert_v4_event,
            ev(0, 100, "request/header",
               {"header": {"config": {"provider": "fake", "model": "fake"},
                           "system": "x"}, "reason": "initial"}))

    def test_tool_result_error_metadata_mismatch(self):
        data = v4_tool_result_data()
        data["error"] = {"name": "ToolError", "code": "TOOL_ERROR"}
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 tool/result at seq 0 carries error metadata for a "
                      "non-error tool result"),
            assert_v4_event, ev(0, 100, "tool/result", data, surfaceOp="append"))


class V4PayloadStreamTest(unittest.TestCase):
    def _with_assistant(self, data):
        artifact = v4_throughput()
        artifact["events"][4] = ev(4, 104, "assistant/message", data, surfaceOp="append")
        return artifact

    def test_content_disagreement(self):
        artifact = self._with_assistant(
            v4_assistant_data(4, content=[text_block("different")]))
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/message 4 message content disagrees with its embedded stream"),
            assert_released_v4_artifact, artifact)

    def test_invalid_stream(self):
        artifact = self._with_assistant(
            v4_assistant_data(4, content=[text_block("x")],
                              stream=[{"type": "chunk", "time": 110,
                                       "chunk": {"type": "bogus"}}]))
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/message 4 has an invalid embedded stream"),
            assert_released_v4_artifact, artifact)

    def test_assistant_attempt_valid(self):
        artifact = v4_artifact([
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 110, "assistant/attempt",
               {"turn": 1, "step": 1, "stream": text_stream(102)}),
            ev(3, 120, "step/end", {"turn": 1, "step": 1}),
            ev(4, 130, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        assert_released_v4_artifact(artifact)

    def test_workspace_changes_valid(self):
        artifact = v4_throughput()
        artifact["events"] = [*artifact["events"][:8],
                              ev(8, 108, "workspace/changes", {"turn": 1}),
                              ev(9, 109, "turn/end", {"turn": 1,
                                                      "reason": {"kind": "completed"}})]
        artifact["events"][7] = ev(7, 107, "step/end", {"turn": 1, "step": 1})
        assert_released_v4_artifact(artifact)


class V4MarkerCutTest(unittest.TestCase):
    def test_cut_exceeds_events(self):
        artifact = v4_throughput()
        artifact["inherited_event_count"] = 99
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 inherited event count exceeds its events"),
            assert_released_v4_artifact, artifact)

    def test_unseeded_with_inherited_events(self):
        artifact = v4_throughput()
        artifact["inherited_event_count"] = 1
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("unseeded format v4 Session has inherited events"),
            assert_released_v4_artifact, artifact)

    def test_seeded_marker_disagreement(self):
        events = v4_throughput()["events"]
        events = [events[0], ev(1, 101, "session/end-seed", {"inherited": True}),
                  *[{**e, "seq": e["seq"] + 1} for e in events[1:]]]
        artifact = v4_artifact(events, inherited=0, isSeeded=True)
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 seeded header disagrees with its last inherited "
                      "end-seed marker"),
            assert_released_v4_artifact, artifact)

    def test_unseeded_marker_refused(self):
        events = [*v4_throughput()["events"],
                  ev(9, 140, "session/end-seed", {"inherited": True})]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v4 unseeded Session contains an inherited end-seed marker"),
            assert_released_v4_artifact, v4_artifact(events))

    def test_end_seed_inherited_must_be_true(self):
        events = v4_throughput()["events"]
        events = [events[0], ev(1, 101, "session/end-seed", {"inherited": False}),
                  *[{**e, "seq": e["seq"] + 1} for e in events[1:]]]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("session/end-seed 1 inherited must be true when present"),
            assert_released_v4_artifact, v4_artifact(events))


if __name__ == "__main__":
    unittest.main()
