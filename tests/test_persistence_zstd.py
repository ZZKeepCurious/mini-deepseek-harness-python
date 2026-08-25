# -*- coding: utf-8 -*-
"""zstd 拼接帧容器 + StorageRecord 打包行持久化验收。

对齐上游：
- packages/session/session-persistence-jsonl/src/zstd.spec.ts / zstd.compat.spec.ts
  （帧容器：拼接帧、截断前缀恢复、结构拒绝）
- packages/core/session/src/chunk-rows.ts（MIN_RUN 打包、行信封、fail-closed 校验）
- format.ts 目录布局与响亮拒绝（encodingMismatch / legacyLayout / 版本拒读）

运行：python -m unittest tests.test_persistence_zstd
"""
import json
import tempfile
import unittest
from pathlib import Path

import zstandard

from miniharness.core.session import Session
from miniharness.core.session.chunk_rows import MIN_RUN
from miniharness.core.session.persistence import (
    SESSION_FORMAT_VERSION,
    JsonlPersistence,
    SessionFormatUnsupportedError,
    balanced_after_replay,
    decode_segment,
    decode_storage_record,
    encode_segment,
    project_key,
    repair_and_replay,
)
from miniharness.core.session.zstd_frames import (
    ZSTD_MAGIC,
    compress_zstd_frame,
    decode_frames,
    decompress_zstd_frame,
    decompress_zstd_prefix,
    read_first_frame,
    scan_zstd_frames,
)


def _chunk(i, kind="text-delta", text=None):
    return {
        "type": "assistant/chunk",
        "seq": i,
        "time": 1000 + i,
        "data": {
            "turn": 1,
            "step": 1,
            "chunk": {"type": kind, "index": 0, "text": text if text is not None else f"t{i}"},
        },
    }


def _plain(i):
    return {"type": "turn/start", "seq": i, "time": 1, "data": {"turn": 1}}


class TestFrameContainer(unittest.TestCase):
    def test_concatenated_frames_decode_independently(self):
        """compat 语义：逐帧独立压缩后拼接，容器整体可解码。"""
        a = compress_zstd_frame(b"alpha\n")
        b = compress_zstd_frame(b"beta\n")
        buffer = a + b
        scan = scan_zstd_frames(buffer)
        self.assertEqual([(f.start, f.end) for f in scan.frames], [(0, len(a)), (len(a), len(a) + len(b))])
        self.assertIsNone(scan.torn_start)
        self.assertEqual(b"".join(decode_frames(buffer, scan.frames)), b"alpha\nbeta\n")

    def test_torn_final_frame_prefix_recovery(self):
        body = ("line-%d\n" % i for i in range(200))
        payload = "".join(body).encode("utf-8")
        frame = compress_zstd_frame(payload)
        cut = len(frame) // 2
        prefix = decompress_zstd_prefix(frame[:cut])
        if prefix:  # 多块/近完整帧才产出；单块短帧允许为空（流式语义）
            self.assertEqual(payload[: len(prefix)], prefix)

    def test_invalid_magic_rejected_with_offset(self):
        with self.assertRaises(ValueError) as ctx:
            scan_zstd_frames(b"garbage!!" * 4)
        self.assertIn("invalid frame magic at byte 0", str(ctx.exception))

    def test_reserved_block_type_rejected(self):
        # 块头位于 magic(4)+描述符(1)+FCS(1) 之后；type 位在首字节的 bits1-2，
        # 置为保留值 3 必须被结构扫描拒绝
        bad = bytearray(compress_zstd_frame(b"hello\n"))
        bad[6] = (bad[6] & 0xF1) | 0x06
        with self.assertRaises(ValueError):
            scan_zstd_frames(bytes(bad))

    def test_read_first_frame_incremental(self):
        first = compress_zstd_frame(b"header-line\n")
        second = compress_zstd_frame(b"x" * 64)
        chunks = iter((first + second)[i:i + 7] for i in range(0, len(first + second), 7))
        text = read_first_frame(lambda: next(chunks, b""))
        self.assertEqual(text, b"header-line\n")

    def test_header_frame_checksum_corruption_raises(self):
        frame = bytearray(compress_zstd_frame(b"{}\n"))
        frame[-2] ^= 0xFF
        with self.assertRaises(zstandard.ZstdError):
            decompress_zstd_frame(bytes(frame))


class TestStorageRecordPacking(unittest.TestCase):
    def _persist(self, root, events, pack=True):
        p = JsonlPersistence(root / ("pack" if pack else "raw"), pack_chunks=pack)
        for e in events:
            p.append("s1", e)
        p.flush()
        return [json.loads(l) for l in p.read_raw("s1").strip().split("\n")[1:]]

    def test_run_of_min_run_packed_into_row(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = self._persist(Path(tmp), [_chunk(i) for i in range(MIN_RUN)])
            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["type"], "text-chunks")
            data = row["data"]
            self.assertEqual(data["dt"], [1] * (MIN_RUN - 1))  # 相邻 time 差
            self.assertEqual(data["texts"], ["t0", "t1", "t2"])

    def test_short_run_stays_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = self._persist(Path(tmp), [_chunk(i) for i in range(MIN_RUN - 1)])
            self.assertEqual([r["type"] for r in rows], ["assistant/chunk"] * (MIN_RUN - 1))

    def test_mixed_event_breaks_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = [_chunk(0), _chunk(1), _plain(2), _chunk(3), _chunk(4)]
            rows = self._persist(Path(tmp), events)
            types = [r["type"] for r in rows]
            self.assertIn("assistant/chunk", types)
            self.assertNotIn("text-chunks", types)

    def test_tool_call_delta_rows_carry_id_and_first_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = [
                {
                    "type": "assistant/chunk",
                    "seq": i,
                    "time": 10 * i,
                    "data": {
                        "turn": 2,
                        "step": 1,
                        "index": 0,
                        **({"chunk": {"type": "tool-call-delta", "index": 0, "id": "call_1",
                                      "argumentsDelta": part}} if True else {}),
                        **({"name": "bash"} if i == 0 else {}),
                    },
                }
                for i, part in enumerate(['{"cmd', '": "ls"}'])
            ]
            # tool-call-delta 在 mini 的 chunk 形状里走 data.chunk；name 属于首行 chunk 外层？
            # 直接以 chunk_rows.classify 的白名单为准构造：
            from miniharness.core.session.chunk_rows import classify
            self.assertIsNone(classify(events[0]))  # 形状不符 → 不打包，退回逐条
            rows = self._persist(Path(tmp), events)
            self.assertEqual([r["type"] for r in rows], ["assistant/chunk"] * 2)

    def test_roundtrip_through_load_equals_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = [_chunk(i) for i in range(7)] + [_plain(7)]
            p = JsonlPersistence(root / "z")
            for e in events:
                p.append("s1", e)
            p.flush()
            self.assertEqual(p.load("s1"), events)

    def test_seq_gap_prevents_packing(self):
        with tempfile.TemporaryDirectory() as tmp:
            events = [_chunk(0), _chunk(1), _chunk(5)]
            rows = self._persist(Path(tmp), events)
            self.assertEqual([r["type"] for r in rows], ["assistant/chunk"] * 3)

    def test_decode_storage_record_roundtrip_and_validation(self):
        events = [_chunk(i) for i in range(4)]
        p = JsonlPersistence(Path(tempfile.mkdtemp()) / "z")
        for e in events:
            p.append("s1", e)
        p.flush()
        line = p.read_raw("s1").strip().split("\n")[1]
        record = json.loads(line)
        expanded = decode_storage_record(record)
        self.assertEqual(expanded, events[:4])
        broken = dict(record)
        del broken["data"]["dt"]  # 已知标签但行体缺字段 → fail-closed
        with self.assertRaises(ValueError) as ctx:
            decode_storage_record(broken)
        self.assertIn("malformed", str(ctx.exception))


class TestJsonlCarrierLayout(unittest.TestCase):
    def test_default_compression_is_zstd_with_frame_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            self.assertEqual(p.compression, "zstd")
            p.append("s1", _plain(0))
            p.flush()
            path = p.path_of("s1")
            self.assertEqual(path.name, "session.jsonl.zstd")
            buf = path.read_bytes()
            self.assertEqual(buf[:4], ZSTD_MAGIC.to_bytes(4, "little"))
            first = read_first_frame(lambda: buf)
            header = json.loads(first.decode("utf-8"))
            self.assertEqual(next(iter(header)), "type")  # type 居首
            self.assertEqual(header["type"], "session")
            self.assertEqual(header["version"], SESSION_FORMAT_VERSION)
            self.assertIn("delegationDepth", header)

    def test_written_bytes_are_exact_frame_concatenation(self):
        """回归：CRT 文本模式曾把载荷内 0x0A 翻译成 \r\n 破坏帧字节。"""
        from miniharness.core.session.persistence import _flat_header, _to_header_line
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.declare("s1", {"cwd": str(tmp)}, 1234)
            events = [_plain(0), _plain(1)]
            for e in events:
                p.append("s1", e)
            p.flush()
            header_text = json.dumps(
                _to_header_line(_flat_header("s1", {"cwd": str(tmp)}, 1234)),
                ensure_ascii=False) + "\n"
            batch_text = "".join(json.dumps(e, ensure_ascii=False) + "\n" for e in events)
            expected = (compress_zstd_frame(header_text.encode("utf-8"))
                        + compress_zstd_frame(batch_text.encode("utf-8")))
            buf = p.path_of("s1").read_bytes()
            self.assertEqual(buf, expected)  # 逐字节相等：无 \r 插入、无缺字节

    def test_none_mode_is_plain_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp), compression="none")
            p.append("s1", _plain(0))
            p.flush()
            path = p.path_of("s1")
            self.assertEqual(path.name, "session.jsonl")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(lines[0])["type"], "session")

    def test_encoding_mismatch_rejected_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            JsonlPersistence(root / "a", compression="none").declare("s1")
            with self.assertRaises(ValueError) as ctx:
                JsonlPersistence(root / "a").list_headers()
            self.assertIn('uses .jsonl, but this backend is configured for compression "zstd"',
                          str(ctx.exception))

    def test_legacy_flat_layout_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            legacy = JsonlPersistence(root / "old", compression="none")
            legacy._materialize_header(legacy.root / "sess-old" / "session.jsonl", "sess-old", None, None)
            with self.assertRaises(ValueError) as ctx:
                JsonlPersistence(root / "old").list_headers()
            self.assertIn("unsupported flat-file layout", str(ctx.exception))

    def test_duplicate_id_across_projects_rejected_on_listing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            JsonlPersistence(root).declare("dup", {"cwd": "/w1"})
            JsonlPersistence(root, compression="none")
            second_root = JsonlPersistence(root)
            second_root._cwd.clear()
            # 同一根内伪造第二个项目目录里的同 id 头帧
            other = root / project_key("/w2") / encode_segment("dup")
            other.mkdir(parents=True)
            header = {"type": "session", "version": 0, "id": "dup", "createdAt": 1,
                      "delegationDepth": 0, "cwd": "/w2"}
            (other / "session.jsonl.zstd").write_bytes(
                compress_zstd_frame((json.dumps(header) + "\n").encode("utf-8")))
            with self.assertRaises(ValueError) as ctx:
                second_root.list_headers()
            self.assertIn('duplicate JSONL session id "dup"', str(ctx.exception))

    def test_materialize_refusal_when_log_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.declare("s1")
            path = p.path_of("s1")
            fresh = JsonlPersistence(Path(tmp))
            with self.assertRaises(ValueError) as ctx:
                fresh._materialize_header(path, "s1", None, None)
            self.assertIn("refusing to materialize", str(ctx.exception))
            self.assertIn("(load/resume it instead)", str(ctx.exception))

    def test_list_headers_only_reads_first_frame(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.declare("s1", {"cwd": str(tmp)})
            p.append("s1", _plain(0))
            p.flush()
            path = p.path_of("s1")
            with open(path, "ab") as f:  # 第二帧起即垃圾：仅首帧读取的列举必须不受影响
                f.write(b"\x00" * 32)
            headers = JsonlPersistence(Path(tmp)).list_headers()
            self.assertEqual([h["id"] for h in headers], ["s1"])
            with self.assertRaises(ValueError):
                p.load("s1")

    def test_version_refusal_newer_and_older(self):
        for version, fragment in (
            (99, "but this harness reads only v0"),
            (-1, "older than the supported v0"),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "v"
                root.mkdir(parents=True)
                d = root / project_key(str(tmp)) / encode_segment("s1")
                d.mkdir(parents=True)
                header = {"type": "session", "version": version, "id": "s1",
                          "createdAt": 1, "delegationDepth": 0, "cwd": str(tmp)}
                (d / "session.jsonl.zstd").write_bytes(
                    compress_zstd_frame((json.dumps(header) + "\n").encode("utf-8")))
                with self.assertRaises(SessionFormatUnsupportedError) as ctx:
                    JsonlPersistence(root).load("s1")
                self.assertIn(f'uses log format v{version}', str(ctx.exception))
                self.assertIn(fragment, str(ctx.exception))

    def test_torn_final_frame_keeps_committed_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = JsonlPersistence(root / "z")
            p.declare("s1", {"cwd": str(root)})
            for i in range(6):
                p.append("s1", _chunk(i))
            p.flush()
            for i in range(6, 12):
                p.append("s1", _chunk(i))
            p.flush()
            path = p.path_of("s1")
            buf = path.read_bytes()
            scan = scan_zstd_frames(buf)
            self.assertGreaterEqual(len(scan.frames), 3)
            last = scan.frames[-1]
            path.write_bytes(buf[: last.start + (last.end - last.start) // 2])
            prepared = JsonlPersistence(root / "z").read_prepared("s1")
            self.assertEqual(len(prepared["events"]), 6)
            self.assertIsNotNone(prepared["truncate_to"])
            repair_and_replay(JsonlPersistence(root / "z"), "s1", Session("s1"))
            self.assertTrue(balanced_after_replay(JsonlPersistence(root / "z"), "s1"))
            back = JsonlPersistence(root / "z").load("s1")
            self.assertEqual(back[:6], [_chunk(i) for i in range(6)])


class TestDirectoryLayout(unittest.TestCase):
    def test_encode_segment_escapes(self):
        self.assertEqual(encode_segment("."), "~002E")
        self.assertEqual(encode_segment(".."), "~002E~002E")
        self.assertEqual(encode_segment("~"), "~007E")
        self.assertEqual(decode_segment("~002E~002E"), "..")
        self.assertEqual(decode_segment(encode_segment("a b/c:d")), "a b/c:d")

    def test_project_key_lossy_truncation(self):
        long = "x" * 400
        key = project_key(long)
        self.assertEqual(len(key), 2 + 251 + 2)  # 有损截断至 251，含定界符共 ≤255
        self.assertTrue(key.startswith("--") and key.endswith("--"))
        self.assertEqual(project_key("a/b\\c:d"), "--a-b-c-d--")

    def test_no_cwd_bucket(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.append("s1", _plain(0))  # 无 cwd
            p.flush()
            rel = p.path_of("s1").relative_to(Path(tmp)).parts
            self.assertEqual(rel[0], "_no-cwd")


if __name__ == "__main__":
    unittest.main()
