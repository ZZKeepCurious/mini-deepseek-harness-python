"""released v0..v4 物理 codec 专项（codec.py 直测）。

覆盖 `RELEASED_V0_CODEC`..`RELEASED_V4_CODEC`、`create_released_codec`、
`decode_released_header`、`PackedRowError`、打包行/provenance 展开与逐条错误分支；
真实制品 + 逐字异常断言，无 mock。

`seedLength → isSeeded` 语义、`inherited_cut` 派生、可恢复扫描（issue/回滚）均直接
驱动公开 codec 面。
"""
import re
import unittest

from miniharness.core.session.released import (
    RELEASED_V0_CODEC,
    RELEASED_V1_CODEC,
    RELEASED_V2_CODEC,
    RELEASED_V3_CODEC,
    RELEASED_V4_CODEC,
    PackedRowError,
    SessionFormatError,
    create_released_codec,
    decode_released_header,
)
from miniharness.core.session.released.codec import (
    _decode_event_row,
    _decode_provenance,
    _expand_packed_row,
)


def ev(seq, time, etype, data, **extra):
    return {"type": etype, "seq": seq, "time": time, "data": data, **extra}


def v0_header(**overrides):
    header = {"type": "session", "version": 0, "id": "s", "createdAt": 1,
              "delegationDepth": 0}
    header.update(overrides)
    return header


def v1_header(**overrides):
    header = {"type": "session", "version": 1, "id": "s", "createdAt": 1,
              "delegationDepth": 0}
    header.update(overrides)
    return header


def v2_header(**overrides):
    header = {"type": "session", "version": 2, "id": "s", "createdAt": 1,
              "isSeeded": False, "delegationDepth": 0}
    header.update(overrides)
    return header


def v3_header(**overrides):
    header = {"type": "session", "version": 3, "id": "s", "createdAt": 1,
              "isSeeded": False, "delegationDepth": 0}
    header.update(overrides)
    return header


def v4_header(**overrides):
    header = {"type": "session", "version": 4, "id": "s", "createdAt": 1,
              "isSeeded": False, "delegationDepth": 0}
    header.update(overrides)
    return header


def simple_rows():
    return [
        ev(0, 1, "turn/start", {"turn": 1}),
        ev(1, 2, "step/start", {"turn": 1, "step": 1}),
        ev(2, 3, "step/end", {"turn": 1, "step": 1}),
        ev(3, 4, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
    ]


class DecodeReleasedHeaderTest(unittest.TestCase):
    def test_reads_version(self):
        self.assertEqual(decode_released_header({"version": 0}), 0)
        self.assertEqual(decode_released_header({"version": 3}), 3)

    def test_non_object(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("corrupt session log: header is not a JSON object"),
            decode_released_header, [1, 2])
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("corrupt session log: header is not a JSON object"),
            decode_released_header, None)

    def test_invalid_versions(self):
        for version in (None, -1, True, 1.5, "2"):
            self.assertRaisesRegex(
                SessionFormatError,
                re.escape("session log header version must be a non-negative safe integer"),
                decode_released_header, {"version": version})


class V0CodecHeaderTest(unittest.TestCase):
    def test_decode_header_round_trip(self):
        header = RELEASED_V0_CODEC["decode_header"](v0_header(cwd="/w"))
        self.assertEqual(header, {"version": 0, "id": "s", "createdAt": 1,
                                  "isSeeded": False, "delegationDepth": 0,
                                  "cwd": "/w"})

    def test_seed_length_semantics(self):
        header = RELEASED_V0_CODEC["decode_header"](v0_header(seedLength=0))
        self.assertTrue(header["isSeeded"])
        header = RELEASED_V0_CODEC["decode_header"](v0_header(seedLength=4))
        self.assertTrue(header["isSeeded"])

    def test_optional_strings_and_origin(self):
        header = RELEASED_V0_CODEC["decode_header"](v0_header(
            cwd="/w", parentSession="p", agentPreset="a", origin="subagent"))
        self.assertEqual(header["parentSession"], "p")
        self.assertEqual(header["agentPreset"], "a")
        self.assertEqual(header["origin"], "subagent")

    def test_non_object(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("released v0 physical Session header must be a JSON object"),
            RELEASED_V0_CODEC["decode_header"], "nope")

    def test_unexpected_member(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape('released v0 physical Session header has unexpected member "x"'),
            RELEASED_V0_CODEC["decode_header"], v0_header(x=1))

    def test_missing_member(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape('released v0 physical Session header lacks required member "type"'),
            RELEASED_V0_CODEC["decode_header"], {"version": 0, "id": "s",
                                                 "createdAt": 1, "delegationDepth": 0})

    def test_wrong_type_and_version(self):
        for header in (v0_header(type="other"), v0_header(version=1), v0_header(version=True)):
            self.assertRaisesRegex(
                SessionFormatError,
                re.escape("expected released v0 physical Session header"),
                RELEASED_V0_CODEC["decode_header"], header)

    def test_bad_scalars(self):
        cases = (
            (v0_header(id=5),
             "released v0 physical Session header id must be a string"),
            (v0_header(createdAt=-1),
             "released v0 physical Session header createdAt must be a non-negative safe integer"),
            (v0_header(delegationDepth=-1),
             "released v0 physical Session header delegationDepth must be a non-negative safe integer"),
            (v0_header(cwd=7),
             "released v0 physical Session header cwd must be a string"),
            (v0_header(parentSession=7),
             "released v0 physical Session header parentSession must be a string"),
            (v0_header(agentPreset=7),
             "released v0 physical Session header agentPreset must be a string"),
            (v0_header(origin="parent"),
             'released v0 physical Session header origin must be "subagent"'),
            (v0_header(cwd="relative"),
             "format v0 header cwd must be absolute"),
        )
        for header, message in cases:
            self.assertRaisesRegex(SessionFormatError, re.escape(message),
                                   RELEASED_V0_CODEC["decode_header"], header)


class V1CodecTest(unittest.TestCase):
    def test_decode_header_seed_semantics(self):
        self.assertTrue(RELEASED_V1_CODEC["decode_header"](
            v1_header(seedLength=0))["isSeeded"])
        self.assertFalse(RELEASED_V1_CODEC["decode_header"](
            v1_header())["isSeeded"])

    def test_decode_artifact_inherited_count(self):
        artifact = RELEASED_V1_CODEC["decode_artifact"](v1_header(seedLength=2),
                                                        simple_rows())
        self.assertEqual(artifact["inherited_event_count"], 2)
        self.assertTrue(artifact["header"]["isSeeded"])
        self.assertEqual([event["type"] for event in artifact["events"]],
                         ["turn/start", "step/start", "step/end", "turn/end"])

    def test_create_released_codec_defaults(self):
        v0 = create_released_codec(0)
        self.assertEqual(v0["version"], 0)
        v1 = create_released_codec(1)
        self.assertEqual(v1["version"], 1)
        artifact = v1["decode_artifact"](v1_header(), simple_rows())
        self.assertEqual(len(artifact["events"]), 4)

    def test_create_released_codec_explicit_validator(self):
        seen = []

        def validator(artifact):
            seen.append(artifact)

        codec = create_released_codec(1, validator)
        artifact = codec["decode_artifact"](v1_header(), simple_rows())
        self.assertEqual(seen, [artifact])


class V2V3V4CodecTest(unittest.TestCase):
    def test_v2_round_trip(self):
        artifact = RELEASED_V2_CODEC["decode_artifact"](v2_header(), simple_rows())
        self.assertEqual(artifact["inherited_event_count"], 0)
        self.assertFalse(artifact["header"]["isSeeded"])
        self.assertEqual(len(artifact["events"]), 4)

    def test_v3_round_trip(self):
        artifact = RELEASED_V3_CODEC["decode_artifact"](v3_header(), simple_rows())
        self.assertEqual(artifact["header"]["version"], 3)
        self.assertEqual(len(artifact["events"]), 4)

    def test_v4_round_trip(self):
        artifact = RELEASED_V4_CODEC["decode_artifact"](v4_header(), simple_rows())
        self.assertEqual(artifact["header"]["version"], 4)
        self.assertEqual(len(artifact["events"]), 4)

    def test_decode_header_is_seeded(self):
        self.assertTrue(RELEASED_V4_CODEC["decode_header"](v4_header(isSeeded=True))["isSeeded"])
        self.assertFalse(RELEASED_V4_CODEC["decode_header"](v4_header(isSeeded=False))["isSeeded"])

    def test_decode_header_missing_is_seeded(self):
        header = v2_header()
        del header["isSeeded"]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape('released v2 physical Session header lacks required member "isSeeded"'),
            RELEASED_V2_CODEC["decode_header"], header)

    def test_decode_header_bad_is_seeded(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("released v2 physical Session header isSeeded must be boolean"),
            RELEASED_V2_CODEC["decode_header"], v2_header(isSeeded=1))

    def test_decode_header_bad_origin(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape('released v2 physical Session header origin must be "subagent"'),
            RELEASED_V2_CODEC["decode_header"], v2_header(origin="parent"))

    def test_decode_header_wrong_type_version_id(self):
        cases = (
            (v2_header(type="other"), "expected released v2 physical Session header"),
            (v2_header(version=3), "expected released v2 physical Session header"),
            (v2_header(version=True), "expected released v2 physical Session header"),
            (v2_header(id=5), "released v2 physical Session header id must be a string"),
        )
        for header, message in cases:
            with self.subTest(message=message):
                self.assertRaisesRegex(SessionFormatError, re.escape(message),
                                       RELEASED_V2_CODEC["decode_header"], header)

    def test_decode_header_optional_string(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("released v2 physical Session header cwd must be a string"),
            RELEASED_V2_CODEC["decode_header"], v2_header(cwd=7))

    def test_v2_recoverable(self):
        rows = [ev(0, 1, "turn/start", {"turn": 1}),
                ev(2, 3, "turn/end", {"turn": 1, "reason": {"kind": "completed"}})]
        artifact = RELEASED_V2_CODEC["decode_recoverable_artifact"](v2_header(), rows)
        self.assertEqual([event["seq"] for event in artifact["events"]], [0])

    def test_seeded_marker_derives_cut(self):
        rows = [
            ev(0, 1, "turn/start", {"turn": 1}),
            ev(1, 2, "session/end-seed", {"inherited": True}),
            ev(2, 3, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        artifact = RELEASED_V2_CODEC["decode_artifact"](v2_header(isSeeded=True), rows)
        self.assertEqual(artifact["inherited_event_count"], 1)

    def test_unseeded_marker_refused(self):
        rows = [
            ev(0, 1, "turn/start", {"turn": 1}),
            ev(1, 2, "session/end-seed", {"inherited": True}),
            ev(2, 3, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        with self.assertRaises(ValueError) as ctx:
            RELEASED_V2_CODEC["decode_artifact"](v2_header(isSeeded=False), rows)
        self.assertIn("unseeded v2 header contains an inherited end-seed marker",
                      str(ctx.exception))

    def test_seeded_header_without_marker_refused(self):
        with self.assertRaises(ValueError) as ctx:
            RELEASED_V2_CODEC["decode_artifact"](v2_header(isSeeded=True), simple_rows())
        self.assertIn("seeded v2 header lacks an inherited end-seed marker",
                      str(ctx.exception))


class PackedRowTest(unittest.TestCase):
    def test_text_chunks_expand(self):
        events = _expand_packed_row(
            {"type": "text-chunks", "seq0": 0, "time0": 10,
             "data": {"turn": 1, "step": 1, "index": 0, "dt": [5], "texts": ["a", "b"]}},
            0)
        self.assertEqual(events, [
            {"type": "assistant/chunk", "seq": 0, "time": 10,
             "data": {"turn": 1, "step": 1,
                      "chunk": {"type": "text-delta", "index": 0, "text": "a"}}},
            {"type": "assistant/chunk", "seq": 1, "time": 15,
             "data": {"turn": 1, "step": 1,
                      "chunk": {"type": "text-delta", "index": 0, "text": "b"}}},
        ])

    def test_reasoning_chunks_expand(self):
        events = _expand_packed_row(
            {"type": "reasoning-chunks", "seq0": 0, "time0": 10,
             "data": {"turn": 1, "step": 1, "index": 1, "dt": [], "texts": ["r"]}},
            0)
        self.assertEqual(events[0]["data"]["chunk"],
                         {"type": "reasoning-delta", "index": 1, "text": "r"})

    def test_tool_call_chunks_expand(self):
        events = _expand_packed_row(
            {"type": "tool-call-chunks", "seq0": 0, "time0": 10,
             "data": {"turn": 1, "step": 1, "index": 0, "id": "c1", "name": "read",
                      "dt": [2], "args": ["{}", "{}"]}},
            0)
        self.assertEqual(events[1]["data"]["chunk"],
                         {"type": "tool-call-delta", "index": 0, "id": "c1",
                          "argumentsDelta": "{}", "name": "read"})
        self.assertEqual(events[1]["time"], 12)

    def test_tool_call_chunks_without_name(self):
        events = _expand_packed_row(
            {"type": "tool-call-chunks", "seq0": 0, "time0": 10,
             "data": {"turn": 1, "step": 1, "index": 0, "id": "c1",
                      "dt": [], "args": ["{}"]}},
            0)
        self.assertNotIn("name", events[0]["data"]["chunk"])

    def test_packed_row_via_codec(self):
        row = {"type": "text-chunks", "seq0": 0, "time0": 10,
               "data": {"turn": 1, "step": 1, "index": 0, "dt": [5],
                        "texts": ["a", "b"]}}
        artifact = RELEASED_V1_CODEC["decode_artifact"](v1_header(), [row])
        self.assertEqual([event["seq"] for event in artifact["events"]], [0, 1])
        self.assertEqual(artifact["events"][1]["time"], 15)

    def test_packed_row_errors(self):
        base = {"type": "text-chunks", "seq0": 0, "time0": 10,
                "data": {"turn": 1, "step": 1, "index": 0, "dt": [], "texts": ["a"]}}
        cases = (
            ({**base, "seq0": -1},
             "released packed row 1 (text-chunks) seq0 must be a non-negative safe integer"),
            ({**base, "time0": 2 ** 53},
             "released packed row 1 (text-chunks) time0 must be a safe integer"),
            ({"type": "text-chunks", "seq0": 0, "time0": 10},
             'released packed row 1 (text-chunks) lacks required member "data"'),
            ({**base, "extra": 1},
             'released packed row 1 (text-chunks) has unexpected member "extra"'),
            ({**base, "data": {"turn": True, "step": 1, "index": 0, "dt": [],
                               "texts": ["a"]}},
             "released packed row 1 (text-chunks) turn must be a number"),
            ({**base, "data": {"turn": 1, "step": 1, "index": 0, "dt": [],
                               "texts": []}},
             "released packed row 1 (text-chunks) texts must be a non-empty array of strings"),
            ({**base, "data": {"turn": 1, "step": 1, "index": 0, "dt": [],
                               "texts": ["a", "b"]}},
             "released packed row 1 (text-chunks) dt must have one gap per payload gap"),
            ({**base, "data": {"turn": 1, "step": 1, "index": 0, "dt": [2 ** 53],
                               "texts": ["a", "b"]}},
             "released packed row 1 (text-chunks) dt[0] must be a safe integer"),
        )
        for row, message in cases:
            with self.subTest(message=message):
                self.assertRaisesRegex(
                    SessionFormatError, re.escape(message),
                    RELEASED_V1_CODEC["decode_artifact"], v1_header(), [row])

    def test_tool_call_packed_row_errors(self):
        base = {"type": "tool-call-chunks", "seq0": 0, "time0": 10,
                "data": {"turn": 1, "step": 1, "index": 0, "id": "c1",
                         "dt": [], "args": ["{}"]}}
        cases = (
            ({**base, "data": {**base["data"], "id": 7}},
             "released packed row 1 (tool-call-chunks) tool id must be a string"),
            ({**base, "data": {**base["data"], "name": 7}},
             "released packed row 1 (tool-call-chunks) tool name must be a string"),
        )
        for row, message in cases:
            with self.subTest(message=message):
                self.assertRaisesRegex(
                    SessionFormatError, re.escape(message),
                    RELEASED_V1_CODEC["decode_artifact"], v1_header(), [row])


class EventRowTest(unittest.TestCase):
    def test_decodes_members(self):
        event = _decode_event_row(
            {"type": "turn/start", "seq": 0, "time": 5, "data": {"turn": 1},
             "ignorable": True, "surfaceOp": "append"}, 0, 0)
        self.assertEqual(event, {"type": "turn/start", "seq": 0, "time": 5,
                                 "data": {"turn": 1}, "ignorable": True,
                                 "surfaceOp": "append"})

    def test_non_object(self):
        self.assertRaisesRegex(
            SessionFormatError, re.escape("released v0 row 1 must be a JSON object"),
            _decode_event_row, "nope", 0, 0)

    def test_unexpected_and_missing(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape('released v0 row 1 has unexpected member "extra"'),
            _decode_event_row,
            {"type": "turn/start", "seq": 0, "time": 5, "data": {}, "extra": 1}, 0, 0)
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape('released v0 row 1 lacks required member "data"'),
            _decode_event_row, {"type": "turn/start", "seq": 0, "time": 5}, 0, 0)

    def test_bad_seq_and_time(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("released v0 row 1 seq must be a non-negative safe integer"),
            _decode_event_row,
            {"type": "turn/start", "seq": -1, "time": 5, "data": {}}, 0, 0)
        self.assertRaisesRegex(
            SessionFormatError, re.escape("released v0 row 1 time must be a safe integer"),
            _decode_event_row,
            {"type": "turn/start", "seq": 0, "time": 2 ** 53, "data": {}}, 0, 0)

    def test_ignorable_must_be_true(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("released v0 row 1 ignorable must be true when present"),
            _decode_event_row,
            {"type": "turn/start", "seq": 0, "time": 5, "data": {}, "ignorable": 1},
            0, 0)

    def test_type_must_be_non_empty_string(self):
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("released v0 row 1 type must be a non-empty string"),
            _decode_event_row,
            {"type": "", "seq": 0, "time": 5, "data": {}}, 0, 0)

    def test_source_event_seqs_decoded(self):
        event = _decode_event_row(
            {"type": "user/message", "seq": 3, "time": 5, "data": {},
             "sourceEventSeqs": [[0, 2]]}, 3, 1)
        self.assertEqual(event["sourceEventSeqs"], [0, 1, 2])

    def test_source_event_seqs_none_returns_empty(self):
        event = _decode_event_row(
            {"type": "user/message", "seq": 3, "time": 5, "data": {},
             "sourceEventSeqs": None}, 3, 1)
        self.assertEqual(event["sourceEventSeqs"], [])


class ProvenanceTest(unittest.TestCase):
    def test_none_returns_empty(self):
        self.assertEqual(_decode_provenance(None, 3, "label"), [])

    def test_non_array(self):
        self.assertRaisesRegex(
            SessionFormatError, re.escape("label sourceEventSeqs must be an array"),
            _decode_provenance, "nope", 3, "label")

    def test_folded_range_expands(self):
        self.assertEqual(_decode_provenance([[0, 2]], 3, "label"), [0, 1, 2])

    def test_invalid_range_type_error(self):
        with self.assertRaises(TypeError) as ctx:
            _decode_provenance([[2, 0]], 3, "label")
        self.assertIn("ranges require start <= end", str(ctx.exception))


class ScanRowsTest(unittest.TestCase):
    def test_strict_seq_gap(self):
        rows = [ev(0, 1, "turn/start", {"turn": 1}),
                ev(2, 3, "turn/end", {"turn": 1, "reason": {"kind": "completed"}})]
        self.assertRaisesRegex(
            SessionFormatError,
            re.escape("released v1 row 2 has seq gap (expected 1, got 2)"),
            RELEASED_V1_CODEC["decode_artifact"], v1_header(), rows)

    def test_strict_malformed_provenance_wraps_packed_row_error(self):
        rows = [ev(0, 1, "turn/start", {"turn": 1}),
                ev(1, 2, "turn/end", {"turn": 1, "reason": {"kind": "completed"}},
                   sourceEventSeqs=[[2, 0]])]
        self.assertRaisesRegex(
            PackedRowError,
            re.escape("released v1 row 2 is malformed: sourceEventSeqs ranges "
                      "require start <= end"),
            RELEASED_V1_CODEC["decode_artifact"], v1_header(), rows)

    def test_strict_provenance_exceeds_event_sequence(self):
        rows = [ev(0, 1, "turn/start", {"turn": 1}),
                ev(1, 2, "turn/end", {"turn": 1, "reason": {"kind": "completed"}},
                   sourceEventSeqs=[[0, 5]])]
        self.assertRaisesRegex(
            PackedRowError,
            re.escape("released v1 row 2 is malformed: sourceEventSeqs range exceeds "
                      "its event sequence"),
            RELEASED_V1_CODEC["decode_artifact"], v1_header(), rows)

    def test_recoverable_rolls_back_gap(self):
        rows = [ev(0, 1, "turn/start", {"turn": 1}),
                ev(2, 3, "turn/end", {"turn": 1, "reason": {"kind": "completed"}})]
        artifact = RELEASED_V1_CODEC["decode_recoverable_artifact"](v1_header(), rows)
        self.assertEqual([event["seq"] for event in artifact["events"]], [0])

    def test_recoverable_keeps_prefix_after_issue(self):
        rows = [ev(0, 1, "turn/start", {"turn": 1}),
                "bad-row",
                ev(1, 2, "step/start", {"turn": 1, "step": 1})]
        artifact = RELEASED_V1_CODEC["decode_recoverable_artifact"](v1_header(), rows)
        self.assertEqual([event["type"] for event in artifact["events"]], ["turn/start"])

    def test_recoverable_raises_issue_on_terminal_frame(self):
        rows = [ev(0, 1, "turn/start", {"turn": 1}),
                "bad-row",
                ev(1, 2, "turn/end", {"turn": 1, "reason": {"kind": "completed"}})]
        self.assertRaisesRegex(
            SessionFormatError, re.escape("released v1 row 2 must be a JSON object"),
            RELEASED_V1_CODEC["decode_recoverable_artifact"], v1_header(), rows)


if __name__ == "__main__":
    unittest.main()

