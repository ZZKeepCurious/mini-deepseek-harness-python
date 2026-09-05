"""released v2 深度校验测试（Phase B，validate_v2.py + 共享 payload/关系/acodec）。

覆盖上游 `session-format-v1-to-v2/src/validation.ts` 与共享
`session-format-v0-to-v1/src/validation.ts` 的关键行为钉：
  * v2 header 精确逻辑头（field 无引号方言、missing-first、版本/id/isSeeded/cwd/
    parentSession/origin 逐条措辞）；
  * artifact 坐标（事件信封闭集、dense seq、time、ignorable、未知类型 target 拒读）；
  * marker/cut 双向一致性（seeded↔marker、unseeded↔marker、cut 越界）；
  * assistant/message 内嵌流三事实复核（content/usage/replayState 分歧）+ invalid
    embedded stream 收口 + assistant/attempt 可还原性与 step 门（v2 关系扩展）；
  * surface 元数据（obsolete chunk provenance 拒绝、空 sourceEventSeqs 拒绝）；
  * physical 词表中立与 current/restore 安装门（ignorable 未知放行、envelope-only）。

文档字符串措辞对齐 helpers.py 的 exact_keys 双方言说明（v2：``field`` / ``missing_first``）。
"""
import re
import unittest

from miniharness.core.session.released import (
    RELEASED_V2_EVENT_TYPES,
    SessionFormatError,
    SessionFormatUnsupportedMigrationError,
    assert_released_v2_artifact,
    assert_released_v2_header,
    assert_released_v2_physical_artifact,
    restore_released_v2_artifact,
)

DEFAULT_HEADER = {
    "version": 2,
    "id": "s-v2",
    "createdAt": 1,
    "isSeeded": False,
    "delegationDepth": 0,
}


def ev(seq, time, etype, data, **extra):
    return {"type": etype, "seq": seq, "time": time, "data": data, **extra}


def text_block(text):
    return {"type": "text", "text": text}


def artifact_v2(events, inherited=0, is_seeded=False, **header_overrides):
    header = dict(DEFAULT_HEADER)
    header["isSeeded"] = is_seeded
    header.update(header_overrides)
    return {"header": header, "inherited_event_count": inherited, "events": events}


def text_stream(time0, text="hello", chunk_records=(), reason=None):
    """一条可被 expand_assistant_stream 还原的 text-chunks + finish 压缩流。

    chunk_records 已含 finish 时不再追加（finish 必须唯一，BlockAssembler 只认最后一条）。
    """
    records = [{"type": "text-chunks", "time0": time0, "index": 0, "dt": [], "texts": [text]}]
    records.extend(chunk_records)
    has_finish = any(isinstance(r, dict) and r.get("type") == "chunk"
                     and isinstance(r.get("chunk"), dict)
                     and r["chunk"].get("type") == "finish"
                     for r in chunk_records)
    if not has_finish:
        records.append({
            "type": "chunk",
            "time": time0 + 10,
            "chunk": {"type": "finish", "reason": reason or {"kind": "stop"}},
        })
    return records


def usage_record(time, usage):
    return {"type": "chunk", "time": time,
            "chunk": {"type": "usage", "usage": usage}}


def assistant_message(seq, time, text="hello", *, stream=None, usage=None,
                      interrupted=False, content=None, source=None, replay_state=None):
    data = {
        "turn": 1,
        "step": 1,
        "message": {
            "id": f"am{seq}",
            "role": "assistant",
            "content": content if content is not None else [text_block(text)],
            "source": source if source is not None
            else {"kind": "model", "provider": "fake", "model": "fake"},
        },
        "stream": stream if stream is not None else text_stream(time - 10),
    }
    if usage is not None:
        data["usage"] = usage if isinstance(usage, dict) else {
            "inputTokens": usage[0], "outputTokens": usage[1]}
    if interrupted:
        data["interrupted"] = True
    if replay_state is not None:
        data["message"]["source"] = {**data["message"]["source"],
                                     "replayState": replay_state}
    return ev(seq, time, "assistant/message", data, surfaceOp="append")


def user_message(seq, time, text="hi", **extra):
    return ev(seq, time, "user/message",
              {"id": f"m{seq}", "role": "user",
               "content": [text_block(text)], "source": {"kind": "user"}},
              surfaceOp="append", **extra)


def v2_throughput():
    return artifact_v2([
        user_message(0, 100),
        ev(1, 101, "turn/start", {"turn": 1}),
        ev(2, 102, "step/start", {"turn": 1, "step": 1}),
        assistant_message(3, 112),
        ev(4, 122, "step/end", {"turn": 1, "step": 1}),
        ev(5, 130, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
    ])


class V2HeaderTest(unittest.TestCase):
    def test_valid_header_passes(self):
        assert_released_v2_header(dict(DEFAULT_HEADER))

    def test_non_object(self):
        self.assertRaisesRegex(SessionFormatError, re.escape("format v2 header must be an object"),
                               assert_released_v2_header, ["not", "an", "object"])
        self.assertRaisesRegex(SessionFormatError, re.escape("format v2 header must be an object"),
                               assert_released_v2_header, None)

    def test_missing_required_field(self):
        header = dict(DEFAULT_HEADER)
        del header["id"]
        # field 方言 + missing-first：缺 id 而是缺第一个必填 version 的镜像检查
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v2 header lacks required field id"),
            assert_released_v2_header, header)

    def test_unexpected_field(self):
        header = dict(DEFAULT_HEADER)
        header["flavor"] = "strawberry"
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v2 header has unexpected field flavor"),
            assert_released_v2_header, header)

    def test_wrong_version(self):
        header = dict(DEFAULT_HEADER)
        header["version"] = 1
        self.assertRaisesRegex(SessionFormatError, re.escape("expected format v2 header"),
                               assert_released_v2_header, header)
        header["version"] = True
        self.assertRaisesRegex(SessionFormatError, re.escape("expected format v2 header"),
                               assert_released_v2_header, header)

    def test_id_type(self):
        header = dict(DEFAULT_HEADER)
        header["id"] = 42
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v2 header id must be a string"),
                               assert_released_v2_header, header)

    def test_created_at_count(self):
        header = dict(DEFAULT_HEADER)
        header["createdAt"] = -1
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v2 header createdAt must be a non-negative safe integer"),
            assert_released_v2_header, header)

    def test_is_seeded_type(self):
        header = dict(DEFAULT_HEADER)
        header["isSeeded"] = 1
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v2 header isSeeded must be boolean"),
                               assert_released_v2_header, header)

    def test_cwd_relative(self):
        header = dict(DEFAULT_HEADER)
        header["cwd"] = "relative/path"
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v2 header cwd must be absolute"),
                               assert_released_v2_header, header)

    def test_cwd_null(self):
        header = dict(DEFAULT_HEADER)
        header["cwd"] = None
        # cwd 在场即校验：Null 非字符串也非绝对路径，同一候选消息
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v2 header cwd must be absolute"),
                               assert_released_v2_header, header)

    def test_cwd_absolute_pass(self):
        header = dict(DEFAULT_HEADER)
        header["cwd"] = "/absolute/path"
        assert_released_v2_header(header)

    def test_parent_session_null(self):
        header = dict(DEFAULT_HEADER)
        header["parentSession"] = None
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v2 header parentSession must be a string"),
                               assert_released_v2_header, header)

    def test_agent_preset_null(self):
        header = dict(DEFAULT_HEADER)
        header["agentPreset"] = None
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v2 header agentPreset must be a string"),
                               assert_released_v2_header, header)

    def test_origin(self):
        header = dict(DEFAULT_HEADER)
        header["origin"] = "parent"
        self.assertRaisesRegex(
            SessionFormatError, re.escape('format v2 header origin must be "subagent"'),
            assert_released_v2_header, header)


class V2ArtifactEnvelopeTest(unittest.TestCase):
    def test_valid_artifact_passes(self):
        assert_released_v2_artifact(v2_throughput())

    def test_event_non_object(self):
        artifact = v2_throughput()
        artifact["events"][0] = "not an event"
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v2 event 0 must be an object"),
                               assert_released_v2_artifact, artifact)

    def test_missing_event_field(self):
        artifact = v2_throughput()
        del artifact["events"][0]["data"]
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v2 event 0 lacks required field data"),
                               assert_released_v2_artifact, artifact)

    def test_unexpected_event_field(self):
        artifact = v2_throughput()
        artifact["events"][0]["extra"] = "spurious"
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v2 event 0 has unexpected field extra"),
                               assert_released_v2_artifact, artifact)

    def test_non_dense_seq(self):
        artifact = v2_throughput()
        artifact["events"][0]["seq"] = 1
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v2 event 0 is not dense"),
                               assert_released_v2_artifact, artifact)

    def test_time_not_safe_integer(self):
        artifact = v2_throughput()
        artifact["events"][0]["time"] = 2**53
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v2 event 0 time must be a safe integer"),
            assert_released_v2_artifact, artifact)

    def test_ignorable_must_be_true(self):
        artifact = v2_throughput()
        artifact["events"][0]["ignorable"] = 1
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v2 event 0 ignorable must be true when present"),
            assert_released_v2_artifact, artifact)

    def test_unknown_type_refused(self):
        artifact = artifact_v2([
            user_message(0, 100),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            ev(3, 110, "future/event-type", {"anything": 1}),
            ev(4, 120, "step/end", {"turn": 1, "step": 1}),
            ev(5, 130, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        self.assertRaisesRegex(
            SessionFormatUnsupportedMigrationError,
            re.escape('format v2 contains unknown event type "future/event-type" at seq 3'),
            assert_released_v2_artifact, artifact)

    def test_ignorable_unknown_still_refused_target(self):
        artifact = artifact_v2([
            user_message(0, 100),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            ev(3, 110, "future/event-type", {"anything": 1}, ignorable=True),
            ev(4, 120, "step/end", {"turn": 1, "step": 1}),
            ev(5, 130, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        # target 模式对未知事件即使 ignorable 也拒读：未来事件不再受 target 约定约束
        self.assertRaisesRegex(
            SessionFormatUnsupportedMigrationError,
            re.escape('format v2 contains unknown event type "future/event-type" at seq 3'),
            assert_released_v2_artifact, artifact)


class V2MarkerCutTest(unittest.TestCase):
    def test_cut_exceeds_events(self):
        artifact = v2_throughput()
        artifact["inherited_event_count"] = 99
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v2 inherited event count exceeds its events"),
            assert_released_v2_artifact, artifact)

    def test_unseeded_with_inherited_events(self):
        artifact = v2_throughput()
        artifact["inherited_event_count"] = 1
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("unseeded format v2 Session has inherited events"),
            assert_released_v2_artifact, artifact)

    def _seeded_events(self, marker_index):
        events = [
            user_message(0, 100),
            ev(1, 101, "session/end-seed", {"inherited": True}),
            ev(2, 102, "turn/start", {"turn": 1}),
            ev(3, 103, "step/start", {"turn": 1, "step": 1}),
            assistant_message(4, 113),
            ev(5, 123, "step/end", {"turn": 1, "step": 1}),
            ev(6, 130, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        if marker_index != 1:
            marker = events.pop(1)
            marker["seq"] = marker_index
            events.insert(marker_index, marker)
            for i in range(len(events)):
                events[i]["seq"] = i
        return events

    def test_seeded_header_marker_agreement(self):
        artifact = artifact_v2(self._seeded_events(1), inherited=1, is_seeded=True)
        assert_released_v2_artifact(artifact)

    def test_seeded_header_marker_disagreement(self):
        artifact = artifact_v2(self._seeded_events(4), inherited=1, is_seeded=True)
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v2 seeded header disagrees with its last inherited end-seed marker"),
            assert_released_v2_artifact, artifact)

    def test_unseeded_marker_refused(self):
        events = v2_throughput()["events"]
        events = [*events, ev(6, 140, "session/end-seed", {"inherited": True})]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("format v2 unseeded Session contains an inherited end-seed marker"),
            assert_released_v2_artifact, artifact_v2(events))

    def test_end_seed_inherited_must_be_true(self):
        artifact = artifact_v2([
            user_message(0, 100),
            ev(1, 101, "session/end-seed", {"inherited": False}),
            ev(2, 102, "turn/start", {"turn": 1}),
            ev(3, 103, "step/start", {"turn": 1, "step": 1}),
            assistant_message(4, 113),
            ev(5, 123, "step/end", {"turn": 1, "step": 1}),
            ev(6, 130, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("session/end-seed 1 inherited must be true when present"),
            assert_released_v2_artifact, artifact)


class V2SurfaceMetadataTest(unittest.TestCase):
    def test_assistant_message_retains_obsolete_provenance(self):
        events = v2_throughput()["events"]
        events[3]["sourceEventSeqs"] = [1, 2]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/message 3 retains obsolete chunk provenance"),
            assert_released_v2_artifact, artifact_v2(events))

    def test_user_message_empty_sources_refused(self):
        events = v2_throughput()["events"]
        events[0]["sourceEventSeqs"] = []
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("user/message 0 sourceEventSeqs must be non-empty"),
            assert_released_v2_artifact, artifact_v2(events))


class V2StreamCrossCheckTest(unittest.TestCase):
    def test_content_disagreement(self):
        events = v2_throughput()["events"]
        events[3] = assistant_message(3, 112, content=[text_block("different")])
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/message 3 message content disagrees with its embedded stream"),
            assert_released_v2_artifact, artifact_v2(events))

    def test_interrupted_content_uses_interrupted_blocks(self):
        events = v2_throughput()["events"]
        events[3] = assistant_message(3, 112, interrupted=True)
        assert_released_v2_artifact(artifact_v2(events))

    def test_usage_disagreement(self):
        events = v2_throughput()["events"]
        events[3] = assistant_message(
            3, 112,
            stream=text_stream(102, chunk_records=(usage_record(110, {"inputTokens": 10,
                                                                     "outputTokens": 5}),)),
            usage=(99, 99))
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/message 3 usage disagrees with its embedded stream"),
            assert_released_v2_artifact, artifact_v2(events))

    def test_usage_omitted_but_stream_carries_usage(self):
        events = v2_throughput()["events"]
        events[3] = assistant_message(
            3, 112,
            stream=text_stream(102, chunk_records=(usage_record(110, {"inputTokens": 10,
                                                                     "outputTokens": 5}),)))
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/message 3 usage disagrees with its embedded stream"),
            assert_released_v2_artifact, artifact_v2(events))

    def test_replay_state_disagreement(self):
        envelope = {"response": {"r": "a"}, "blocks": [{"k": "v"}]}
        events = v2_throughput()["events"]
        events[3] = assistant_message(
            3, 112,
            stream=text_stream(
                102, chunk_records=({
                    "type": "chunk", "time": 112,
                    "chunk": {"type": "finish", "reason": {"kind": "stop"},
                              "replayState": envelope},
                },)),
            replay_state={"response": {"r": "zzz"}, "blocks": []})
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/message 3 replay state disagrees with its embedded stream"),
            assert_released_v2_artifact, artifact_v2(events))

    def test_valid_usage_and_replay_pass(self):
        envelope = {"response": {"r": "a"}, "blocks": [{"k": "v"}]}
        stream = text_stream(
            102, chunk_records=(
                usage_record(110, {"inputTokens": 10, "outputTokens": 5}),
                {"type": "chunk", "time": 112,
                 "chunk": {"type": "finish", "reason": {"kind": "stop"},
                           "replayState": envelope}},
            ))
        events = v2_throughput()["events"]
        events[3] = assistant_message(3, 112, stream=stream,
                                      usage=(10, 5), replay_state=envelope)
        assert_released_v2_artifact(artifact_v2(events))

    def test_invalid_embedded_stream(self):
        events = v2_throughput()["events"]
        bad = [{"type": "text-chunks", "time0": 102, "index": 0,
                "dt": [1, 1], "texts": ["a", "b"]}]
        events[3] = assistant_message(3, 112, stream=bad)
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/message 3 has an invalid embedded stream"),
            assert_released_v2_artifact, artifact_v2(events))

    def test_invalid_chunk_kind_in_stream(self):
        events = v2_throughput()["events"]
        events[3] = assistant_message(3, 112, stream=[
            {"type": "chunk", "time": 110,
             "chunk": {"type": "bogus-unknown-chunk-type"}},
        ])
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/message 3 has an invalid embedded stream"),
            assert_released_v2_artifact, artifact_v2(events))


class V2AttemptTest(unittest.TestCase):
    def _attempt_artifact(self):
        return artifact_v2([
            user_message(0, 100),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            ev(3, 112, "assistant/attempt",
               {"turn": 1, "step": 1, "stream": text_stream(102)}),
            ev(4, 122, "step/end", {"turn": 1, "step": 1}),
            ev(5, 130, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])

    def test_valid_attempt_inside_step(self):
        assert_released_v2_artifact(self._attempt_artifact())

    def test_attempt_outside_open_step(self):
        artifact = artifact_v2([
            user_message(0, 100),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 112, "assistant/attempt",
               {"turn": 1, "step": 1, "stream": text_stream(102)}),
            ev(3, 130, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        # v2 关系扩展：assistant/attempt 是 step 生命周期门事件
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/attempt does not match an open turn and step"),
            assert_released_v2_artifact, artifact)

    def test_attempt_invalid_stream(self):
        artifact = self._attempt_artifact()
        artifact["events"][3]["data"]["stream"] = [
            {"type": "chunk", "time": 110,
             "chunk": {"type": "bogus-unknown-chunk-type"}},
        ]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("assistant/attempt 3 has an invalid embedded stream"),
            assert_released_v2_artifact, artifact)


class V2PhysicalModeTest(unittest.TestCase):
    def test_unknown_type_passes_physical(self):
        artifact = artifact_v2([
            user_message(0, 100),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            ev(3, 110, "future/event-type", {"anything": 1}),
            ev(4, 120, "step/end", {"turn": 1, "step": 1}),
            ev(5, 130, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        assert_released_v2_physical_artifact(artifact)

    def test_invalid_embedded_stream_passes_physical(self):
        events = v2_throughput()["events"]
        events[3] = assistant_message(3, 112, stream=[
            {"type": "chunk", "time": 110, "chunk": {"type": "bogus"}},
        ])
        assert_released_v2_physical_artifact(artifact_v2(events))

    def test_obsolete_provenance_passes_physical(self):
        events = v2_throughput()["events"]
        events[3]["sourceEventSeqs"] = [1, 2]
        assert_released_v2_physical_artifact(artifact_v2(events))

    def test_physical_still_rejects_bad_coordinates(self):
        artifact = v2_throughput()
        artifact["events"][0]["seq"] = 1
        self.assertRaisesRegex(SessionFormatError,
                               re.escape("format v2 event 0 is not dense"),
                               assert_released_v2_physical_artifact, artifact)


class V2RestoreModeTest(unittest.TestCase):
    KNOWN = frozenset(RELEASED_V2_EVENT_TYPES)

    def test_returns_artifact_identity(self):
        artifact = v2_throughput()
        self.assertIs(restore_released_v2_artifact(artifact, self.KNOWN), artifact)

    def test_unknown_required_refused(self):
        # current 安装门：词表外、未安装、非 ignorable 的未来事件必须拒读
        artifact = artifact_v2([
            user_message(0, 100),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            ev(3, 110, "future/event-type", {"anything": 1}),
            ev(4, 120, "step/end", {"turn": 1, "step": 1}),
            ev(5, 130, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        self.assertRaisesRegex(
            SessionFormatUnsupportedMigrationError,
            re.escape('format v2 contains unknown event type "future/event-type" at seq 3'),
            restore_released_v2_artifact, artifact, self.KNOWN)

    def test_ignorable_unknown_accepted(self):
        artifact = artifact_v2([
            user_message(0, 100),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            ev(3, 110, "future/event-type", {"anything": 1}, ignorable=True),
            ev(4, 120, "step/end", {"turn": 1, "step": 1}),
            ev(5, 130, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        restored = restore_released_v2_artifact(artifact, self.KNOWN)
        self.assertIs(restored, artifact)

    def test_envelope_only_invalid_stream(self):
        # 恢复 = 信封级装载：内嵌流三事实复核不跑（target 才跑）
        events = v2_throughput()["events"]
        events[3] = assistant_message(3, 112, stream=[
            {"type": "chunk", "time": 110, "chunk": {"type": "bogus"}},
        ])
        restore_released_v2_artifact(artifact_v2(events), self.KNOWN)

    def test_envelope_only_relationship_violation(self):
        # 恢复 = 信封级装载：step 门关系状态机不跑（target 才跑）
        artifact = artifact_v2([
            user_message(0, 100),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 112, "assistant/attempt",
               {"turn": 1, "step": 1, "stream": text_stream(102)}),
            ev(3, 130, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        restore_released_v2_artifact(artifact, self.KNOWN)


if __name__ == "__main__":
    unittest.main()