# -*- coding: utf-8 -*-
"""zstd 拼接帧容器 + 一行一事件 JSONL 持久化验收（V2 载体）。

对齐上游：
- packages/session/session-persistence-jsonl/src/zstd.spec.ts / zstd.compat.spec.ts
  （帧容器：拼接帧、截断前缀恢复、结构拒绝）
- format.ts 目录布局与响亮拒绝（encodingMismatch / legacyLayout / 版本拒读）
- V2 载体：一行一事件；chunk 行与 StorageRecord 打包层随 assistant/chunk 一并
  废止（流内嵌 assistant/message；上游仅 v0→v1 迁移 codec 保留打包，mini 从未有过）

运行：python -m unittest tests.test_persistence_zstd
"""
import io
import json
import tempfile
import unittest
from pathlib import Path

import zstandard

from miniharness.core.session import Session
from miniharness.core.session.persistence import (
    SESSION_FORMAT_VERSION,
    JsonlPersistence,
    SessionFormatUnsupportedError,
    _log_path,
    _to_header_line,
    balanced_after_replay,
    decode_segment,
    encode_segment,
    inherited_cut,
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


def _user_msg(i, text=None):
    """V2 surface 事件（assistant/chunk 已废止）：事件载体用 user/message。"""
    return {
        "type": "user/message",
        "seq": i,
        "time": 1000 + i,
        "data": {"content": [{"type": "text", "text": text if text is not None else f"t{i}"}]},
        "surfaceOp": "append",
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


class TestJsonlCarrierLayout(unittest.TestCase):
    def test_default_compression_is_zstd_with_frame_header(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            self.assertEqual(p.compression, "zstd")
            p.append("s1", _plain(0))
            p.flush()
            path = p.path_of("s1")
            # v2 generation 制品名（上游 sessionFormatLogFilename：vN 段）
            self.assertEqual(path.name, "session.v2.jsonl.zstd")
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
            self.assertEqual(path.name, "session.v2.jsonl")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(json.loads(lines[0])["type"], "session")

    def test_roundtrip_through_load_equals_original(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            events = [_user_msg(i) for i in range(7)] + [_plain(7)]
            p = JsonlPersistence(root / "z")
            for e in events:
                p.append("s1", e)
            p.flush()
            self.assertEqual(p.load("s1"), events)

    def test_seq_gap_rejected_on_load(self):
        """读路径 seq 连续性校验：提交区断档响亮拒绝（fail-closed）。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.append("s1", {"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}})
            p.append("s1", {"type": "user/message", "seq": 1, "time": 2,
                            "data": {}, "surfaceOp": "append"})
            p.append("s1", {"type": "turn/end", "seq": 3, "time": 3,
                            "data": {"turn": 1, "reason": {"kind": "stop"}}})
            p.flush()
            with self.assertRaises(ValueError) as ctx:
                p.load("s1")
            self.assertIn("seq gap in committed region at line 3", str(ctx.exception))

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
            # 同一根内伪造第二个项目目录里的同 id 头帧（v2 generation 制品名）
            other = root / project_key("/w2") / encode_segment("dup")
            other.mkdir(parents=True)
            header = {"type": "session", "version": SESSION_FORMAT_VERSION, "id": "dup",
                      "createdAt": 1, "isSeeded": False, "delegationDepth": 0, "cwd": "/w2"}
            (other / "session.v2.jsonl.zstd").write_bytes(
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
            (99, "but this harness reads only v2"),
            (-1, "older than the supported v2"),
        ):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp) / "v"
                root.mkdir(parents=True)
                d = root / project_key(str(tmp)) / encode_segment("s1")
                d.mkdir(parents=True)
                header = {"type": "session", "version": version, "id": "s1",
                          "createdAt": 1, "isSeeded": False, "delegationDepth": 0,
                          "cwd": str(tmp)}
                (d / "session.v2.jsonl.zstd").write_bytes(
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
                p.append("s1", _user_msg(i))
            p.flush()
            for i in range(6, 12):
                p.append("s1", _user_msg(i))
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
            self.assertEqual(back[:6], [_user_msg(i) for i in range(6)])


class TestDirectoryLayout(unittest.TestCase):
    def test_encode_segment_escapes(self):
        self.assertEqual(encode_segment("."), "~002E")
        self.assertEqual(encode_segment(".."), "~002E~002E")
        self.assertEqual(encode_segment("~"), "~007E")
        self.assertEqual(decode_segment("~002E~002E"), "..")
        self.assertEqual(decode_segment(encode_segment("a b/c:d")), "a b/c:d")

    def test_v2_generation_artifact_name(self):
        """generation 制品名版本化（上游 sessionFormatLogFilename）：v2 带 vN 段。"""
        self.assertEqual(_log_path(Path("r"), None, "s", "zstd").name,
                         "session.v2.jsonl.zstd")
        self.assertEqual(_log_path(Path("r"), None, "s", "none").name,
                         "session.v2.jsonl")

    def test_header_key_closure_write_rejects_unknown(self):
        """物理 header 键闭集（上游 format.ts HEADER_KEYS）：未知键写侧 fail loud。"""
        with self.assertRaises(ValueError) as ctx:
            _to_header_line({"version": 2, "id": "s", "createdAt": 1, "label": "x"})
        self.assertIn("closed physical header key set", str(ctx.exception))
        self.assertIn("['label']", str(ctx.exception))

    def test_header_key_closure_read_rejects_unknown_and_missing_required(self):
        """读守卫：未知键 / 缺必填键 / isSeeded 非 boolean / cwd 相对路径 → 非 header。"""
        from miniharness.core.session.persistence import _is_header_line
        base = {"type": "session", "version": 2, "id": "s", "createdAt": 1,
                "isSeeded": False, "delegationDepth": 0}
        self.assertTrue(_is_header_line(base))
        self.assertTrue(_is_header_line({**base, "cwd": "/abs/path", "origin": "subagent"}))
        # 未知键拒读（扩展 meta 键上行会让上游拒读本制品）
        self.assertFalse(_is_header_line({**base, "label": "x"}))
        self.assertFalse(_is_header_line({**base, "seedLength": 3}))
        # 缺必填键
        for key in ("type", "version", "id", "createdAt", "isSeeded", "delegationDepth"):
            broken = {k: v for k, v in base.items() if k != key}
            self.assertFalse(_is_header_line(broken), key)
        # isSeeded 严格 boolean
        self.assertFalse(_is_header_line({**base, "isSeeded": "true"}))
        self.assertFalse(_is_header_line({**base, "isSeeded": 1}))
        # cwd 绝对路径
        self.assertFalse(_is_header_line({**base, "cwd": "relative/path"}))
        self.assertFalse(_is_header_line({**base, "cwd": 5}))
        self.assertFalse(_is_header_line({**base, "origin": "other"}))

    def test_inherited_cut_derives_from_marker_with_corrupt_checks(self):
        """cut 由最后一个 `{inherited:true}` marker 派生（上游 format.ts inheritedCut），
        seeded 无 marker / unseeded 有 marker 双向 corrupt。"""
        unseeded = {"isSeeded": False}
        seeded = {"isSeeded": True}
        normal_seed = [{"type": "session/end-seed", "seq": 0, "data": {}}]
        self.assertEqual(inherited_cut(unseeded, normal_seed), 0)
        forked = [
            {"type": "user/message", "seq": 0, "data": {}, "surfaceOp": "append"},
            {"type": "session/end-seed", "seq": 2, "data": {"inherited": True}},
            {"type": "session/end-seed", "seq": 3, "data": {}},
        ]
        self.assertEqual(inherited_cut(seeded, forked), 2)
        # seeded header 缺 marker → corrupt
        with self.assertRaises(ValueError) as ctx:
            inherited_cut(seeded, normal_seed)
        self.assertIn("seeded v2 header lacks an inherited end-seed marker", str(ctx.exception))
        # unseeded header 带 inherited marker → corrupt
        with self.assertRaises(ValueError) as ctx:
            inherited_cut(unseeded, forked)
        self.assertIn("unseeded v2 header contains an inherited end-seed marker",
                      str(ctx.exception))

    def test_fork_child_disk_roundtrip_recovers_cut_from_marker(self):
        """fork 子会话落盘重载：cut 不随 header 携带，由 marker 派生——
        own_events 恰为 marker 之后的自有事件。"""
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            inherited = [_user_msg(i) for i in range(3)]
            p.declare("child-1", {"parentSession": "p", "origin": "subagent",
                                  "isSeeded": True, "delegationDepth": 1})
            for event in inherited:
                p.append("child-1", event)
            p.append("child-1", {"type": "session/end-seed", "seq": 3, "time": 9,
                                 "data": {"inherited": True}})
            p.append("child-1", {"type": "subagent/descriptor", "seq": 4, "time": 10,
                                 "data": {"version": 3, "mode": "continuable",
                                          "provider": "in-process", "label": "研"}})
            p.flush()
            resumed = repair_and_replay(p, "child-1", Session("child-1"))
            self.assertTrue(resumed.is_seeded)
            self.assertEqual(resumed.inherited_event_count, 3)
            own = resumed.own_events()
            # own = [继承切割 marker(inherited:true), descriptor] + restore 边界
            # {} marker（上游 restore：seed 尾非 end-seed → 补记）
            self.assertEqual([e["type"] for e in own],
                             ["session/end-seed", "subagent/descriptor", "session/end-seed"])
            self.assertIs(own[0]["data"].get("inherited"), True)
            self.assertEqual(own[1]["data"]["label"], "研")
            self.assertEqual(own[2]["data"], {})

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


def _stream_compress_frame(payload: bytes) -> bytes:
    """上游写入侧等价物：流式压缩，帧头**不带**内容大小字段。"""
    buffer = io.BytesIO()
    with zstandard.ZstdCompressor(write_checksum=True).stream_writer(
        buffer, closefd=False
    ) as writer:
        writer.write(payload)
    return buffer.getvalue()


class TestUpstreamInterop(unittest.TestCase):
    """跨实现互读回归：上游 Node 栈（流式压缩帧 + 有损 projectKey）产出的
    真实工件 mini 必须可读——两条路径都曾真实翻车。"""

    def test_stream_compressed_frames_decode(self):
        """无内容大小字段的帧头必须可解（one-shot API 会拒收）。"""
        payload = b'{"type":"session","version":2}\n{"type":"turn/start"}\n'
        frame = _stream_compress_frame(payload)
        with self.assertRaises(zstandard.ZstdError):
            zstandard.ZstdDecompressor().decompress(frame)  # one-shot 拒收，证明前提成立
        self.assertEqual(decompress_zstd_frame(frame), payload)
        scan = scan_zstd_frames(frame)
        self.assertEqual(b"".join(decode_frames(frame, scan.frames)), payload)
        chunks = iter([frame[:5], frame[5:]])
        self.assertEqual(read_first_frame(lambda: next(chunks)), payload)

    def test_upstream_style_artifact_roundtrip(self):
        """逐记录流式压缩拼接的工件（上游默认形态）可被 load/read_raw 读回。"""
        cwd = r"C:\Users\ZHANGZ~1\proj dir"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            header_line = json.dumps(
                {"type": "session", "version": SESSION_FORMAT_VERSION, "id": "up-1",
                 "createdAt": 1234, "cwd": cwd, "isSeeded": False, "delegationDepth": 0},
                separators=(",", ":"),
            ) + "\n"
            events = [_plain(i) for i in range(3)]
            body = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in events)
            artifact = (
                _stream_compress_frame(header_line.encode("utf-8"))
                + b"".join(
                    _stream_compress_frame(line.encode("utf-8"))
                    for line in body.splitlines(keepends=True)
                )
            )
            path = _log_path(root, cwd, "up-1", "zstd")
            path.parent.mkdir(parents=True)
            path.write_bytes(artifact)

            p = JsonlPersistence(root)
            self.assertEqual(p.load("up-1"), events)
            self.assertEqual(p.read_raw("up-1"), header_line + body)
            headers = p.list_headers()
            self.assertEqual([h["id"] for h in headers], ["up-1"])
            self.assertEqual(headers[0]["meta"]["cwd"], cwd)

    def test_scan_then_cached_reads_on_same_instance(self):
        """扫描定位后缓存真实路径：projectKey 有损反解码不得污染后续解析。

        曾有 bug：_find 把反解码 projectKey 当 cwd 记忆，第二次 resolve 用它
        重算出不存在的路径——首次成功、后续全挂。
        """
        cwd = r"C:\Users\ZHANGZ~1\tmp dir\x"  # 折叠后不可逆（~1→~007E、\\→-）
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            w = JsonlPersistence(root)
            w.declare("s-lossy", {"cwd": cwd}, created_at=7)
            for i in range(2):
                w.append("s-lossy", _plain(i))
            w.flush()

            q = JsonlPersistence(root)  # 冷实例：首次走扫描定位
            self.assertEqual(len(q.load("s-lossy")), 2)
            # 同一实例上的后续读取全部命中已定位缓存
            meta = q.inspect("s-lossy")["meta"]
            self.assertEqual(meta["cwd"], cwd)
            self.assertIsNotNone(q.read_raw("s-lossy"))
            self.assertEqual(q.path_of("s-lossy"), q.path_of("s-lossy"))
            self.assertTrue(str(q.path_of("s-lossy")).endswith("session.v2.jsonl.zstd"))


if __name__ == "__main__":
    unittest.main()
