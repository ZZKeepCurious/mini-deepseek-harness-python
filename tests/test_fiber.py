"""fiber 生命周期 + effect 全语义（对齐上游 vendor/cordis/src/fiber.ts Phase 1）。

覆盖：effect 调用约定（body 立即执行、返回值收集为 disposer）、四种返回值形态、
setup 失败回滚、generator / async generator、单发 + awaitable disposer、setup
barrier（重入拆解）、fiber 状态机 + internal/status、INACTIVE_EFFECT、异步拆解
顺序与并发、错误 contained、父拆解收回作用域 fiber。
"""
from __future__ import annotations

import asyncio
import unittest

from miniharness.core.scope import (
    Context,
    CordisError,
    FiberState,
    INACTIVE_EFFECT,
)


class TestEffectCallingConvention(unittest.TestCase):
    """对齐上游：execute 立即执行，返回值收集为 disposer（与旧 mini 相反）。"""

    def test_effect_executes_body_at_registration(self):
        ctx = Context()
        calls = []
        ctx.effect(lambda: calls.append("body"))
        self.assertEqual(calls, ["body"])

    def test_effect_returned_disposer_runs_on_dispose(self):
        ctx = Context()
        order = []
        d = ctx.effect(lambda: (lambda: order.append("dispose")))
        self.assertEqual(order, [])   # body 立即执行，但 disposer 尚未跑
        d()
        self.assertEqual(order, ["dispose"])

    def test_effect_none_body_no_disposer(self):
        ctx = Context()
        d = ctx.effect(lambda: None)
        d()   # 无 disposer → no-op，不报错

    def test_effect_invalid_body_type_raises(self):
        ctx = Context()
        with self.assertRaises(TypeError):
            ctx.effect(lambda: 42)


class TestGeneratorEffects(unittest.TestCase):
    def test_generator_yields_disposers_reverse_on_dispose(self):
        ctx = Context()
        order = []

        def body():
            yield None
            yield lambda: order.append("d1")
            yield lambda: order.append("d2")

        ctx.effect(body)
        self.assertEqual(order, [])
        ctx.dispose()
        self.assertEqual(order, ["d2", "d1"])

    def test_generator_invalid_yield_rolls_back_and_raises(self):
        ctx = Context()
        order = []

        def body():
            yield lambda: order.append("d1")
            yield 123

        with self.assertRaises(TypeError):
            ctx.effect(body)
        self.assertEqual(order, ["d1"])   # 已收集项逆序回滚

    def test_sync_body_throw_rolls_back_own_and_rethrows(self):
        ctx = Context()
        order = []

        def body():
            yield lambda: order.append("own")
            raise ValueError("boom")

        with self.assertRaisesRegex(ValueError, "boom"):
            ctx.effect(body)
        # 当前 effect 已收集项被回滚；fiber 本身仍存活（失败只影响当前 effect）
        self.assertEqual(order, ["own"])
        ctx.on("e", lambda p: None)

    def test_async_generator_effect(self):
        async def main():
            ctx = Context()
            order = []

            async def body():
                yield None
                yield lambda: order.append("x")

            ctx.effect(body)
            completion = ctx.dispose()
            await completion
            self.assertEqual(order, ["x"])

        asyncio.run(main())


class TestDisposerSemantics(unittest.TestCase):
    def test_dispose_reverse_order_across_effects(self):
        ctx = Context()
        order = []

        def reg(tag):
            return ctx.effect(lambda t=tag: (lambda: order.append(f"d:{t}")))

        reg("a")
        reg("b")
        ctx.dispose()
        self.assertEqual(order, ["d:b", "d:a"])

    def test_disposer_single_shot(self):
        ctx = Context()
        order = []
        d = ctx.effect(lambda: (lambda: order.append("d")))
        d()
        d()
        self.assertEqual(order, ["d"])

    def test_await_effect_disposer(self):
        async def main():
            ctx = Context()
            order = []
            d = ctx.effect(lambda: (lambda: order.append("d")))
            await d
            self.assertEqual(order, ["d"])

        asyncio.run(main())

    def test_async_disposers_within_effect_sequential_reverse(self):
        async def main():
            ctx = Context()
            order = []

            def body():
                async def d1():
                    await asyncio.sleep(0)
                    order.append("d1")

                async def d2():
                    await asyncio.sleep(0)
                    order.append("d2")

                yield d1
                yield d2

            ctx.effect(body)
            completion = ctx.dispose()
            self.assertIsNotNone(completion)
            await completion
            self.assertEqual(order, ["d2", "d1"])

        asyncio.run(main())

    def test_async_dispose_waits_all_effects(self):
        async def main():
            ctx = Context()
            order = []

            def reg(tag):
                async def disposer():
                    await asyncio.sleep(0)
                    order.append(tag)
                return ctx.effect(lambda t=tag: disposer, f"e:{tag}")

            reg("a")
            reg("b")
            completion = ctx.dispose()
            self.assertIsNotNone(completion)
            await completion
            self.assertEqual(sorted(order), ["a", "b"])

        asyncio.run(main())


class TestFiberStateMachine(unittest.TestCase):
    def test_root_fiber_active_scope_fiber_transitions(self):
        ctx = Context()
        statuses = []
        ctx.on("internal/status",
               lambda p: statuses.append((p["old"], p["fiber"].state)))
        scope = ctx.create_scope("agent:1")
        self.assertIn(("pending", "loading"), statuses)
        self.assertIn(("loading", "active"), statuses)
        self.assertEqual(scope.fiber.state, FiberState.ACTIVE)
        statuses.clear()
        scope.dispose()
        self.assertIn(("active", "unloading"), statuses)
        self.assertIn(("unloading", "disposed"), statuses)
        self.assertEqual(scope.fiber.state, FiberState.DISPOSED)

    def test_create_scope_mints_distinct_fiber(self):
        root = Context()
        s1 = root.create_scope("a")
        s2 = root.create_scope("b")
        self.assertIsNot(s1.fiber, s2.fiber)
        self.assertNotEqual(s1.fiber.uid, s2.fiber.uid)
        self.assertIs(s1.fiber.context, s1.ctx)
        self.assertIs(s2.fiber.context, s2.ctx)

    def test_effect_rejected_after_dispose(self):
        ctx = Context()
        ctx.dispose()
        with self.assertRaisesRegex(RuntimeError, "已销毁"):
            ctx.effect(lambda: None)
        with self.assertRaises(CordisError) as cm:
            ctx.fiber.effect(lambda: None)
        self.assertEqual(cm.exception.code, INACTIVE_EFFECT)

    def test_registration_rejected_during_unloading(self):
        async def main():
            ctx = Context()
            rejected = []

            def body():
                async def disposer():
                    try:
                        ctx.on("e", lambda p: None)
                        rejected.append("no-error")
                    except RuntimeError:
                        rejected.append("rejected")
                return disposer

            ctx.effect(body)
            completion = ctx.dispose()
            self.assertIsNotNone(completion)
            await completion
            self.assertEqual(rejected, ["rejected"])

        asyncio.run(main())


class TestDisposeIdempotenceAndJoin(unittest.TestCase):
    def test_dispose_idempotent_joins_inflight(self):
        async def main():
            ctx = Context()
            order = []

            def body():
                async def disposer():
                    await asyncio.sleep(0)
                    order.append("done")
                return disposer

            ctx.effect(body)
            c1 = ctx.dispose()
            c2 = ctx.dispose()
            self.assertIs(c1, c2)   # 竞态共享同一完成
            await c1
            self.assertEqual(order, ["done"])

        asyncio.run(main())

    def test_root_dispose_unwinds_scope_fiber(self):
        root = Context()
        scope = root.create_scope("agent:1")
        served = []
        scope.on("e", lambda p: served.append(p))
        root.dispose()
        self.assertEqual(scope.fiber.state, FiberState.DISPOSED)
        with self.assertRaisesRegex(RuntimeError, "已销毁"):
            scope.on("e", lambda p: None)


class TestReentrantUnload(unittest.TestCase):
    def test_registration_before_execute_reentrant_unload(self):
        async def main():
            ctx = Context()
            order = []

            def body():
                # body 执行中途触发 unload：当前 effect 已先入 fiber._disposables
                #（registration-before-execute），拆解会挂 setup barrier 等 body
                # 完成收集后再清理 → 返回的 disposer 不会漏跑。
                ctx.dispose()

                async def disposer():
                    order.append("dispose")

                return disposer

            d = ctx.effect(body)
            await d
            self.assertIn("dispose", order)

        asyncio.run(main())


class TestErrorContainment(unittest.TestCase):
    def test_dispose_error_contained_in_fiber(self):
        ctx = Context()

        def body():
            def disposer():
                raise ValueError("bad teardown")
            return disposer

        ctx.effect(body)
        ctx.dispose()   # 不抛出；错误 recorded
        self.assertEqual(len(ctx.fiber._errors), 1)
        self.assertIsInstance(ctx.fiber._errors[0], ValueError)

    def test_get_effects_labels(self):
        ctx = Context()
        ctx.effect(lambda: None, "first")
        ctx.effect(lambda: None, "second")
        labels = [label for label in ctx.fiber.get_effects()
                  if not label.startswith("ctx.provide(")
                  and label != "ctx.logger.exporter()"]
        self.assertEqual(labels, ["first", "second"])


if __name__ == "__main__":
    unittest.main()