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
        # 载体级断言走明文后端（zstd 载体的帧级行为见 test_persistence_zstd）
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp), compression="none")
            p.append("s1", {"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}})
            p.flush()
            lines = p.path_of("s1").read_text(encoding="utf-8").splitlines()
            header = json.loads(lines[0])
            # 扁平 header（上游 SessionHeader）：恒带 createdAt（Date.now 语义）
            self.assertEqual(header["version"], SESSION_FORMAT_VERSION)
            self.assertEqual(header["id"], "s1")
            self.assertIsInstance(header["createdAt"], int)

    def test_version_mismatch_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp), compression="none")
            p.append("s1", {"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}})
            p.flush()
            path = p.path_of("s1")
            lines = path.read_text(encoding="utf-8").splitlines()
            lines[0] = json.dumps({"version": 99, "id": "s1"})
            path.write_text("\n".join(lines) + "\n", encoding="utf-8")
            with self.assertRaises(RuntimeError):
                p.load("s1")

    def test_torn_tail_truncated(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp), compression="none")
            p.append("s1", {"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}})
            p.flush()
            path = p.path_of("s1")
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

    def test_repair_persists_closers_jsonl(self):
        # 上游 commitRepair：修复合成的 closers 必须落盘（追加 + fsync），
        # 二次加载幂等（不会重复追加 closers）。
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.append("s1", {"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}})
            p.append("s1", {"type": "user/message", "seq": 1, "time": 2,
                            "data": _msg("hi"), "surfaceOp": "append"})
            p.flush()
            first = repair_and_replay(p, "s1", Session("s1"))
            # 内存会话 = 2 真实 + 1 closer + session/end-seed 构造标记
            self.assertEqual(len(first.events), 4)
            # 磁盘上已持久化 closers：直接读文件也应看到 turn/end
            persisted = p.load("s1")
            self.assertEqual([e["type"] for e in persisted], ["turn/start", "user/message", "turn/end"])
            # 二次加载：日志已平衡 → 不再合成新 closers（幂等）
            second = repair_and_replay(p, "s1", Session("s1"))
            self.assertEqual(len(second.events), 4)
            self.assertEqual(len(persisted), len(p.load("s1")))

    def test_repair_persists_closers_sqlite(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p = SqlitePersistence(root)
            p.append("s1", {"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}})
            p.append("s1", {"type": "user/message", "seq": 1, "time": 2,
                            "data": _msg("hi"), "surfaceOp": "append"})
            p.flush()
            first = repair_and_replay(p, "s1", Session("s1"))
            self.assertEqual(len(first.events), 4)  # 3 日志 + end-seed 标记
            p.close()
            # 重开后日志仍含持久化的 closers
            p2 = SqlitePersistence(root)
            self.assertEqual(
                [e["type"] for e in p2.load("s1")],
                ["turn/start", "user/message", "turn/end"],
            )
            second = repair_and_replay(p2, "s1", Session("s1"))
            self.assertEqual(len(second.events), 4)
            p2.close()

    def test_unknown_event_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.append("s1", {"type": "future/event", "seq": 0, "time": 1, "data": {}})
            p.flush()
            with self.assertRaises(RuntimeError) as ctx:
                load_events_checked(p.load("s1"))
            self.assertIn(
                'unknown to this harness and not marked ignorable', str(ctx.exception)
            )

    def test_unknown_ignorable_event_allowed(self):
        # alpha.2 对齐上游 coordinator.ts assertEventsSupported：带 ignorable 标记的
        # 未知事件放行保留（丢失不影响重建），不标则响亮拒绝。
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.append("s1", {"type": "future/info", "seq": 0, "time": 1,
                            "data": {"note": "x"}, "ignorable": True})
            p.append("s1", {"type": "user/message", "seq": 1, "time": 2,
                            "data": {}, "ignorable": False})
            p.flush()
            loaded = load_events_checked(p.load("s1"))
            self.assertEqual([e["type"] for e in loaded], ["future/info", "user/message"])
            self.assertTrue(loaded[0]["ignorable"])

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

    def test_patch_missing_target_with_on_missing_skips_entry(self):
        # 上游 per-entry Loader warning（index.ts:309-311）：
        # 目标缺失 → on_missing 通知 + 跳过该条，其余补丁照常应用
        warned = []
        patched = apply_patch(
            [{"id": "a", "config": {"x": 1}}],
            [
                {"replace": {"id": "zz", "config": {"y": 2}}},
                {"insert": [{"id": "new", "config": {}}]},
            ],
            on_missing=warned.append,
        )
        self.assertEqual(warned, ["zz"])
        self.assertEqual([e["id"] for e in patched], ["a", "new"])
        self.assertEqual(patched[0]["config"], {"x": 1})  # 目标缺失的 replace 未生效

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
            self.assertEqual(ctx.get("greeter")("张三"), "你好呀, 张三!")
            self.assertEqual(ctx.get("extra_greeter")("李四"), "hello, 李四!")

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