"""第 2 章验收：插件上下文 + 事件总线 + 作用域 + 注册表（Cordis 对齐）。
运行：python -m unittest discover -s tests -t ."""

import unittest

from miniharness.core.scope import Context, FiberState, RegistryService


class TestBus(unittest.TestCase):
    def test_provide_inject(self):
        ctx = Context()
        ctx.provide("s", 42)
        self.assertEqual(ctx.get("s"), 42)

    def test_get_missing_returns_none(self):
        ctx = Context()
        self.assertIsNone(ctx.get("nope"))   # 对齐上游 get：未提供 → None（不抛 KeyError）

    def test_get_strict_filters_inactive_provider(self):
        ctx = Context()
        fiber = ctx.plugin({"name": "p", "apply": lambda ctx, cfg: ctx.provide("s", 1)})
        self.assertEqual(fiber.state, FiberState.ACTIVE)
        self.assertEqual(ctx.get("s"), 1)
        fiber.dispose()
        self.assertIsNone(ctx.get("s"))      # 提供者非 ACTIVE → strict 返回 None

    def test_emit_order(self):
        ctx = Context()
        calls = []
        ctx.on("e", lambda p: calls.append("a"))
        ctx.on("e", lambda p: calls.append("b"))
        ctx.emit("e")
        self.assertEqual(calls, ["a", "b"])

    def test_waterfall_short_circuit(self):
        ctx = Context()
        ctx.on("w", lambda p, nxt: "DENY")   # 不调 next → 短路
        ctx.on("w", lambda p, nxt: nxt("ALLOW"))
        self.assertEqual(ctx.waterfall("w", {}), "DENY")

    def test_waterfall_chain(self):
        ctx = Context()
        ctx.on("w", lambda p, nxt: nxt(p + 1))
        ctx.on("w", lambda p, nxt: nxt(p + 1))
        self.assertEqual(ctx.waterfall("w", 0), 2)

    def test_waterfall_chain_with_transform(self):
        ctx = Context()
        ctx.on("w", lambda p, nxt: nxt({"v": p["v"] + 1}))
        ctx.on("w", lambda p, nxt: "REJECT" if p["v"] > 5 else nxt(p))
        self.assertEqual(ctx.waterfall("w", {"v": 4}), {"v": 5})
        self.assertEqual(ctx.waterfall("w", {"v": 6}), "REJECT")

    def test_parallel_gathers(self):
        ctx = Context()
        ctx.on("e", lambda p: p * 2)
        ctx.on("e", lambda p: p * 3)
        self.assertEqual(ctx.parallel("e", 5), [10, 15])

    def test_serial_ordered(self):
        ctx = Context()
        ctx.on("e", lambda p: f"first:{p}")
        ctx.on("e", lambda p: f"second:{p}")
        self.assertEqual(ctx.serial("e", "x"), ["first:x", "second:x"])

    def test_dispose_rollback(self):
        ctx = Context()
        ctx.on("e", lambda p: None)
        ctx.provide("s", 1)
        ctx.dispose()
        with self.assertRaises(RuntimeError):
            ctx.provide("s2", 2)   # 已销毁，拒绝注册

    def test_scope_visibility(self):
        root = Context()
        root.provide("svc", "global")
        a = root.create_scope("a")
        self.assertEqual(a.get("svc"), "global")   # 根服务全作用域可见
        a.provide("local", "A")
        b = root.create_scope("b")
        # 兄弟作用域共享根标签（对齐上游全局 isolate store：scope 不提供服务，
        # 进程级服务在根上对所有作用域可见）
        self.assertEqual(b.get("local"), "A")
        iso = a.isolate("local")   # 隔离作用域：name 换新标签解析
        iso.provide("local", "B")
        self.assertEqual(a.get("local"), "A")      # 原标签不受影响
        self.assertEqual(iso.get("local"), "B")
        self.assertEqual(b.get("local"), "A")

    def test_dependency_wakes_pending_fiber(self):
        root = Context()
        activations = []
        consumer = root.plugin({
            "name": "consumer",
            "inject": ["svc"],
            "apply": lambda ctx, cfg: activations.append(ctx.get("svc")),
        })
        # 依赖缺失 → 静默 PENDING，不激活
        self.assertEqual(consumer.state, FiberState.PENDING)
        provider = root.plugin({
            "name": "provider",
            "apply": lambda ctx, cfg: ctx.provide("svc", 42),
        })
        # 依赖满足 → provider 装载后唤醒 consumer（依赖驱动，而非手工排序）
        self.assertEqual(provider.state, FiberState.ACTIVE)
        self.assertEqual(consumer.state, FiberState.ACTIVE)
        self.assertEqual(activations, [42])
        self.assertEqual(root.get("svc"), 42)

    def test_dependency_cycle_stays_pending(self):
        root = Context()
        p1 = root.plugin({"name": "p1", "inject": ["y"], "apply": lambda ctx, cfg: None})
        p2 = root.plugin({"name": "p2", "inject": ["x"], "apply": lambda ctx, cfg: None})
        self.assertEqual(p1.state, FiberState.PENDING)   # 环依赖 → 静默 PENDING（不抛错）
        self.assertEqual(p2.state, FiberState.PENDING)

    def test_dispose_rolls_back_service(self):
        root = Context()
        fiber = root.plugin({"name": "p", "apply": lambda ctx, cfg: ctx.provide("svc", 1)})
        self.assertEqual(root.get("svc"), 1)
        fiber.dispose()
        self.assertIsNone(root.get("svc"))   # 卸载 → 服务消失（strict get 回 None）


if __name__ == "__main__":
    unittest.main()