"""v3→v4 迁移的未覆盖分支测试（补充 tests/test_released_migration.py 的
`V3ToV4MigrationTest`，不重复其已覆盖向量）。

覆盖 `miniharness/core/session/released/migrate_v3_to_v4.py` 的边界分支：
tool/result 提升的各类拒绝与扩展字段、消息 source 改名变体、事件内容标签
迁移、显式引用重映射（`_remap_v3_references` 全表）、`_Stage` 的修复/继承
/交付守卫/dense/词表路径。期望值均取自源实现。
"""
import unittest

from miniharness.core.session.released import (
    SessionFormatError,
    SessionFormatUnsupportedMigrationError,
    V3_TO_V4,
)
from miniharness.core.session.released import migrate_v3_to_v4 as mv4


def ev(seq, etype, data, **extra):
    return {"type": etype, "seq": seq, "time": 100 + seq, "data": data, **extra}


def text_block(text):
    return {"type": "text", "text": text}


def tool_result_event(seq, message, **extra):
    return ev(seq, "tool/result", message, **extra)


class ResultLiftingBranchesTest(unittest.TestCase):
    def test_result_content_requires_array(self):
        with self.assertRaises(SessionFormatError) as raised:
            mv4._result_content("nope", "subject")
        self.assertEqual(str(raised.exception),
                         "subject tool-result content must be an array")

    def test_result_content_rejects_nested_tool_result(self):
        with self.assertRaises(SessionFormatUnsupportedMigrationError) as raised:
            mv4._result_content([{"type": "tool-result"}], "subject")
        self.assertEqual(
            str(raised.exception),
            "subject contains a nested tool-result unsupported by this converter")

    def test_lift_returns_event_for_non_wrapped_shapes(self):
        plain = ev(2, "feedback/record", {"text": "x"})
        self.assertIs(mv4._lift_tool_result(plain), plain)
        not_object = {"type": "tool/result", "seq": 3, "data": "x"}
        self.assertIs(mv4._lift_tool_result(not_object), not_object)
        non_user = {"type": "tool/result", "seq": 4,
                    "data": {"message": {"role": "assistant"}}}
        self.assertIs(mv4._lift_tool_result(non_user), non_user)

    def test_lift_requires_matching_wrapper(self):
        cases = [
            # 空 id
            {"id": "", "role": "user",
             "content": [{"type": "tool-result", "toolCallId": "c"}],
             "source": {"kind": "tool", "callId": "c"}},
            # source.kind 不是 tool
            {"id": "r", "role": "user",
             "content": [{"type": "tool-result", "toolCallId": "c"}],
             "source": {"kind": "plugin", "callId": "c"}},
            # wrapper.type 不是 tool-result
            {"id": "r", "role": "user", "content": [text_block("o")],
             "source": {"kind": "tool", "callId": "c"}},
            # wrapper.toolCallId 与 source.callId 不匹配
            {"id": "r", "role": "user",
             "content": [{"type": "tool-result", "toolCallId": "other"}],
             "source": {"kind": "tool", "callId": "c"}},
            # content 非恰一个 block
            {"id": "r", "role": "user",
             "content": [{"type": "tool-result", "toolCallId": "c"},
                         {"type": "tool-result", "toolCallId": "c"}],
             "source": {"kind": "tool", "callId": "c"}},
        ]
        for index, message in enumerate(cases):
            with self.subTest(index=index):
                event = tool_result_event(7, {"message": message})
                with self.assertRaises(SessionFormatError) as raised:
                    mv4._lift_tool_result(event)
                self.assertEqual(
                    str(raised.exception),
                    "format v3 tool/result at seq 7 requires exactly one "
                    "tool-result wrapper matching its tool source")

    def test_lift_rejects_non_boolean_is_error(self):
        message = {"id": "r", "role": "user",
                   "content": [{"type": "tool-result", "toolCallId": "c",
                                "content": [text_block("o")], "isError": "yes"}],
                   "source": {"kind": "tool", "callId": "c"}}
        with self.assertRaises(SessionFormatError) as raised:
            mv4._lift_tool_result(tool_result_event(8, {"message": message}))
        self.assertEqual(
            str(raised.exception),
            "format v3 tool/result at seq 8 tool-result isError must be boolean")

    def test_lift_prefixes_extension_provenance(self):
        wrapper = {"type": "tool-result", "toolCallId": "c",
                   "content": [text_block("o")], "isError": False,
                   "durationMs": 12}
        message = {"id": "r", "role": "user", "content": [wrapper],
                   "source": {"kind": "tool", "callId": "c"},
                   "note": "kept"}
        event = tool_result_event(9, {"turn": 1, "step": 1,
                                      "message": message})
        target = mv4._lift_tool_result(event)["data"]["message"]
        self.assertEqual(target["role"], "tool")
        self.assertEqual(target["toolCallId"], "c")
        self.assertEqual(target["id"], "r")
        self.assertIs(target["isError"], False)
        self.assertEqual(target["content"], [text_block("o")])
        self.assertEqual(target["plugin:message:note"], "kept")
        self.assertEqual(target["plugin:result:durationMs"], 12)


class SourceRenamingBranchesTest(unittest.TestCase):
    def test_producer_kind_variants(self):
        self.assertEqual(mv4._producer_kind("compact", "user"),
                         "compact-checkpoint")
        self.assertEqual(mv4._producer_kind("tools-code-mode", "user"),
                         "ptc-mode")
        self.assertEqual(mv4._producer_kind("tools-ptc", "user"), "ptc-mode")
        self.assertEqual(mv4._producer_kind("dsh-compaction-basic", "user"),
                         "compact-basic")
        self.assertEqual(
            mv4._producer_kind("@deepseek-ai/dsh-system-prompt", "system"),
            "system-prompt")
        self.assertEqual(
            mv4._producer_kind("@deepseek-ai/dsh-system-prompt", "user"),
            "runtime-context")
        self.assertEqual(mv4._producer_kind("plan-mode", "assistant"),
                         "plan-mode")
        self.assertEqual(mv4._producer_kind("mystery-plugin", "user"),
                         "plugin:mystery-plugin")

    def test_rewrite_plugin_source_requires_string_plugin(self):
        with self.assertRaises(SessionFormatError) as raised:
            mv4._rewrite_plugin_source({"kind": "plugin", "plugin": 5}, 9, "user")
        self.assertEqual(
            str(raised.exception),
            "plugin source at seq 9 is not canonical: plugin requires a string")

    def test_rewrite_plugin_source_preserves_other_keys(self):
        out = mv4._rewrite_plugin_source(
            {"kind": "plugin", "plugin": "compact", "extra": 1}, 3, "user")
        self.assertEqual(out, {"kind": "compact-checkpoint", "extra": 1})

    def test_rewrite_message_source_requires_nonempty_kind(self):
        for kind in ("", 5):
            with self.subTest(kind=kind):
                with self.assertRaises(SessionFormatError) as raised:
                    mv4._rewrite_message_source({"kind": kind}, 3, "user")
                self.assertEqual(
                    str(raised.exception),
                    "message source at seq 3 requires a nonempty kind")

    def test_rewrite_message_source_delegates_plugin(self):
        out = mv4._rewrite_message_source(
            {"kind": "plugin", "plugin": "compact"}, 4, "user")
        self.assertEqual(out, {"kind": "compact-checkpoint"})

    def test_rewrite_message_source_passes_through_known_kind(self):
        source = {"kind": "model", "provider": "x"}
        self.assertIs(mv4._rewrite_message_source(source, 4, "assistant"), source)

    def test_map_event_messages_ignores_non_object_data(self):
        event = {"type": "user/message", "seq": 1, "time": 1, "data": "x"}
        self.assertIs(mv4._map_event_messages(event, lambda m: m), event)

    def test_map_event_messages_requires_message_object(self):
        event = ev(2, "system/message", {"turn": 1, "step": 1})
        with self.assertRaises(SessionFormatError) as raised:
            mv4._map_event_messages(event, lambda m: m)
        self.assertEqual(str(raised.exception),
                         "system/message requires a message")
        event = ev(3, "tool/result", {"turn": 1, "message": "x"})
        with self.assertRaises(SessionFormatError) as raised:
            mv4._map_event_messages(event, lambda m: m)
        self.assertEqual(str(raised.exception),
                         "tool/result requires a message")

    def test_map_event_messages_array_keys(self):
        seen = []

        def transform(message):
            seen.append(message)
            return {**message, "seen": True}

        spliced = ev(4, "agent/inbox/spliced",
                     {"target": "current", "start": 0,
                      "inserted": [{"source": {"kind": "user"}}]})
        out = mv4._map_event_messages(spliced, transform)
        self.assertTrue(out["data"]["inserted"][0]["seen"])
        self.assertEqual(seen[0]["source"], {"kind": "user"})

        title = ev(5, "session/title-llm-request",
                   {"messages": [{"role": "user"}]})
        out = mv4._map_event_messages(title, transform)
        self.assertTrue(out["data"]["messages"][0]["seen"])

    def test_map_event_messages_array_errors(self):
        with self.assertRaises(SessionFormatError) as raised:
            mv4._map_event_messages(
                ev(6, "agent/inbox/spliced", {"inserted": "x"}), lambda m: m)
        self.assertEqual(str(raised.exception),
                         "agent/inbox/spliced requires message array")
        with self.assertRaises(SessionFormatError) as raised:
            mv4._map_event_messages(
                ev(7, "agent/inbox/spliced", {"inserted": [5]}), lambda m: m)
        self.assertEqual(str(raised.exception),
                         "agent/inbox/spliced requires message objects")
        with self.assertRaises(SessionFormatError) as raised:
            mv4._map_event_messages(
                ev(8, "session/title-llm-request", {"messages": [5]}),
                lambda m: m)
        self.assertEqual(str(raised.exception),
                         "session/title-llm-request requires message objects")

    def test_rewrite_event_sources_skips_non_object_source(self):
        event = ev(9, "user/message",
                   {"id": "u", "role": "user", "content": [], "source": "bad"})
        self.assertIs(mv4._rewrite_event_sources(event), event)

    def test_rewrite_event_sources_renames_plugin_source(self):
        event = ev(10, "user/message",
                   {"id": "u", "role": "user", "content": [],
                    "source": {"kind": "plugin", "plugin": "compact"}})
        out = mv4._rewrite_event_sources(event)
        self.assertEqual(out["data"]["source"], {"kind": "compact-checkpoint"})


class ContentMigrationBranchesTest(unittest.TestCase):
    def test_migrate_block_requires_string_type(self):
        for value in ("x", {"type": 5}, {"no": "type"}):
            with self.subTest(value=value):
                with self.assertRaises(SessionFormatError) as raised:
                    mv4._migrate_block(value, "subject")
                self.assertEqual(
                    str(raised.exception),
                    "subject requires content blocks with string type tags")

    def test_migrate_block_tags_unknown_type(self):
        known = {"type": "text", "text": "t"}
        self.assertIs(mv4._migrate_block(known, "s"), known)
        self.assertEqual(mv4._migrate_block({"type": "custom"}, "s"),
                         {"type": "plugin:custom"})

    def test_migrate_content_requires_array(self):
        with self.assertRaises(SessionFormatError) as raised:
            mv4._migrate_content("x", "subject")
        self.assertEqual(str(raised.exception), "subject content must be an array")

    def test_migrate_content_identity_and_mapped(self):
        known = [{"type": "text", "text": "t"}]
        self.assertIs(mv4._migrate_content(known, "s"), known)
        self.assertEqual(mv4._migrate_content([{"type": "custom"}], "s"),
                         [{"type": "plugin:custom"}])

    def test_migrate_message_identity_and_mapped(self):
        unchanged = {"content": [{"type": "text", "text": "t"}]}
        self.assertIs(mv4._migrate_message(unchanged, "s"), unchanged)
        changed = mv4._migrate_message({"content": [{"type": "custom"}]}, "s")
        self.assertEqual(changed["content"], [{"type": "plugin:custom"}])

    def test_migrate_chunk_identity_for_non_object(self):
        self.assertEqual(mv4._migrate_chunk("x", "s"), "x")

    def test_migrate_chunk_block_end(self):
        known = {"type": "block-end", "block": {"type": "text", "text": "t"}}
        self.assertIs(mv4._migrate_chunk(known, "s"), known)
        changed = mv4._migrate_chunk(
            {"type": "block-end", "block": {"type": "custom"}}, "s")
        self.assertEqual(changed["block"], {"type": "plugin:custom"})

    def test_migrate_chunk_block_start(self):
        known = {"type": "block-start", "blockType": "text"}
        self.assertIs(mv4._migrate_chunk(known, "s"), known)
        changed = mv4._migrate_chunk({"type": "block-start",
                                      "blockType": "custom"}, "s")
        self.assertEqual(changed["blockType"], "plugin:custom")
        with self.assertRaises(SessionFormatError) as raised:
            mv4._migrate_chunk({"type": "block-start", "blockType": 5}, "s")
        self.assertEqual(str(raised.exception), "s blockType must be a string")

    def test_migrate_chunk_passthrough_other_type(self):
        chunk = {"type": "finish", "reason": {"kind": "stop"}}
        self.assertIs(mv4._migrate_chunk(chunk, "s"), chunk)

    def test_event_content_returns_when_data_not_object(self):
        event = {"type": "feedback/record", "seq": 1, "time": 1, "data": "x"}
        self.assertEqual(mv4._migrate_event_content(event), event)

    def test_event_content_compaction_summary(self):
        event = ev(1, "compaction/summary",
                   {"summary": [{"type": "custom"}],
                    "rawOutput": [{"type": "other"}]})
        out = mv4._migrate_event_content(event)
        self.assertEqual(out["data"]["summary"], [{"type": "plugin:custom"}])
        self.assertEqual(out["data"]["rawOutput"], [{"type": "plugin:other"}])

    def test_event_content_ptc_dispatch(self):
        event = ev(2, "tool/ptc-dispatch", {"content": [{"type": "custom"}]})
        out = mv4._migrate_event_content(event)
        self.assertEqual(out["data"]["content"], [{"type": "plugin:custom"}])

    def test_event_content_team_message_queued(self):
        event = ev(3, "team/message/queued",
                   {"message": {"content": [{"type": "custom"}]}})
        out = mv4._migrate_event_content(event)
        self.assertEqual(out["data"]["message"]["content"],
                         [{"type": "plugin:custom"}])

    def test_event_content_request_header_rejects_defer_loading(self):
        event = ev(4, "request/header",
                   {"header": {"tools": [{"name": "x", "deferLoading": True}]},
                    "reason": "initial"})
        with self.assertRaises(SessionFormatUnsupportedMigrationError) as raised:
            mv4._migrate_event_content(event)
        self.assertIn(
            "header.tools[0] contains deferLoading, which is only defined in V4",
            str(raised.exception))

    def test_event_content_stream_chunks(self):
        event = ev(5, "assistant/attempt",
                   {"turn": 1, "step": 1,
                    "stream": [
                        {"type": "chunk", "time": 10,
                         "chunk": {"type": "block-start",
                                   "blockType": "custom"}},
                        {"type": "text-chunks", "time0": 10, "index": 0,
                         "texts": ["a"]},
                    ]})
        out = mv4._migrate_event_content(event)
        self.assertEqual(out["data"]["stream"][0]["chunk"]["blockType"],
                         "plugin:custom")
        self.assertEqual(out["data"]["stream"][1]["type"], "text-chunks")


class ReferenceRemapBranchesTest(unittest.TestCase):
    MAPPING = list(range(10))

    def test_identity_when_target_equals_event_seq(self):
        event = ev(6, "feedback/record", {"text": "x"})
        self.assertIs(mv4._remap_v3_references(event, 6, self.MAPPING), event)

    def test_command_done_source_event_seq(self):
        event = ev(6, "command/done",
                   {"commandId": "c", "kind": "success", "sourceEventSeq": 3})
        out = mv4._remap_v3_references(event, 7, self.MAPPING)
        self.assertEqual(out["data"]["sourceEventSeq"], 3)
        self.assertEqual(out["seq"], 7)

    def test_reference_must_name_earlier_event(self):
        event = ev(6, "command/done",
                   {"commandId": "c", "kind": "success", "sourceEventSeq": 6})
        with self.assertRaises(SessionFormatError) as raised:
            mv4._remap_v3_references(event, 7, self.MAPPING)
        self.assertEqual(str(raised.exception),
                         "V3 reference must name an earlier source event")

    def test_compaction_summary_range_and_seqs(self):
        event = ev(7, "compaction/summary",
                   {"shadowedRange": {"start": 1, "end": 2},
                    "shadowedSeqs": [0, 1]})
        out = mv4._remap_v3_references(event, 8, self.MAPPING)
        self.assertEqual(out["data"]["shadowedRange"], {"start": 1, "end": 2})
        self.assertEqual(out["data"]["shadowedSeqs"], [0, 1])

    def test_compaction_prune_range_and_seqs(self):
        event = ev(8, "compaction/prune",
                   {"shadowedRange": {"start": 3, "end": 4},
                    "shadowedSeqs": [5]})
        out = mv4._remap_v3_references(event, 9, self.MAPPING)
        self.assertEqual(out["data"]["shadowedRange"], {"start": 3, "end": 4})
        self.assertEqual(out["data"]["shadowedSeqs"], [5])

    def test_compaction_range_must_be_object(self):
        event = ev(8, "compaction/summary",
                   {"shadowedRange": "x", "shadowedSeqs": []})
        with self.assertRaises(SessionFormatError) as raised:
            mv4._remap_v3_references(event, 9, self.MAPPING)
        self.assertEqual(str(raised.exception),
                         "V3 event reference container must be an object")

    def test_compaction_seqs_must_be_array(self):
        event = ev(8, "compaction/summary",
                   {"shadowedRange": {"start": 1, "end": 2},
                    "shadowedSeqs": "x"})
        with self.assertRaises(SessionFormatError) as raised:
            mv4._remap_v3_references(event, 9, self.MAPPING)
        self.assertEqual(str(raised.exception),
                         "V3 event references must be an array")

    def test_title_message_seqs(self):
        event = ev(9, "session/title",
                   {"title": "t", "messageSeqs": [1, 2],
                    "source": {"kind": "user"}})
        out = mv4._remap_v3_references(event, 10, self.MAPPING)
        self.assertEqual(out["data"]["messageSeqs"], [1, 2])

    def test_title_llm_request_message_seqs(self):
        event = ev(9, "session/title-llm-request",
                   {"messageSeqs": [4, 5]})
        out = mv4._remap_v3_references(event, 10, self.MAPPING)
        self.assertEqual(out["data"]["messageSeqs"], [4, 5])

    def test_image_offload_targets(self):
        event = ev(9, "image/offload",
                   {"targets": [{"seq": 1, "imageIndexes": [0]},
                                {"seq": 2, "imageIndexes": [1]}]})
        out = mv4._remap_v3_references(event, 10, self.MAPPING)
        self.assertEqual(out["data"]["targets"],
                         [{"seq": 1, "imageIndexes": [0]},
                          {"seq": 2, "imageIndexes": [1]}])

    def test_image_offload_targets_must_be_array(self):
        event = ev(9, "image/offload", {"targets": "x"})
        with self.assertRaises(SessionFormatError) as raised:
            mv4._remap_v3_references(event, 10, self.MAPPING)
        self.assertEqual(str(raised.exception),
                         "V3 image offload targets must be an array")

    def test_image_offload_target_must_be_object(self):
        event = ev(9, "image/offload", {"targets": [5]})
        with self.assertRaises(SessionFormatError) as raised:
            mv4._remap_v3_references(event, 10, self.MAPPING)
        self.assertEqual(str(raised.exception),
                         "V3 event reference container must be an object")

    def test_event_source_event_seqs(self):
        event = ev(8, "command/run",
                   {"commandId": "c", "name": "n", "source": {"kind": "user"}})
        event["sourceEventSeqs"] = [1, 2]
        out = mv4._remap_v3_references(event, 7, self.MAPPING)
        self.assertEqual(out["sourceEventSeqs"], [1, 2])
        self.assertEqual(out["seq"], 7)

    def test_surface_op_replace_remaps_endpoints(self):
        event = ev(8, "user/message",
                   {"id": "u", "role": "user", "content": [],
                    "source": {"kind": "user"}},
                   surfaceOp={"op": "replace", "startSeq": 1, "endSeq": 2})
        out = mv4._remap_v3_references(event, 7, self.MAPPING)
        self.assertEqual(out["surfaceOp"],
                         {"op": "replace", "startSeq": 1, "endSeq": 2})

    def test_surface_op_append_is_untouched(self):
        event = ev(8, "user/message",
                   {"id": "u", "role": "user", "content": [],
                    "source": {"kind": "user"}}, surfaceOp="append")
        out = mv4._remap_v3_references(event, 7, self.MAPPING)
        self.assertEqual(out["surfaceOp"], "append")

    def test_surface_op_invalid_container(self):
        event = ev(8, "user/message",
                   {"id": "u", "role": "user", "content": [],
                    "source": {"kind": "user"}}, surfaceOp="weird")
        with self.assertRaises(SessionFormatError) as raised:
            mv4._remap_v3_references(event, 7, self.MAPPING)
        self.assertEqual(str(raised.exception),
                         "V3 event reference container must be an object")


class StageBranchesTest(unittest.TestCase):
    def _stage(self, is_seeded=False, source_inherited=None, session_id="s"):
        return mv4._Stage({"createdAt": 1, "isSeeded": is_seeded,
                           "id": session_id}, source_inherited)

    def test_transform_requires_dense_events(self):
        stage = self._stage()
        with self.assertRaises(SessionFormatError) as raised:
            stage.transform(ev(5, "turn/start", {"turn": 1}))
        self.assertEqual(str(raised.exception),
                         "format v3 source events must be dense")

    def test_interrupted_turn_repair_is_inserted(self):
        stage = self._stage()
        stage.transform(ev(0, "turn/start", {"turn": 1}))
        stage.transform(ev(1, "agent/inbox/spliced",
                           {"target": "next-turn", "start": 0,
                            "inserted": [{"role": "user", "content": [],
                                          "source": {"kind": "user"}}]}))
        stage.transform(ev(2, "turn/start", {"turn": 2}))
        self.assertEqual([e["type"] for e in stage.out_events],
                         ["turn/start", "agent/inbox/spliced",
                          "turn/end", "turn/start"])
        repair = stage.out_events[2]
        self.assertEqual(repair["data"],
                         {"turn": 1, "reason": {"kind": "interrupted"}})
        self.assertEqual(repair["seq"], 2)
        self.assertEqual(stage.out_events[3]["seq"], 3)

    def test_end_seed_inherited_requires_seeded_session(self):
        stage = self._stage(is_seeded=False)
        with self.assertRaises(SessionFormatError) as raised:
            stage.transform(ev(0, "session/end-seed", {"inherited": True}))
        self.assertEqual(
            str(raised.exception),
            "format v3 unseeded Session contains an inherited end-seed marker")

    def test_end_seed_inherited_sets_cut(self):
        stage = self._stage(is_seeded=True)
        stage.transform(ev(0, "session/end-seed", {"inherited": True}))
        self.assertEqual(stage.source_cut, 0)
        self.assertEqual(stage.cut, 0)

    def test_delivery_marker_claiming_v4_is_refused(self):
        stage = self._stage()
        with self.assertRaises(SessionFormatUnsupportedMigrationError) as raised:
            stage.transform(ev(0, "session-log-deepseek/delivery-accepted",
                               {"sessionId": "s", "throughSeq": 0,
                                "sessionFormatVersion": 4}))
        self.assertEqual(str(raised.exception),
                         "format v3 delivery marker claims target format v4")

    def test_foreign_delivery_marker_recorded(self):
        stage = self._stage()
        stage.transform(ev(0, "turn/start", {"turn": 1}))
        stage.transform(ev(1, "session-log-deepseek/delivery-accepted",
                           {"sessionId": "other", "throughSeq": 0,
                            "sessionFormatVersion": 3}))
        self.assertEqual(stage.foreign_delivery_seq, 1)

    def test_ignorable_unknown_type_is_namespaced(self):
        stage = self._stage()
        stage.transform({"type": "mystery/event", "seq": 0, "time": 1,
                         "data": {"x": 1}, "ignorable": True})
        self.assertEqual(stage.out_events[0]["type"], "plugin:mystery/event")
        self.assertEqual(stage.out_events[0]["seq"], 0)

    def test_unknown_type_is_refused(self):
        stage = self._stage()
        with self.assertRaises(SessionFormatUnsupportedMigrationError) as raised:
            stage.transform({"type": "mystery/event", "seq": 0, "time": 1,
                             "data": {}})
        self.assertEqual(
            str(raised.exception),
            "format v3 contains unknown event type 'mystery/event' at seq 0")

    def test_finish_rejects_inherited_cut_mismatch(self):
        stage = self._stage(is_seeded=False, source_inherited=5)
        with self.assertRaises(SessionFormatError) as raised:
            stage.finish()
        self.assertEqual(
            str(raised.exception),
            "format v3 inherited cut disagrees with its source marker")

    def test_finish_rejects_foreign_delivery(self):
        stage = self._stage()
        stage.transform(ev(0, "turn/start", {"turn": 1}))
        stage.transform(ev(1, "session-log-deepseek/delivery-accepted",
                           {"sessionId": "other", "throughSeq": 0,
                            "sessionFormatVersion": 3}))
        with self.assertRaises(SessionFormatError) as raised:
            stage.finish()
        self.assertEqual(
            str(raised.exception),
            "current-generation delivery marker names the wrong Session")

    def test_validate_delivery_requires_object_data(self):
        stage = self._stage()
        with self.assertRaises(SessionFormatError) as raised:
            stage._validate_delivery(
                {"type": "session-log-deepseek/delivery-accepted",
                 "seq": 5, "data": 5})
        self.assertEqual(str(raised.exception),
                         "delivery-accepted data must be an object")

    def test_validate_delivery_through_seq_must_precede(self):
        stage = self._stage()
        with self.assertRaises(SessionFormatError) as raised:
            stage._validate_delivery(
                {"type": "session-log-deepseek/delivery-accepted", "seq": 0,
                 "data": {"sessionId": "s", "throughSeq": 0,
                          "sessionFormatVersion": 3}})
        self.assertEqual(str(raised.exception),
                         "delivery throughSeq must precede its marker")

    def test_validate_delivery_requires_session_id(self):
        stage = self._stage()
        with self.assertRaises(SessionFormatError) as raised:
            stage._validate_delivery(
                {"type": "session-log-deepseek/delivery-accepted", "seq": 5,
                 "data": {"sessionId": "", "throughSeq": 0,
                          "sessionFormatVersion": 3}})
        self.assertEqual(str(raised.exception),
                         "delivery requires a nonempty Session id")

    def test_validate_delivery_non_v3_returns_none(self):
        stage = self._stage()
        self.assertIsNone(stage._validate_delivery(
            {"type": "session-log-deepseek/delivery-accepted", "seq": 5,
             "data": {"sessionId": "s", "throughSeq": 0,
                      "sessionFormatVersion": 2}}))
        self.assertIsNone(stage._validate_delivery(
            {"type": "turn/start", "seq": 5, "data": {"turn": 1}}))


class V3ToV4EndToEndTest(unittest.TestCase):
    def _artifact_v3(self, events, is_seeded=False, parent=None):
        header = {"version": 3, "id": "s-v3", "createdAt": 1,
                  "isSeeded": is_seeded, "delegationDepth": 0}
        if parent is not None:
            header["parentSession"] = parent
        return {"header": header, "inherited_event_count": 0,
                "events": events}

    def test_multiple_tool_results_and_plugin_source_prefix(self):
        def wrapper(call_id, text):
            return {"type": "tool-result", "toolCallId": call_id,
                    "content": [text_block(text)]}

        events = [
            ev(0, "turn/start", {"turn": 1}),
            ev(1, "step/start", {"turn": 1, "step": 1}),
            ev(2, "user/message",
               {"id": "u1", "role": "user", "content": [text_block("hi")],
                "source": {"kind": "plugin", "plugin": "mystery"}},
               surfaceOp="append"),
            ev(3, "assistant/message",
               {"turn": 1, "step": 1,
                "message": {"id": "a1", "role": "assistant",
                            "content": [{"type": "tool-call", "id": "c1",
                                         "name": "read", "arguments": "{}"},
                                        {"type": "tool-call", "id": "c2",
                                         "name": "read", "arguments": "{}"}],
                            "source": {"kind": "model", "provider": "fake",
                                       "model": "fake"}},
                "stream": [
                    {"type": "tool-call-chunks", "time0": 103, "index": 0,
                     "dt": [], "id": "c1", "name": "read", "args": ["{}"]},
                    {"type": "tool-call-chunks", "time0": 103, "index": 1,
                     "dt": [], "id": "c2", "name": "read", "args": ["{}"]},
                    {"type": "chunk", "time": 104,
                     "chunk": {"type": "finish",
                               "reason": {"kind": "tool-calls"}}},
                ]},
               surfaceOp="append"),
            ev(4, "tool/call",
               {"turn": 1, "step": 1, "callId": "c1", "name": "read",
                "arguments": "{}"}),
            ev(5, "tool/call",
               {"turn": 1, "step": 1, "callId": "c2", "name": "read",
                "arguments": "{}"}),
            ev(6, "tool/result",
               {"turn": 1, "step": 1,
                "message": {"id": "r1", "role": "user",
                            "content": [wrapper("c1", "out-1")],
                            "source": {"kind": "tool", "callId": "c1"}}},
               sourceEventSeqs=[4], surfaceOp="append"),
            ev(7, "tool/result",
               {"turn": 1, "step": 1,
                "message": {"id": "r2", "role": "user",
                            "content": [wrapper("c2", "out-2")],
                            "source": {"kind": "tool", "callId": "c2"}}},
               sourceEventSeqs=[5], surfaceOp="append"),
            ev(8, "step/end", {"turn": 1, "step": 1}),
            ev(9, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        target = V3_TO_V4["migrate"](self._artifact_v3(events))
        self.assertEqual(target["header"]["version"], 4)
        user = next(e for e in target["events"] if e["type"] == "user/message")
        self.assertEqual(user["data"]["source"], {"kind": "plugin:mystery"})
        results = [e for e in target["events"] if e["type"] == "tool/result"]
        self.assertEqual(len(results), 2)
        self.assertEqual([r["data"]["message"]["toolCallId"]
                          for r in results], ["c1", "c2"])
        self.assertTrue(all(r["data"]["message"]["role"] == "tool"
                            for r in results))

    def test_migrate_header_translates_version(self):
        translated = V3_TO_V4["migrate_header"](
            {"version": 3, "id": "s", "createdAt": 1, "isSeeded": False,
             "delegationDepth": 0})
        self.assertEqual(translated["version"], 4)
        self.assertEqual(translated["id"], "s")


if __name__ == "__main__":
    unittest.main()
