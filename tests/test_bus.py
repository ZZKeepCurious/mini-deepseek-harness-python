"""第 2 章验收：插件上下文 + 事件总线 + 作用域。运行：python -m unittest discover -s tests -t ."""

import unittest

from miniharness.bus import Context, PluginManager


class TestBus(unittest.TestCase):
    def test_provide_inject(self):
        ctx = Context()
        ctx.provide("s", 42)
        self.assertEqual(ctx.inject("s"), 42)

    def test_inject_missing_raises(self):
        ctx = Context()
        with self.assertRaises(KeyError):
            ctx.inject("nope")

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

    def test_scope_visibility_up_chain(self):
        root = Context()
        root.provide("svc", "global")
        a = root.create_scope("a")
        self.assertEqual(a.inject("svc"), "global")
        a.provide("local", "A")
        self.assertEqual(a.inject("local"), "A")
        b = root.create_scope("b")
        with self.assertRaises(KeyError):
            b.inject("local")   # 兄弟作用域不可见

    def test_plugin_manager_dependency_order(self):
        root = Context()
        manager = PluginManager(root)
        activations = manager.activate([
            {
                "name": "consumer",
                "inject": ["svc"],
                "apply": lambda ctx: ctx.effect(lambda: None),  # 记录 svc 可用
            },
            {
                "name": "provider",
                "provides": ["svc"],
                "apply": lambda ctx: ctx.provide("svc", 42),
            },
        ])
        # provider 必须先于 consumer 激活（依赖驱动，而非手工排序）
        self.assertEqual([n for n, _ in activations], ["provider", "consumer"])
        self.assertEqual(root.inject("svc"), 42)

    def test_plugin_manager_cycle_raises(self):
        root = Context()
        manager = PluginManager(root)
        with self.assertRaises(RuntimeError):
            manager.activate([
                {"name": "p1", "inject": ["x"], "provides": ["y"], "apply": lambda ctx: None},
                {"name": "p2", "inject": ["y"], "provides": ["x"], "apply": lambda ctx: None},
            ])

    def test_plugin_manager_dispose_rolls_back(self):
        root = Context()
        manager = PluginManager(root)
        activations = manager.activate([
            {"name": "p", "provides": ["svc"], "apply": lambda ctx: ctx.provide("svc", 1)},
        ])
        activations[0][1]()   # 卸载插件
        with self.assertRaises(KeyError):
            root.inject("svc")


if __name__ == "__main__":
    unittest.main()