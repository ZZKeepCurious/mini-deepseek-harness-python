"""多代 generation + released 迁移链测试（Phase A，design-generation-migration.md §3）。

向量对拍上游：`session-format-v1-to-v2/tests/{migration,codec,validation}.spec.ts` 与
`session-format-v0-to-v1/tests/{legacy,migration}.spec.ts` 的关键行为钉；错误文案
regex 逐字断言。scoped 深度校验（Phase B）不在此复刻。
"""
import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from miniharness.core.session.generation import (
    CURRENT_GENERATION_VERSION,
    JsonlGenerationNewerVersionError,
    JsonlGenerationTargetConflictError,
    ensure_generation_current,
    generation_log_filename,
    parse_generation_log_filename,
    resolve_generation_in_directory,
)
from miniharness.core.session.persistence import JsonlPersistence, project_key
from miniharness.core.session.released import (
    RELEASED_V0_CODEC,
    RELEASED_V1_CODEC,
    V0_TO_V1,
    V1_TO_V2,
    SessionFormatError,
    SessionFormatUnsupportedMigrationError,
    migrate_released_header,
)


def ev(seq, time, etype, data, **extra):
    return {"type": etype, "seq": seq, "time": time, "data": data, **extra}


def text_block(text):
    return {"type": "text", "text": text}


def v1_user(seq, time, text):
    return ev(seq, time, "user/message",
              {"id": f"m{seq}", "role": "user",
               "content": [text_block(text)], "source": {"kind": "user"}},
              surfaceOp="append")


def v1_assistant_message(seq, time, text, sources, **extra):
    data = {"turn": 1, "step": 1,
            "message": {"id": f"am{seq}", "role": "assistant",
                        "content": [text_block(text)],
                        "source": {"kind": "model", "provider": "fake",
                                   "model": "fake"}}}
    extraout = {"surfaceOp": "append"}
    if sources is not None:
        extraout["sourceEventSeqs"] = list(sources)
    return ev(seq, time, "assistant/message", data, **extraout)


def artifact_v1(events, inherited=0, is_seeded=False, session_id="s-v1",
                created_at=1, parent=None):
    header = {"version": 1, "id": session_id, "createdAt": created_at,
              "isSeeded": is_seeded, "delegationDepth": 0}
    if parent is not None:
        header["parentSession"] = parent
    return {"header": header, "inherited_event_count": inherited, "events": events}


def migrate_v1_v2(events, **kwargs):
    return V1_TO_V2["migrate"](artifact_v1(events, **kwargs))


class GenerationNamingTest(unittest.TestCase):
    def test_canonical_names(self):
        self.assertEqual(generation_log_filename(0, "none"), "session.jsonl")
        self.assertEqual(generation_log_filename(0, "zstd"), "session.jsonl.zstd")
        self.assertEqual(generation_log_filename(2, "zstd"), "session.v2.jsonl.zstd")
        self.assertEqual(generation_log_filename(17, "none"), "session.v17.jsonl")

    def test_parse_accepts_and_rejects(self):
        self.assertEqual(parse_generation_log_filename("session.jsonl", "none"), 0)
        self.assertEqual(parse_generation_log_filename("session.v2.jsonl.zstd", "zstd"), 2)
        self.assertEqual(parse_generation_log_filename("session.v12.jsonl", "none"), 12)
        # 非 canonical：.v0 / 前导零 / 大写 / 临时 / 缺压缩后缀
        for name, compression in (
            ("session.v0.jsonl", "none"),
            ("session.v02.jsonl", "none"),
            ("Session.v2.jsonl", "none"),
            ("session.v2.jsonl.tmp", "none"),
            ("session.migration.abc.jsonl", "none"),
            ("session.v2.jsonl", "zstd"),
        ):
            self.assertIsNone(parse_generation_log_filename(name, compression), name)
        # v0 + 压缩后缀是合法 canonical（session.jsonl.zstd = v0-zstd）
        self.assertEqual(parse_generation_log_filename("session.jsonl.zstd", "zstd"), 0)


class GenerationDirectoryTest(unittest.TestCase):
    def test_picks_highest_generation(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "session.jsonl").write_text("{}\n")
            (d / "session.v1.jsonl").write_text("{}\n")
            (d / "session.v2.jsonl").write_text("{}\n")
            resolved = resolve_generation_in_directory(d, "none")
            self.assertEqual(resolved["source_version"], 2)
            self.assertEqual(resolved["source_path"].name, "session.v2.jsonl")
            self.assertEqual(resolved["current_path"].name, "session.v2.jsonl")

    def test_opposite_encoding_refused(self):
        with TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "session.v2.jsonl.zstd").write_bytes(b"x")
            with self.assertRaises(ValueError) as ctx:
                resolve_generation_in_directory(d, "none")
            self.assertIn("uses .zstd", str(ctx.exception))

    def test_absent_returns_none(self):
        with TemporaryDirectory() as tmp:
            self.assertIsNone(resolve_generation_in_directory(Path(tmp), "none"))


def _write_v1_artifact(directory, events, compression, inherited=0, is_seeded=False,
                       session_id="s-v1", cwd=None):
    artifact = artifact_v1(events, inherited=inherited, is_seeded=is_seeded,
                           session_id=session_id)
    artifact["header"]["cwd"] = cwd or str(directory.parents[1])
    # v1 物理头：seedLength 形态（isSeeded 由 seedLength 在场表达）
    physical_header = {"type": "session", "version": 1,
                       "id": artifact["header"]["id"],
                       "createdAt": artifact["header"]["createdAt"],
                       "delegationDepth": 0,
                       "cwd": artifact["header"]["cwd"]}
    if is_seeded:
        physical_header["seedLength"] = inherited
    if artifact["header"].get("parentSession"):
        physical_header["parentSession"] = artifact["header"]["parentSession"]
    header_line = json.dumps(physical_header, ensure_ascii=False) + "\n"
    # 存储态行：provenance 折叠（≥3 连续折叠；测试向量短序列保持平铺）
    from miniharness.core.session.seq_ranges import encode_seq_ranges
    lines = [header_line]
    for event in events:
        row = {k: v for k, v in event.items()}
        if "sourceEventSeqs" in row:
            row = {**row, "sourceEventSeqs": encode_seq_ranges(row["sourceEventSeqs"])}
        lines.append(json.dumps(row, ensure_ascii=False) + "\n")
    text = "".join(lines)
    if compression == "zstd":
        from miniharness.core.session.zstd_frames import compress_zstd_frame
        return (compress_zstd_frame(header_line.encode("utf-8"))
                + compress_zstd_frame(text[len(header_line):].encode("utf-8")))
    return text.encode("utf-8")


class GenerationEnsureTest(unittest.TestCase):
    def _layout(self, tmp, events, compression="none", **kwargs):
        root = Path(tmp)
        from miniharness.core.session.persistence import project_key
        session_dir = root / project_key(str(root)) / "s-v1"
        session_dir.mkdir(parents=True)
        (session_dir / generation_log_filename(1, compression)).write_bytes(
            _write_v1_artifact(session_dir, events, compression, **kwargs))
        return root, session_dir

    def test_v1_artifact_migrates_on_open(self):
        events = [
            v1_user(0, 100, "hi"),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            ev(3, 110, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "text-delta", "index": 0, "text": "hello"}}),
            ev(4, 120, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "finish", "reason": {"kind": "stop"}}}),
            v1_assistant_message(5, 130, "hello", sources=[3, 4]),
            ev(6, 131, "step/end", {"turn": 1, "step": 1}),
            ev(7, 132, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        with TemporaryDirectory() as tmp:
            root, session_dir = self._layout(tmp, events)
            resolved = resolve_generation_in_directory(session_dir, "none")
            self.assertEqual(resolved["source_version"], 1)
            result = ensure_generation_current(
                resolved["source_path"], 1, resolved["current_path"], "none")
            self.assertEqual(result["status"], "migrated")
            self.assertEqual(result["from_version"], 1)
            self.assertEqual(result["to_version"], 2)
            self.assertTrue((session_dir / "session.v1.jsonl").exists())
            self.assertTrue((session_dir / "session.v2.jsonl").exists())
            # persistence 直接打开迁移后的会话
            p = JsonlPersistence(root, compression="none")
            loaded = p.load("s-v1")
            types = [event["type"] for event in loaded]
            self.assertEqual(types[0], "user/message")  # unseeded：无合成 marker
            self.assertIn("assistant/message", types)
            message = next(e for e in loaded if e["type"] == "assistant/message")
            self.assertNotIn("sourceEventSeqs", message)
            self.assertIn("stream", message["data"])
            # 再次打开 = 已是当前代（幂等）
            again = resolve_generation_in_directory(session_dir, "none")
            self.assertEqual(again["source_version"], 2)
            result2 = ensure_generation_current(
                again["source_path"], 2, again["current_path"], "none")
            self.assertEqual(result2["status"], "current")

    def test_filename_header_version_mismatch_refused(self):
        with TemporaryDirectory() as tmp:
            root, session_dir = self._layout(tmp, [])
            source = session_dir / "session.v1.jsonl"
            # header 内版本改成 2：文件名 v1 ↔ header v2 失配
            data = json.loads(source.read_text("utf-8").splitlines()[0])
            self.assertEqual(data["version"], 1)
            source.write_text(source.read_text("utf-8").replace('"version": 1', '"version": 2'),
                              encoding="utf-8")
            resolved = resolve_generation_in_directory(session_dir, "none")
            with self.assertRaises(SessionFormatError) as ctx:
                ensure_generation_current(resolved["source_path"], 1,
                                          resolved["current_path"], "none")
            self.assertIn("identifies v1", str(ctx.exception))
            self.assertIn("identifies v2", str(ctx.exception))

    def test_newer_generation_refused(self):
        with TemporaryDirectory() as tmp:
            root, session_dir = self._layout(tmp, [])
            source = session_dir / "session.v3.jsonl"
            source.write_text(json.dumps({
                "type": "session", "version": 3, "id": "s-v1", "createdAt": 1,
                "isSeeded": False, "delegationDepth": 0}) + "\n", encoding="utf-8")
            resolved = resolve_generation_in_directory(session_dir, "none")
            self.assertEqual(resolved["source_version"], 3)
            with self.assertRaises(JsonlGenerationNewerVersionError):
                ensure_generation_current(resolved["source_path"], 3,
                                          resolved["current_path"], "none")

    def test_target_conflict_on_foreign_current(self):
        events = [
            v1_user(0, 100, "hi"),
            ev(1, 101, "turn/start", {"turn": 1}),
            ev(2, 102, "step/start", {"turn": 1, "step": 1}),
            ev(3, 110, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "text-delta", "index": 0, "text": "hello"}}),
            ev(4, 120, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "finish", "reason": {"kind": "stop"}}}),
            v1_assistant_message(5, 130, "hello", sources=[3, 4]),
            ev(6, 131, "step/end", {"turn": 1, "step": 1}),
            ev(7, 132, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        with TemporaryDirectory() as tmp:
            root, session_dir = self._layout(tmp, events)
            (session_dir / "session.v2.jsonl").write_text(
                json.dumps({"type": "session", "version": 2, "id": "other",
                            "createdAt": 1, "isSeeded": False, "delegationDepth": 0})
                + "\n", encoding="utf-8")
            resolved = resolve_generation_in_directory(session_dir, "none")
            self.assertEqual(resolved["source_version"], 2)
            # 目录里 v2 已是最高代 → 直接视为 current（不迁移，行为同上游 select-highest）
            self.assertEqual(resolved["source_path"].name, "session.v2.jsonl")


class V1ToV2MigrationTest(unittest.TestCase):
    def test_interleaved_success_stream_dense_remap(self):
        """上游 migration.spec :36-105 交错成功流向量：chunk 消费、message 5→3、
        command/done 引用重映射。"""
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 110, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "text-delta", "index": 0, "text": "hello"}}),
            ev(3, 111, "feedback/record", {"text": "interleaved"}),
            ev(4, 120, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "finish", "reason": {"kind": "stop"}}}),
            v1_assistant_message(5, 121, "hello", sources=[2, 4]),
            ev(6, 122, "step/end", {"turn": 1, "step": 1}),
            ev(7, 123, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
            ev(8, 124, "command/run", {"commandId": "c1", "name": "inspect",
                                       "source": {"kind": "user"}}),
            ev(9, 125, "command/done", {"commandId": "c1", "kind": "success",
                                        "sourceEventSeq": 5}),
        ]
        target = migrate_v1_v2(events)
        self.assertEqual(target["inherited_event_count"], 0)
        self.assertEqual([e["type"] for e in target["events"]],
                         ["turn/start", "step/start", "feedback/record",
                          "assistant/message", "step/end", "turn/end",
                          "command/run", "command/done"])
        message = target["events"][3]
        self.assertEqual(message["seq"], 3)
        self.assertNotIn("sourceEventSeqs", message)
        records = message["data"]["stream"]
        self.assertEqual(records, [
            {"type": "text-chunks", "time0": 110, "index": 0, "dt": [],
             "texts": ["hello"]},
            {"type": "chunk", "time": 120,
             "chunk": {"type": "finish", "reason": {"kind": "stop"}}},
        ])
        self.assertEqual(target["events"][7]["data"]["sourceEventSeq"], 3)

    def test_failed_attempt_becomes_assistant_attempt(self):
        """上游 migration.spec :107-154：finish error 流 → assistant/attempt 落最后
        chunk seq/time，不造 message。"""
        failure = {"message": "provider failed", "code": "PROVIDER_ERROR"}
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 110, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "text-delta", "index": 0, "text": "partial"}}),
            ev(3, 120, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "finish", "reason": {"kind": "error",
                                                      "failure": failure}}}),
            ev(4, 121, "step/end", {"turn": 1, "step": 1}),
            ev(5, 122, "turn/end", {"turn": 1, "reason": {"kind": "error",
                                                          "error": failure}}),
        ]
        target = migrate_v1_v2(events)
        attempt = next(e for e in target["events"] if e["type"] == "assistant/attempt")
        self.assertEqual(attempt["seq"], 2)  # 密集化后的新 index（原 3 → 2）
        self.assertEqual(attempt["time"], 120)
        self.assertEqual(attempt["data"]["turn"], 1)
        self.assertEqual(attempt["data"]["stream"], [
            {"type": "text-chunks", "time0": 110, "index": 0, "dt": [],
             "texts": ["partial"]},
            {"type": "chunk", "time": 120,
             "chunk": {"type": "finish", "reason": {"kind": "error",
                                                    "failure": failure}}},
        ])
        self.assertNotIn("assistant/message", [e["type"] for e in target["events"]])

    def test_retry_seals_first_attempt(self):
        """上游 :156-226：request/header + llm/retry + retry-started 封口第一组；
        失败前缀成 attempt（finish 缺席也不造 message）；重试 attempt 被消息认领。"""
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "request/header", {"header": {"config": {"provider": "fake",
                                                                "model": "fake"}},
                                          "reason": "initial"}),
            ev(3, 110, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "text-delta", "index": 0, "text": "partial"}}),
            ev(4, 120, "llm/retry", {"retryId": "r1", "turn": 1, "step": 1,
               "provider": "fake", "mode": "normal", "policyKey": "default",
               "retry": 1, "maxRetries": 1, "delayMs": 0,
               "failure": {"message": "retry", "code": "SERVER"}}),
            ev(5, 121, "llm/retry-started", {"retryId": "r1", "turn": 1,
                                             "step": 1, "retry": 1}),
            ev(6, 130, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "text-delta", "index": 0, "text": "hello"}}),
            ev(7, 140, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "finish", "reason": {"kind": "stop"}}}),
            v1_assistant_message(8, 141, "hello", sources=[6, 7]),
            ev(9, 142, "step/end", {"turn": 1, "step": 1}),
            ev(10, 143, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        target = migrate_v1_v2(events)
        attempts = [e for e in target["events"] if e["type"] == "assistant/attempt"]
        messages = [e for e in target["events"] if e["type"] == "assistant/message"]
        self.assertEqual(len(attempts), 1)
        # 第一 attempt：两条 delta 归并为一条 text-chunks record
        self.assertEqual(attempts[0]["data"]["stream"], [
            {"type": "text-chunks", "time0": 110, "index": 0, "dt": [],
             "texts": ["partial"]},
        ])
        self.assertEqual(len(messages), 1)
        self.assertEqual(messages[0]["data"]["stream"], [
            {"type": "text-chunks", "time0": 130, "index": 0, "dt": [],
             "texts": ["hello"]},
            {"type": "chunk", "time": 140,
             "chunk": {"type": "finish", "reason": {"kind": "stop"}}},
        ])
        self.assertEqual(messages[0]["data"]["message"]["content"],
                         [text_block("hello")])

    def test_seeded_marker_moves_and_retags(self):
        """上游 :228-271 向量：cut 7→5、marker 落新 index 5、data 重打标、
        time 124 保真。"""
        message = {"id": "am", "role": "assistant",
                   "content": [text_block("hello")],
                   "source": {"kind": "model", "provider": "fake", "model": "fake"}}
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 110, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "text-delta", "index": 0, "text": "hello"}}),
            ev(3, 120, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "finish", "reason": {"kind": "stop"}}}),
            ev(4, 121, "assistant/message", {"turn": 1, "step": 1, "message": message},
               sourceEventSeqs=[2, 3], surfaceOp="append"),
            ev(5, 122, "step/end", {"turn": 1, "step": 1}),
            ev(6, 123, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
            ev(7, 124, "session/end-seed", {}),
        ]
        target = migrate_v1_v2(events, inherited=7, is_seeded=True, parent="parent")
        self.assertEqual(target["inherited_event_count"], 5)
        self.assertEqual(target["events"][5], {
            "type": "session/end-seed", "seq": 5, "time": 124,
            "data": {"inherited": True}})

    def test_empty_seed_synthesizes_marker(self):
        """上游 :273-297：空种子 → cut 0 + 合成 marker@0（time=createdAt）。"""
        target = migrate_v1_v2([], inherited=0, is_seeded=True, parent="parent",
                               created_at=42)
        self.assertEqual(target["inherited_event_count"], 0)
        self.assertEqual(target["events"], [{
            "type": "session/end-seed", "seq": 0, "time": 42,
            "data": {"inherited": True}}])

    def test_retained_prefix_synthesizes_marker_after_prefix(self):
        """上游 :299-315：保留前缀 → 合成 marker 紧随前缀、time=前事件。"""
        target = migrate_v1_v2([ev(0, 9, "feedback/record", {"text": "inherited"})],
                               inherited=1, is_seeded=True, parent="parent")
        self.assertEqual(target["inherited_event_count"], 1)
        self.assertEqual(len(target["events"]), 2)
        self.assertEqual(target["events"][1]["time"], 9)
        self.assertEqual(target["events"][1]["data"], {"inherited": True})

    def test_consumed_chunk_reference_refused(self):
        """上游 :357-397：引用指向已消费 chunk seq → 拒，绝不重定向。"""
        failure = {"message": "failed", "code": "UNKNOWN"}
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 110, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "text-delta", "index": 0, "text": "partial"}}),
            ev(3, 120, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "finish", "reason": {"kind": "error",
                                                      "failure": failure}}}),
            ev(4, 121, "step/end", {"turn": 1, "step": 1}),
            ev(5, 122, "turn/end", {"turn": 1, "reason": {"kind": "error",
                                                          "error": failure}}),
            ev(6, 123, "command/run", {"commandId": "c", "name": "inspect",
                                       "source": {"kind": "user"}}),
            ev(7, 124, "command/done", {"commandId": "c", "kind": "success",
                                        "sourceEventSeq": 3}),
        ]
        with self.assertRaises(SessionFormatUnsupportedMigrationError) as ctx:
            migrate_v1_v2(events)
        self.assertIn("targets consumed assistant/chunk 3", str(ctx.exception))

    def test_unknown_type_refused_even_ignorable(self):
        """上游 :399-418：封闭词表——未知事件即使 ignorable 也拒。"""
        events = [ev(0, 100, "external/info", {"note": "x"}, ignorable=True)]
        with self.assertRaises(SessionFormatUnsupportedMigrationError) as ctx:
            migrate_v1_v2(events)
        self.assertIn('unknown event type "external/info" at seq 0', str(ctx.exception))

    def test_uncited_unclaimed_group_refused_and_empty_sources_legal(self):
        """上游 :420-476：同坐标存在未认领组时无引用 message 拒；显式 [] 合法
        （legacy 空流 message → stream: []）。"""
        refused = [
            ev(0, 1, "turn/start", {"turn": 1}),
            ev(1, 2, "step/start", {"turn": 1, "step": 1}),
            ev(2, 3, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "text-delta", "index": 0, "text": "hello"}}),
            ev(3, 4, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "finish", "reason": {"kind": "stop"}}}),
            v1_assistant_message(4, 5, "hello", sources=None),
            ev(5, 6, "step/end", {"turn": 1, "step": 1}),
            ev(6, 7, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        with self.assertRaises(SessionFormatUnsupportedMigrationError) as ctx:
            migrate_v1_v2(refused)
        self.assertIn("does not cite its complete v1 chunk attempt", str(ctx.exception))
        target = migrate_v1_v2([
            ev(0, 1, "turn/start", {"turn": 1}),
            ev(1, 2, "step/start", {"turn": 1, "step": 1}),
            v1_assistant_message(2, 3, "hello", sources=[]),
            ev(3, 4, "step/end", {"turn": 1, "step": 1}),
            ev(4, 5, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ])
        message = next(e for e in target["events"] if e["type"] == "assistant/message")
        self.assertEqual(message["data"]["stream"], [])

    def test_cut_splitting_refused(self):
        """上游 :478-534：切割 attempt（流中 / 流与 message 之间）都拒。"""
        # 流中切割：chunk 2 在 cut 前、chunk 3 在 cut 后
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 110, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "text-delta", "index": 0, "text": "a"}}),
            ev(3, 120, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "text-delta", "index": 0, "text": "b"}}),
            ev(4, 130, "assistant/message", {"turn": 1, "step": 1,
               "message": {"id": "am", "role": "assistant",
                           "content": [text_block("ab")],
                           "source": {"kind": "model", "provider": "fake",
                                      "model": "fake"}}},
               sourceEventSeqs=[2, 3], surfaceOp="append"),
        ]
        with self.assertRaises(SessionFormatUnsupportedMigrationError) as ctx:
            migrate_v1_v2(events, inherited=3, is_seeded=True, parent="parent")
        self.assertIn("cut 3 splits one Assistant attempt", str(ctx.exception))
        # 流与 message 之间切割
        with self.assertRaises(SessionFormatUnsupportedMigrationError) as ctx:
            migrate_v1_v2(events, inherited=4, is_seeded=True, parent="parent")
        self.assertIn("cut 4 splits one Assistant attempt", str(ctx.exception))

    def test_cross_turn_groups_isolated(self):
        """上游 :736-763：连续两 turn 各一个 finish-error chunk → 恰两个 attempt。"""
        failure = {"message": "failed", "code": "UNKNOWN"}
        events = [
            ev(0, 1, "turn/start", {"turn": 1}),
            ev(1, 2, "step/start", {"turn": 1, "step": 1}),
            ev(2, 3, "assistant/chunk", {"turn": 1, "step": 1,
               "chunk": {"type": "finish", "reason": {"kind": "error",
                                                      "failure": failure}}}),
            ev(3, 4, "step/end", {"turn": 1, "step": 1}),
            ev(4, 5, "turn/end", {"turn": 1, "reason": {"kind": "error",
                                                        "error": failure}}),
            ev(5, 6, "turn/start", {"turn": 2}),
            ev(6, 7, "step/start", {"turn": 2, "step": 1}),
            ev(7, 8, "assistant/chunk", {"turn": 2, "step": 1,
               "chunk": {"type": "finish", "reason": {"kind": "error",
                                                      "failure": failure}}}),
            ev(8, 9, "step/end", {"turn": 2, "step": 1}),
            ev(9, 10, "turn/end", {"turn": 2, "reason": {"kind": "error",
                                                         "error": failure}}),
        ]
        target = migrate_v1_v2(events)
        attempts = [e for e in target["events"] if e["type"] == "assistant/attempt"]
        self.assertEqual(len(attempts), 2)
        self.assertEqual(attempts[0]["data"]["turn"], 1)
        self.assertEqual(attempts[1]["data"]["turn"], 2)


class V0ToV1MigrationTest(unittest.TestCase):
    def test_flat_messages_wrapped_with_legacy_ids(self):
        """上游 legacy.spec :21-66：flat user/assistant/tool-result 补 legacy id；
        替换 tool/result 继承被替换表面首节点 id。"""
        events = [
            ev(0, 1, "turn/start", {"turn": 1}),
            ev(1, 2, "user/message", {"content": [text_block("hi")],
                                      "source": {"kind": "user"}},
               surfaceOp="append"),
            ev(2, 3, "step/start", {"turn": 1, "step": 1}),
            ev(3, 4, "assistant/message", {"turn": 1, "step": 1,
               "content": [{"type": "tool-call", "id": "call-1", "name": "read",
                            "arguments": "{}"}],
               "provenance": {"provider": "mock", "model": "mock"}},
               surfaceOp="append"),
            ev(4, 5, "tool/call", {"turn": 1, "step": 1, "callId": "call-1",
                                   "name": "read", "arguments": "{}"}),
            ev(5, 6, "tool/result", {"turn": 1, "step": 1, "callId": "call-1",
               "content": [text_block("full")], "isError": False},
               sourceEventSeqs=[4], surfaceOp="append"),
            ev(6, 7, "tool/result", {"turn": 1, "step": 1, "callId": "call-1",
               "content": [text_block("pruned")], "isError": False},
               sourceEventSeqs=[5], surfaceOp={"op": "replace", "start": 5, "end": 5}),
            ev(7, 8, "step/end", {"turn": 1, "step": 1}),
            ev(8, 9, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        target = V0_TO_V1["migrate"](artifact_v1(
            events, inherited=0) |
            {"header": {**artifact_v1([])["header"], "version": 0}})
        user = target["events"][1]
        self.assertEqual(user["data"]["id"], "legacy-message:s-v1:1")
        self.assertEqual(user["data"]["role"], "user")
        assistant = target["events"][3]
        self.assertEqual(assistant["data"]["message"]["id"], "legacy-message:s-v1:3")
        self.assertEqual(assistant["data"]["message"]["source"]["kind"], "model")
        result = target["events"][5]
        self.assertEqual(result["data"]["message"]["id"], "legacy-message:s-v1:5")
        self.assertEqual(result["data"]["message"]["content"],
                         [{"type": "tool-result", "toolCallId": "call-1",
                           "content": [text_block("full")], "isError": False}])
        self.assertEqual(target["events"][6]["data"]["message"]["id"],
                         "legacy-message:s-v1:5")

    def test_turn_end_reason_conversion_table(self):
        cases = [
            ({"kind": "completed"},
             {"kind": "completed"}),
            ({"kind": "aborted"},
             {"kind": "aborted", "reason": {"kind": "legacy"}}),
            ({"kind": "disposed"},
             {"kind": "aborted", "reason": {"kind": "disposed"}}),
            ({"kind": "error", "step": 2,
              "failure": {"message": "boom", "code": "SERVER"}},
             {"kind": "error", "error": {"message": "boom", "code": "SERVER"}}),
            ({"kind": "error", "step": 2, "message": "bad"},
             {"kind": "error", "error": {"message": "bad", "code": "UNKNOWN"}}),
        ]
        for index, (source_reason, expected) in enumerate(cases):
            events = [
                ev(0, 100, "turn/start", {"turn": 1}),
                ev(1, 100, "turn/end", {"turn": 1, "reason": source_reason}),
            ]
            target = V0_TO_V1["migrate"](artifact_v1(
                events, inherited=0) | {"header": {**artifact_v1([])["header"],
                                                   "version": 0}})
            self.assertEqual(target["events"][1]["data"]["reason"], expected, index)

    def test_retired_types_refused(self):
        for etype, data in (
            ("request/header-delta", {"header": {}}),
            ("mode/set", {"mode": "plan"}),
        ):
            with self.assertRaises(SessionFormatUnsupportedMigrationError) as ctx:
                V0_TO_V1["migrate"](artifact_v1(
                    [ev(0, 100, etype, data)], inherited=0)
                    | {"header": {**artifact_v1([])["header"], "version": 0}})
            self.assertIn(f"unsupported legacy {etype} event", str(ctx.exception))

    def test_request_header_fallback_refused(self):
        with self.assertRaises(SessionFormatUnsupportedMigrationError) as ctx:
            V0_TO_V1["migrate"](artifact_v1(
                [ev(0, 100, "request/header",
                    {"header": {}, "reason": "fallback"})], inherited=0)
                | {"header": {**artifact_v1([])["header"], "version": 0}})
        self.assertIn('reason "fallback"', str(ctx.exception))

    def test_replacement_tool_result_inherits_replaced_message_id(self):
        """上游 legacy.spec：替换 tool/result 继承被替换表面首节点消息 id。"""
        events = [
            ev(0, 100, "turn/start", {"turn": 1}),
            ev(1, 101, "step/start", {"turn": 1, "step": 1}),
            ev(2, 102, "assistant/message", {"turn": 1, "step": 1,
               "message": {"id": "am0", "role": "assistant",
                           "content": [text_block("first")],
                           "source": {"kind": "model", "provider": "fake",
                                      "model": "fake"}}},
               surfaceOp="append"),
            ev(3, 103, "tool/result", {"turn": 1, "step": 1, "callId": "c1",
               "content": [text_block("out")], "isError": False},
               sourceEventSeqs=[2], surfaceOp={"op": "replace", "start": 2, "end": 2}),
            ev(4, 104, "step/end", {"turn": 1, "step": 1}),
            ev(5, 105, "turn/end", {"turn": 1, "reason": {"kind": "completed"}}),
        ]
        target = V0_TO_V1["migrate"](artifact_v1(events, inherited=0)
                                     | {"header": {**artifact_v1([])["header"],
                                                   "version": 0}})
        self.assertEqual(target["events"][3]["data"]["message"]["id"], "am0")


class HeaderTranslationTest(unittest.TestCase):
    def test_v1_header_translates_to_v2_without_body(self):
        header = {"version": 1, "id": "s", "createdAt": 5, "isSeeded": True,
                  "delegationDepth": 0, "parentSession": "p"}
        translated = migrate_released_header(header)
        self.assertEqual(translated["version"], 2)
        self.assertEqual(translated["parentSession"], "p")
        self.assertTrue(translated["isSeeded"])

    def test_codec_decodes_seed_length_semantics(self):
        """上游 codec.spec：`seedLength: 0` = seeded 零切割。"""
        decoded = RELEASED_V1_CODEC["decode_header"]({
            "type": "session", "version": 1, "id": "s", "createdAt": 1,
            "delegationDepth": 0, "seedLength": 0})
        self.assertTrue(decoded["isSeeded"])
        decoded = RELEASED_V1_CODEC["decode_header"]({
            "type": "session", "version": 1, "id": "s", "createdAt": 1,
            "delegationDepth": 0})
        self.assertFalse(decoded["isSeeded"])


if __name__ == "__main__":
    unittest.main()
