"""第 2 章验收：插件上下文 + 事件总线 + 作用域 + 注册表（Cordis 对齐）。
运行：python -m unittest discover -s tests -t ."""

import unittest

from miniharness.core.scope import Context, FiberState, RegistryService, Service


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

    def test_prepend_listener_runs_first(self):
        ctx = Context()
        calls = []
        ctx.on("e", lambda p: calls.append("a"))
        ctx.on("e", lambda p: calls.append("b"), prepend=True)
        ctx.emit("e")
        self.assertEqual(calls, ["b", "a"])   # 对齐上游 EventOptions.prepend

    def test_once_fires_once(self):
        # 对齐上游 Events.once：首次触发先自注销再调用
        ctx = Context()
        calls = []
        ctx.once("e", lambda p: calls.append(p))
        ctx.emit("e", 1)
        ctx.emit("e", 2)
        self.assertEqual(calls, [1])

    def test_once_carrier_dispatch_self_removes(self):
        # 载波路（_flat_hooks）同样生效：once 触发后从扁平表移除
        from miniharness.core.dsh_scope import scope_target
        ctx = Context()
        scope = ctx.create_scope("agent:a")
        calls = []
        ctx.once("e", lambda p: calls.append(p))
        carrier = scope_target(object(), scope.scope_key)
        ctx.emit("e", 1, this_arg=carrier)    # 未打标监听器：任何载波都收
        ctx.emit("e", 2, this_arg=carrier)
        self.assertEqual(calls, [1])

    def test_once_waterfall_passthrough(self):
        ctx = Context()
        ctx.once("w", lambda p, nxt: nxt(p + 1))
        self.assertEqual(ctx.waterfall("w", 1), 2)
        self.assertEqual(ctx.waterfall("w", 1), 1)   # 已自注销 → 无监听器直通

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


class TestServiceBase(unittest.TestCase):
    def test_subclass_auto_provides(self):
        class Counter(Service):
            provide = "counter"

            def __init__(self, ctx, name=None):
                self.value = 0
                super().__init__(ctx, name)

            def _invoke(self, delta=1):
                self.value += delta
                return self.value

        ctx = Context()
        counter = Counter(ctx)
        self.assertEqual(ctx.get("counter"), counter)   # 构造即登记
        self.assertEqual(counter(2), 2)                  # _invoke → 可调用
        self.assertEqual(ctx.get("counter")(3), 5)

    def test_service_disposed_with_fiber(self):
        class Greeter(Service):
            provide = "greeter"

        root = Context()
        fiber = root.plugin({
            "name": "g",
            "apply": lambda ctx, cfg: Greeter(ctx, "greeter"),
        })
        self.assertEqual(fiber.state, FiberState.ACTIVE)
        self.assertIsInstance(root.get("greeter"), Greeter)
        fiber.dispose()
        self.assertIsNone(root.get("greeter"))   # 随拥有 fiber 自动注销

    def test_non_callable_service_raises(self):
        class Silent(Service):
            provide = "silent"

        ctx = Context()
        service = Silent(ctx)
        with self.assertRaises(TypeError):
            service()

    def test_missing_name_raises(self):
        class Anon(Service):
            pass

        with self.assertRaises(TypeError):
            Anon(Context())

    def test_intercept_merges_root_first(self):
        class Conf(Service):
            provide = "conf"

            def current(self):
                return self._resolve_config()

        root = Context()
        first = root.intercept("conf", {"a": 1, "b": "root"})
        child = first.intercept("conf", {"b": "child", "c": 3})
        ctx = child.extend(name="leaf")
        service = Conf(ctx)
        merged = service.current()
        self.assertEqual(merged, {"a": 1, "b": "child", "c": 3})   # 近根者优先，深层覆盖

    def test_intercept_does_not_mutate_parent(self):
        root = Context()
        root.intercept("svc", {"x": 1})
        self.assertEqual(root._resolve_intercept("svc"), [])        # 父上下文未被修改


class TestLoggerService(unittest.TestCase):
    def test_named_logger_facade(self):
        ctx = Context()
        logger = ctx.logger("tester")
        self.assertEqual(logger.name, "tester")
        for method in ("error", "info", "warn", "debug"):
            self.assertTrue(callable(getattr(logger, method)))

    def test_exporter_receives_messages(self):
        ctx = Context()
        received = []
        ctx.logger.exporter({"export": received.append})
        ctx.logger("tester").info("hello %s", "world")
        self.assertEqual(len(received), 1)
        message = received[0]
        self.assertEqual(message["name"], "tester")
        self.assertEqual(message["type"], "info")
        self.assertEqual(message["level"], 1)
        self.assertEqual(message["args"], ("hello %s", "world"))
        self.assertIn("sn", message)
        self.assertIn("ts", message)

    def test_level_filtering(self):
        ctx = Context()
        received = []
        ctx.logger.exporter({"levels": {"default": 2}, "export": received.append})
        ctx.logger("tester").debug("no")
        self.assertEqual(received, [])           # debug(3) < default(2) → 丢弃
        ctx.logger("tester").warn("yes")
        self.assertEqual(len(received), 1)

    def test_service_level_method_uses_fiber_name(self):
        ctx = Context()
        received = []
        ctx.logger.exporter({"export": received.append})
        ctx.logger.info("direct")   # 默认 INFO 阈值：warn/debug 会被过滤（对齐上游）
        self.assertEqual(len(received), 1)
        self.assertEqual(received[0]["type"], "info")
        self.assertTrue(received[0]["name"])     # 以 fiber 派生名记录

    def test_default_buffering_exporter(self):
        ctx = Context()
        ctx.logger("buffered").info("one")
        ctx.logger("buffered").info("two")
        self.assertEqual(len(ctx.logger.buffer), 2)
        self.assertEqual(ctx.logger.buffer[1]["name"], "buffered")

    def test_exporter_disposed_with_fiber(self):
        root = Context()
        received = []
        fiber = root.plugin({
            "name": "lp",
            "apply": lambda ctx, cfg: ctx.logger.exporter({"export": received.append}),
        })
        self.assertEqual(fiber.state, FiberState.ACTIVE)
        root.logger("tester").info("x")
        self.assertEqual(len(received), 1)
        fiber.dispose()
        root.logger("tester").info("y")
        self.assertEqual(len(received), 1)       # 导出器随 fiber 注销

    def test_intercept_config_feeds_logger(self):
        ctx = Context()
        ctx = ctx.intercept("logger", {"name": "wired"})
        logger = ctx.logger("")     # 经 ctx.logger 视图：intercept 从访问方 ctx 解析
        self.assertEqual(logger.name, "wired")   # name 经 intercept 配置注入


class TestConfigValidation(unittest.TestCase):
    """core/schema.py：resolveConfig + ValidationError（对齐 fiber.ts）。"""

    def test_valid_config_normalized(self):
        from miniharness.core.schema import S, resolve_config
        schema = S.object({"name": S.string(), "n": S.natural().default(3)})
        cfg = resolve_config(schema, {"name": "x"})
        self.assertEqual(cfg, {"name": "x", "n": 3})

    def test_invalid_config_raises_aggregated_message(self):
        from miniharness.core.schema import S, ValidationError, resolve_config
        schema = S.object({"name": S.string(), "sub": S.object({"k": S.natural()})})
        with self.assertRaises(ValidationError) as cm:
            resolve_config(schema, {"name": "x", "sub": {"k": "bad"}})
        msg = str(cm.exception)
        self.assertTrue(msg.startswith("invalid config:\n"))
        self.assertIn("  - ", msg)
        self.assertIn("(at sub.k)", msg)      # issue.path 逐段拼接
        self.assertEqual(cm.exception.name, "ValidationError")
        self.assertIsInstance(cm.exception, TypeError)

    def test_no_schema_passthrough(self):
        from miniharness.core.schema import resolve_config
        self.assertEqual(resolve_config(None, {"a": 1}), {"a": 1})


if __name__ == "__main__":
    unittest.main()