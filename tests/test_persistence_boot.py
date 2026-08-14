"""第 5 章验收：持久化 + 崩溃恢复 + 组合加载。运行：python -m unittest discover -s tests -t ."""

import json
import tempfile
import unittest
from pathlib import Path

from miniharness.boot import apply_patch, boot
from miniharness.persistence import (
    JsonlPersistence,
    SqlitePersistence,
    balanced_after_replay,
    load_events_checked,
    repair_and_replay,
)
from miniharness.session import Session, turn_balance


class TestJsonl(unittest.TestCase):
    def test_roundtrip_and_flush_barrier(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            s = Session("s1")
            s.append({"type": "user/message", "content": "hi", "surfaceOp": "append"})
            s.append({"type": "assistant/message", "content": "yo", "surfaceOp": "append"})
            for ev in s.events:
                p.append("s1", dict(ev))
            p.flush()   # 栅栏：写完之后 load 才能看到
            loaded = p.load("s1")
            self.assertEqual(len(loaded), 2)
            self.assertEqual(loaded[0]["content"], "hi")
            self.assertEqual([e["seq"] for e in loaded], [0, 1])


class TestSqlite(unittest.TestCase):
    def test_roundtrip_seq_monotonic(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = SqlitePersistence(root)
            s = Session("s1")
            for i in range(3):
                s.append({"type": "user/message", "content": f"m{i}", "surfaceOp": "append"})
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
            p.append("s1", {"type": "turn/start"})
            p.append("s1", {"type": "user/message", "content": "hi", "surfaceOp": "append"})
            p.flush()
            session = Session("s1")
            repair_and_replay(p, "s1", session)
            self.assertEqual(turn_balance(session.events), 0)
            self.assertEqual(session.events[-1]["type"], "turn/end")
            self.assertEqual(session.events[-1]["reason"], "interrupted")
            self.assertTrue(balanced_after_replay(p, "s1"))

    def test_unknown_event_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.append("s1", {"type": "future/event"})
            p.flush()
            with self.assertRaises(RuntimeError):
                load_events_checked(p.load("s1"))

    def test_unknown_event_with_ignorable_ok(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.append("s1", {"type": "future/event", "ignorable": True})
            p.flush()
            self.assertEqual(len(load_events_checked(p.load("s1"))), 1)

    def test_resume_conversation_after_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            s1 = Session("s1")
            s1.append({"type": "user/message", "content": "记住我的名字是张三", "surfaceOp": "append"})
            for ev in s1.events:
                p.append("s1", dict(ev))
            p.flush()
            s2 = Session("s1")
            repair_and_replay(p, "s1", s2)
            self.assertEqual(len(s2.events), 1)
            self.assertEqual(s2.events[0]["content"], "记住我的名字是张三")


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