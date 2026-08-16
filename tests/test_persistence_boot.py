"""第 5 章验收：持久化 + 崩溃恢复 + 组合加载。运行：python -m unittest discover -s tests -t ."""

import json
import tempfile
import unittest
from pathlib import Path

from miniharness.boot import apply_patch, boot
from miniharness.core.session.persistence import (
    JsonlPersistence,
    SqlitePersistence,
    balanced_after_replay,
    load_events_checked,
    repair_and_replay,
)
from miniharness.core.session import (
    SESSION_FORMAT_VERSION,
    Session,
    create_message,
    text_block,
    turn_balance,
)


def _msg(text):
    return create_message("user", [text_block(text)], {"kind": "user"})


class TestJsonl(unittest.TestCase):
    def test_roundtrip_and_flush_barrier(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            s = Session("s1")
            s.append("user/message", _msg("hi"), surfaceOp="append")
            s.append("assistant/message", {
                "message": create_message("assistant", [text_block("yo")]),
            }, surfaceOp="append")
            for ev in s.events:
                p.append("s1", dict(ev))
            p.flush()   # 栅栏：写完之后 load 才能看到
            loaded = p.load("s1")
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0]["data"]["content"][0]["text"], "hi")
            self.assertEqual([e["seq"] for e in loaded], [0, 1])

    def test_header_written_once_with_version(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.append("s1", {"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}})
            p.flush()
            lines = (Path(tmp) / "s1.jsonl").read_text(encoding="utf-8").splitlines()
            header = json.loads(lines[0])
            self.assertEqual(header, {"version": SESSION_FORMAT_VERSION, "id": "s1"})

    def test_version_mismatch_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.append("s1", {"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}})
            p.flush()
            path = Path(tmp) / "s1.jsonl"
            lines = path.read_text(encoding="utf-8").splitlines()
            lines[0] = json.dumps({"version": 99, "id": "s1"})
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                p.load("s1")

    def test_torn_tail_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.append("s1", {"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}})
            p.flush()
            path = Path(tmp) / "s1.jsonl"
            # 模拟崩溃：第二条事件写到一半
            with open(path, "a", encoding="utf-8") as f:
                f.write('{"type": "turn/end", "seq": 1, "time": 2, "data": {"turn": 1')
            loaded = p.load("s1")
            self.assertEqual(len(loaded), 1)
            self.assertEqual(loaded[0]["type"], "turn/start")
            # 磁盘上残行已被截断修复
            with open(path, encoding="utf-8") as f:
                self.assertEqual(len(f.readlines()), 2)  # header + 1 事件


class TestSqlite(unittest.TestCase):
    def test_roundtrip_seq_monotonic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = SqlitePersistence(root)
            s = Session("s1")
            for i in range(3):
                s.append("user/message", _msg(f"m{i}"), surfaceOp="append")
            for ev in s.events:
                p.append("s1", dict(ev))
            p.flush()
            p.close()
            p2 = SqlitePersistence(root)   # 重开：SCHEMA_VERSION 一致
            events = p2.load("s1")
            self.assertEqual([e["seq"] for e in events], [0, 1, 2])
            p2.close()

    def test_schema_version_mismatch_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = SqlitePersistence(root)
            p._conn.execute("UPDATE meta SET value='99' WHERE key='schema_version'")
            p._conn.commit()
            p.close()
            with self.assertRaises(RuntimeError):
                SqlitePersistence(root)


class TestRecovery(unittest.TestCase):
    def test_crash_recovery_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            # 崩溃前只写入了 turn/start + user/message（turn/end 未及写入）
            p.append("s1", {"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}})
            p.append("s1", {"type": "user/message", "seq": 1, "time": 2,
                            "data": _msg("hi"), "surfaceOp": "append"})
            p.flush()
            session = repair_and_replay(p, "s1", Session("s1"))
            self.assertEqual(turn_balance(session.events), 0)
            ends = [e for e in session.events if e["type"] == "turn/end"]
            self.assertEqual(len(ends), 1)
            self.assertEqual(ends[0]["data"]["reason"], {"kind": "interrupted"})
            self.assertTrue(balanced_after_replay(p, "s1"))

    def test_unknown_event_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.append("s1", {"type": "future/event", "seq": 0, "time": 1, "data": {}})
            p.flush()
            with self.assertRaises(RuntimeError):
                load_events_checked(p.load("s1"))

    def test_unknown_event_with_ignorable_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.append("s1", {"type": "future/event", "seq": 0, "time": 1, "data": {}, "ignorable": True})
            p.flush()
            self.assertEqual(len(load_events_checked(p.load("s1"))), 1)

    def test_resume_conversation_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            s1 = Session("s1")
            s1.append("user/message", _msg("记住我的名字是张三"), surfaceOp="append")
            for ev in s1.events:
                p.append("s1", dict(ev))
            p.flush()
            s2 = repair_and_replay(p, "s1", Session("s1"))
            self.assertEqual(s2.events[0]["type"], "user/message")
            self.assertEqual(s2.events[0]["data"]["content"][0]["text"], "记住我的名字是张三")


class TestBoot(unittest.TestCase):
    def test_patch_replace_and_insert(self):
        entries = [{"id": "a", "config": {"x": 1}}, {"id": "b", "config": {}}]
        patched = apply_patch(entries, [
            {"replace": {"id": "a", "config": {"x": 2}}},
            {"insert": [{"id": "c", "config": {}}]},
        ])
        self.assertEqual(patched[0]["config"], {"x": 2})
        self.assertEqual([e["id"] for e in patched], ["a", "b", "c"])

    def test_patch_missing_target_fails(self):
        with self.assertRaises(KeyError):
            apply_patch([{"id": "a", "config": {}}], [{"replace": {"id": "zz", "config": {}}}])

    def test_boot_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cordis.json"
            config.write_text(json.dumps({"plugins": [
                {"id": "greeter", "module": "miniharness.example_plugins", "config": {"greeting": "你好"}},
            ]}), encoding="utf-8")
            patch = root / "patch.json"
            patch.write_text(json.dumps([
                {"replace": {"id": "greeter", "config": {"greeting": "你好呀"}}},
                {"insert": [{"id": "extra", "module": "miniharness.example_plugins", "config": {"service_name": "extra_greeter"}}]},
            ]), encoding="utf-8")
            ctx, activations = boot(config, patch)
            self.assertEqual([n for n, _ in activations], ["greeter", "extra"])
            self.assertEqual(ctx.inject("greeter")("张三"), "你好呀, 张三!")
            self.assertEqual(ctx.inject("extra_greeter")("李四"), "hello, 李四!")

    def test_boot_asserts_all_active(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = root / "cordis.json"
            config.write_text(json.dumps({"plugins": [
                {"id": "greeter", "module": "miniharness.example_plugins"},
                {"id": "missing", "module": "miniharness.no_such_module"},
            ]}), encoding="utf-8")
            with self.assertRaises(ImportError):
                boot(config)


if __name__ == "__main__":
    unittest.main()