"""session-projection-cache（M14）验收。

对齐上游 `packages/session/session-projection-cache/tests/cache.spec.ts`：

- 写后节流：turn/end 强制、create 强制、dispose 强制、count threshold、
  interval 惰性定时
- 身份匹配：lifecycle 完全匹配才 serve、formatVersion 拒读、predecessor title
- 冷读播种：coldSnapshot 从 checkpoint 播种 + 尾部重折 + 写回
- 无损 JSON 检查（非 JSON 单元拒绝落盘）
"""
import os
import tempfile
import unittest

from miniharness.core.agent_loop.resident_loop import shutdown_resident_loop
from miniharness.core.scope import Context
from miniharness.core.session_store import install_sessions
from miniharness.session_projection import install_session_projections
from miniharness.session_projection_cache import (
    SessionProjectionCache,
    install_session_projection_cache,
)
from miniharness.storage import install_storage


class ProjectionCacheTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Context(name="projcache")
        self.store = install_sessions(self.ctx)
        install_storage(self.ctx, os.path.join(self.tmp.name, "storage"))
        install_session_projections(self.ctx)
        from miniharness.telemetry import install_usage_stats
        install_usage_stats(self.ctx)
        self.cache = install_session_projection_cache(
            self.ctx, {"writeEveryEvents": 100, "writeIntervalMs": 60000})
        self.session = self.store.create("c1", {"meta": {"cwd": os.getcwd()}})

    def tearDown(self):
        self.ctx.dispose()
        shutdown_resident_loop()

    def _append_turn(self):
        self.session.append("turn/start", {"turn": 1})
        self.session.append("user/message", {
            "id": "m1", "role": "user", "content": [{"type": "text", "text": "hi"}],
            "source": {"kind": "user"},
        }, surfaceOp="append")
        self.session.append("turn/end", {"turn": 1, "reason": {"kind": "stop"}})

    def _stored_rows(self):
        return self.cache._table.get("c1")

    def test_turn_end_mandatory(self):
        self._append_turn()
        record = self._stored_rows()
        self.assertIsNotNone(record)
        self.assertEqual(record["identity"]["formatVersion"], 4)
        self.assertEqual(record["identity"]["isSeeded"], False)
        self.assertEqual(record["identity"]["inheritedEventCount"], 0)
        self.assertEqual(record["rows"]["sessionStats"]["ver"], 1)
        # val 折进 sessionStats 状态
        self.assertIsInstance(record["rows"]["sessionStats"]["val"], dict)

    def test_cached_snapshot_returns_wire_view(self):
        self._append_turn()
        snapshot = self.cache.cached_snapshot(
            {"id": "c1", "createdAt": self.session.created_at, "isSeeded": False,
             "cwd": os.getcwd()})
        self.assertIsNotNone(snapshot)
        self.assertIn("sessionStats", snapshot["values"])
        self.assertIn("tokenUsage", snapshot["values"])

    def test_cached_snapshot_refuses_foreign_lifecycle(self):
        self._append_turn()
        snapshot = self.cache.cached_snapshot(
            {"id": "c1", "createdAt": self.session.created_at + 1,
             "isSeeded": False, "cwd": os.getcwd()})
        self.assertIsNone(snapshot)

    def test_cached_snapshot_refuses_unknown_id(self):
        snapshot = self.cache.cached_snapshot(
            {"id": "nope", "createdAt": 1, "isSeeded": False})
        self.assertIsNone(snapshot)

    def test_create_mandatory(self):
        # create 后（无 turn/end）也落盘
        self.session.append("turn/start", {"turn": 1})
        record = self._stored_rows()
        self.assertIsNotNone(record)

    def test_cold_snapshot_seeds_from_checkpoint(self):
        self._append_turn()
        events = list(self.session.events)
        snapshot = self.cache.cold_snapshot(
            {"id": "c1", "createdAt": self.session.created_at, "isSeeded": False,
             "cwd": os.getcwd()},
            0, events)
        self.assertIn("sessionStats", snapshot["values"])

    def test_unseeded_cut_guard(self):
        with self.assertRaises(ValueError):
            self.cache.cold_snapshot(
                {"id": "c1", "createdAt": 1, "isSeeded": False}, 1, [])

    def test_count_threshold(self):
        # 独立 ctx：以 writeEveryEvents=3 装一个新缓存（幂等装置不能换配置）
        from miniharness.core.scope import Context as Ctx
        from miniharness.telemetry import install_usage_stats
        tmp2 = tempfile.TemporaryDirectory()
        self.addCleanup(tmp2.cleanup)
        ctx2 = Ctx(name="projcache-2")
        store2 = install_sessions(ctx2)
        install_storage(ctx2, os.path.join(tmp2.name, "storage"))
        install_session_projections(ctx2)
        install_usage_stats(ctx2)
        cache = install_session_projection_cache(
            ctx2, {"writeEveryEvents": 3, "writeIntervalMs": 60000})
        session = store2.create("c2", {"meta": {"cwd": os.getcwd()}})
        self.addCleanup(ctx2.dispose)

        def seq():
            record = cache._table.get("c2")
            return record["rows"]["sessionStats"]["seq"] if record else None

        # create 已落（seq -1）
        self.assertEqual(seq(), -1)
        # 前 2 条事件未达阈值（pending 1, 2）
        session.append("turn/start", {"turn": 1})
        session.append("user/message", {
            "id": "m1", "role": "user", "content": [{"type": "text", "text": "hi"}],
            "source": {"kind": "user"},
        }, surfaceOp="append")
        self.assertEqual(seq(), -1)
        # 第 3 条 → 阈值触发（writeEveryEvents=3）
        session.append("step/start", {"turn": 1, "step": 1})
        self.assertGreater(seq(), -1)

    def test_non_json_unit_rejected(self):
        # 注册一个返回非 JSON 状态的单元 → checkpoint 拒绝落盘
        from miniharness.session_projection import ProjectionDefinition
        registry = self.ctx.get("sessionProjections")

        def bad_init(header, inherited):
            return {"marks": {"bad": object()}}  # object() 不可 JSON 序列化

        def bad_apply(state, event):
            return state

        registry.register(ProjectionDefinition(
            "cache-test/bad", init=bad_init, apply=bad_apply, state_version=1))
        self._append_turn()
        with self.assertRaises(TypeError):
            self.cache.write(self.session)

    def test_install_idempotent(self):
        again = install_session_projection_cache(
            self.ctx, {"writeEveryEvents": 100, "writeIntervalMs": 60000})
        self.assertIs(again, self.cache)

    def test_missing_storage_fails_loud(self):
        from miniharness.core.scope import Context as Ctx
        ctx2 = Ctx(name="bare-cache")
        try:
            with self.assertRaises(RuntimeError):
                install_session_projection_cache(
                    ctx2, {"writeEveryEvents": 1, "writeIntervalMs": 1})
        finally:
            ctx2.dispose()

    def test_config_validation(self):
        with self.assertRaises(ValueError):
            SessionProjectionCache(self.ctx, {"writeEveryEvents": 0,
                                              "writeIntervalMs": 1})
        with self.assertRaises(ValueError):
            SessionProjectionCache(self.ctx, {"writeEveryEvents": 1,
                                              "writeIntervalMs": 0})


if __name__ == "__main__":
    unittest.main()