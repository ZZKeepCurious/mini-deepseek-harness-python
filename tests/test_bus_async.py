"""阶段 7 验收：事件总线 asyncio 变体（aemit / awaterfall / aparallel）。"""

import asyncio
import time
import unittest

from miniharness.bus import Context


class TestAEmit(unittest.TestCase):
    def test_async_listeners_awaited_in_order(self):
        ctx = Context()
        order = []

        async def a(p):
            await asyncio.sleep(0.01)
            order.append("a")

        async def b(p):
            await asyncio.sleep(0.01)
            order.append("b")

        ctx.on("e", a)
        ctx.on("e", b)
        asyncio.run(ctx.aemit("e"))
        self.assertEqual(order, ["a", "b"])

    def test_sync_and_async_mixed(self):
        ctx = Context()
        seen = []

        def sync(p):
            seen.append("sync")

        async def aasync(p):
            await asyncio.sleep(0)
            seen.append("async")

        ctx.on("e", sync)
        ctx.on("e", aasync)
        asyncio.run(ctx.aemit("e"))
        self.assertEqual(seen, ["sync", "async"])

    def test_payload_passthrough(self):
        ctx = Context()
        got = []

        def sync(p):
            got.append(p)

        ctx.on("e", sync)
        asyncio.run(ctx.aemit("e", {"x": 1}))
        self.assertEqual(got, [{"x": 1}])


class TestAWaterfall(unittest.TestCase):
    def test_async_middleware_short_circuit(self):
        ctx = Context()

        async def blocker(p, nxt):
            return {"verdict": "deny"}

        async def never_reached(p, nxt):
            raise AssertionError("短路后不应被调用")

        ctx.on("w", blocker)
        ctx.on("w", never_reached)
        result = asyncio.run(ctx.awaterfall("w", {"v": 1}))
        self.assertEqual(result, {"verdict": "deny"})

    def test_async_delegation_and_sync_middleware(self):
        ctx = Context()

        def first(p, nxt):
            return nxt({"step": p.get("step", 0) + 1})

        async def second(p, nxt):
            await asyncio.sleep(0.01)
            return nxt({"step": p["step"] + 1})

        ctx.on("w", first)
        ctx.on("w", second)
        result = asyncio.run(ctx.awaterfall("w", {"step": 0}))
        self.assertEqual(result, {"step": 2})

    def test_async_middleware_can_await_before_delegating(self):
        ctx = Context()
        order = []

        async def mw(p, nxt):
            await asyncio.sleep(0.01)
            order.append("mw")
            return nxt(p)

        async def tail(p, nxt):
            order.append("tail")
            return "final"

        ctx.on("w", mw)
        ctx.on("w", tail)
        result = asyncio.run(ctx.awaterfall("w"))
        self.assertEqual(result, "final")
        self.assertEqual(order, ["mw", "tail"])

    def test_empty_returns_payload(self):
        ctx = Context()
        self.assertEqual(asyncio.run(ctx.awaterfall("w", 42)), 42)


class TestAParallel(unittest.TestCase):
    def test_truly_concurrent(self):
        ctx = Context()

        async def slow(p):
            await asyncio.sleep(0.15)
            return "s"

        ctx.on("e", slow)
        ctx.on("e", slow)
        t0 = time.monotonic()
        results = asyncio.run(ctx.aparallel("e"))
        elapsed = time.monotonic() - t0
        self.assertEqual(results, ["s", "s"])
        self.assertLess(elapsed, 0.28)   # 并发：总时长 ≈ 单个 0.15

    def test_results_in_registration_order(self):
        ctx = Context()

        async def a(p):
            await asyncio.sleep(0.05)
            return "a"

        async def b(p):
            await asyncio.sleep(0.01)
            return "b"

        ctx.on("e", a)
        ctx.on("e", b)
        self.assertEqual(asyncio.run(ctx.aparallel("e")), ["a", "b"])

    def test_sync_listeners_mixed(self):
        ctx = Context()
        ctx.on("e", lambda p: 1)
        ctx.on("e", lambda p: 2)
        self.assertEqual(asyncio.run(ctx.aparallel("e")), [1, 2])


if __name__ == "__main__":
    unittest.main()