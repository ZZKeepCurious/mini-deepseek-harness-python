"""dsh-scope 全协议测试（对齐 packages/core/scope：index.ts + store.ts）。

覆盖：ScopeKey 身份比较/可弱引用、scopeParents 图（bind/link/环/重绑/链）、
create_scope 铸 scope（fiber-backed + Scope 包装 + 自动父绑定 + 同步/异步拆解）、
scope_target 事件载波（未打标全局、键或键祖先接纳、旁支/后裔隔离、base 不暴露）、
carrier 派发路由（this_arg）、NamedEntries/AnonymousEntries（插入序/幂等撤销/
活跃迭代器/排空脱离）、ScopedLayers（全局层、精确层、链读、合并遮蔽、层回收+通知）。
"""
from __future__ import annotations

import asyncio
import unittest

from miniharness.core.dsh_scope import (
    AnonymousEntries,
    NamedEntries,
    ScopedLayers,
    ScopeKey,
    bind_scope_parent,
    carrier_key_of,
    create_scope,
    is_scope_carrier,
    link_scope_parent,
    scope_chain_of,
    scope_of,
    scope_parent_of,
    scope_target,
)
from miniharness.core.scope import Context


def _dup_error(name: str) -> Exception:
    return RuntimeError(f"duplicate entry {name}")


class TestScopeKey(unittest.TestCase):
    def test_identity_compared(self):
        a = ScopeKey()
        b = ScopeKey()
        self.assertIsNot(a, b)
        self.assertFalse(a == b)

    def test_weak_referenceable(self):
        import weakref
        k = ScopeKey()
        ref = weakref.ref(k)
        self.assertIs(ref(), k)


class TestScopeParents(unittest.TestCase):
    def test_link_and_read(self):
        root = ScopeKey()
        child = ScopeKey()
        grand = ScopeKey()
        bind_scope_parent(child, root)
        bind_scope_parent(grand, child)
        self.assertIs(scope_parent_of(child), root)
        self.assertIs(scope_parent_of(grand), child)
        self.assertIsNone(scope_parent_of(root))

    def test_chain_nearest_first(self):
        root = ScopeKey()
        child = ScopeKey()
        grand = ScopeKey()
        bind_scope_parent(child, root)
        bind_scope_parent(grand, child)
        chain = scope_chain_of(grand)
        self.assertEqual(chain, [grand, child, root])
        self.assertEqual(scope_chain_of(root), [root])
        self.assertEqual(scope_chain_of(None), [])

    def test_bind_twice_rejected(self):
        root = ScopeKey()
        child = ScopeKey()
        bind_scope_parent(child, root)
        with self.assertRaisesRegex(RuntimeError, "already bound"):
            bind_scope_parent(child, ScopeKey())

    def test_cycle_rejected(self):
        a = ScopeKey()
        b = ScopeKey()
        c = ScopeKey()
        bind_scope_parent(b, a)
        bind_scope_parent(c, b)
        with self.assertRaisesRegex(RuntimeError, "cycle"):
            link_scope_parent(a, c)

    def test_rebind(self):
        root = ScopeKey()
        new_root = ScopeKey()
        child = ScopeKey()
        binding = bind_scope_parent(child, root)
        self.assertIs(scope_parent_of(child), root)
        binding.rebind(new_root)
        self.assertIs(scope_parent_of(child), new_root)


class TestCreateScope(unittest.TestCase):
    def test_scope_wrapper_shape(self):
        root = Context(name="root")
        scope = root.create_scope("agent:1")
        self.assertIsNotNone(scope.ctx)
        self.assertTrue(callable(scope.raw_dispose))
        self.assertTrue(callable(scope.dispose))
        # fiber-backed：scope 的上下文有独立 fiber
        self.assertIsNot(scope.ctx.fiber, root.fiber)
        self.assertEqual(scope.ctx.fiber.state, "active")

    def test_scope_of_resolves_own_and_inherited(self):
        root = Context(name="root")
        scope = root.create_scope("agent:1")
        key = scope.scope_key
        self.assertIsNotNone(key)
        self.assertIs(scope_of(scope.ctx), key)
        self.assertIs(scope_of(scope), key)
        self.assertIsNone(root.scope_key)
        self.assertIsNone(scope_of(root))
        # 作用域下挂载的插件上下文也归属该作用域（原型继承等价）
        child = scope.extend(name="under")
        self.assertIs(scope_of(child), key)

    def test_wrapper_delegates_context_api(self):
        root = Context(name="root")
        scope = root.create_scope("agent:1")
        self.assertIs(scope.parent, root)
        served = []
        scope.on("e", lambda p: served.append(p))
        scope.emit("e", "x")
        self.assertEqual(served, ["x"])

    def test_nested_scope_auto_binds_parent(self):
        root = Context(name="root")
        parent = root.create_scope("agent:parent")
        child = parent.create_scope("subagent:child")
        self.assertIs(scope_parent_of(child.scope_key), parent.scope_key)
        # 兄弟嵌套不会串链：child 只链到 parent，不链到其它作用域
        other = root.create_scope("agent:other")
        self.assertIsNone(scope_parent_of(other.scope_key))

    def test_explicit_parent(self):
        root = Context(name="root")
        a = root.create_scope("agent:a")
        b = root.create_scope("agent:b")
        c = root.create_scope("agent:c", parent=a.scope_key)
        self.assertIs(scope_parent_of(c.scope_key), a.scope_key)
        self.assertIsNot(scope_parent_of(c.scope_key), b.scope_key)

    def test_dispose_sync_path_without_loop(self):
        root = Context(name="root")
        scope = root.create_scope("agent:1")
        scope.on("e", lambda p: None)
        result = scope.dispose()
        self.assertIsNone(result)
        with self.assertRaisesRegex(RuntimeError, "已销毁"):
            scope.on("e", lambda p: None)

    def test_dispose_is_idempotent(self):
        root = Context(name="root")
        scope = root.create_scope("agent:1")
        scope.dispose()
        scope.dispose()
        self.assertEqual(scope.ctx.fiber.state, "disposed")

    def test_dispose_async_quiesces(self):
        async def main():
            root = Context(name="root")
            scope = root.create_scope("agent:1")
            done = []
            scope.ctx.effect(lambda: done.append("setup"), "e")

            async def body():
                done.append("run")

            scope.ctx.effect(lambda: body(), "async")
            result = scope.dispose()
            if result is not None:
                await result
            self.assertEqual(done, ["setup", "run"])

        asyncio.run(main())


class TestScopeTargetCarrier(unittest.TestCase):
    def test_carrier_markers(self):
        root = Context(name="root")
        session = object()
        carrier = scope_target(session, None)
        self.assertTrue(is_scope_carrier(carrier))
        self.assertIsNone(carrier_key_of(carrier))
        self.assertFalse(is_scope_carrier(session))
        self.assertIsNone(carrier_key_of(session))

    def test_carrier_hides_base(self):
        base = Context(name="base")
        base.mark = "secret"
        carrier = scope_target(base, None)
        self.assertFalse(hasattr(carrier, "mark"))

    def test_admit_untagged_always(self):
        root = Context(name="root")
        key = ScopeKey()
        carrier = scope_target(object(), key)
        self.assertTrue(carrier.admit(root))

    def test_admit_tagged_by_key_or_ancestor(self):
        root = Context(name="root")
        agent = root.create_scope("agent:1")
        child = agent.create_scope("subagent:child")
        other = root.create_scope("agent:other")
        carrier = scope_target(object(), agent.scope_key)
        self.assertTrue(carrier.admit(agent.ctx))       # 键自身（监听 ctx 挂载点=键）
        # 事件只向上流：挂后裔作用域标签的监听 ctx（标签=子键）不在键的祖先链上 → 排除
        self.assertFalse(carrier.admit(child.ctx))
        self.assertFalse(carrier.admit(other.ctx))       # 旁支排除

    def test_admit_parent_tagged_listener(self):
        root = Context(name="root")
        parent = root.create_scope("agent:parent")
        child = parent.create_scope("subagent:child")
        carrier = scope_target(object(), child.scope_key)
        # 挂父作用域标签的监听 ctx：父键在载波键的祖先链上 → 接纳
        self.assertTrue(carrier.admit(parent.ctx))

    def test_base_filter_respected(self):
        base = Context(name="base")
        base._context_filter = lambda ctx: False
        carrier = scope_target(base, None)
        root = Context(name="root")
        self.assertFalse(carrier.admit(root))


class TestCarrierDispatch(unittest.TestCase):
    def test_emit_routes_by_carrier(self):
        root = Context(name="root")
        agent = root.create_scope("agent:1")
        sibling = root.create_scope("agent:2")
        agent_seen = []
        sibling_seen = []
        global_seen = []
        root.on("ev", lambda p: global_seen.append(p))          # 未打标 → 全局
        agent.on("ev", lambda p: agent_seen.append(p))           # 打标 agent
        sibling.on("ev", lambda p: sibling_seen.append(p))       # 打标 sibling
        carrier = scope_target(object(), agent.scope_key)
        root.emit("ev", "x", this_arg=carrier)
        self.assertEqual(agent_seen, ["x"])
        self.assertEqual(sibling_seen, [])
        self.assertEqual(global_seen, ["x"])

    def test_global_hook_bypasses_carrier(self):
        root = Context(name="root")
        agent = root.create_scope("agent:1")
        sibling = root.create_scope("agent:2")
        forced = []
        sibling.on("ev", lambda p: forced.append(p), global_=True)
        carrier = scope_target(object(), agent.scope_key)
        root.emit("ev", "x", this_arg=carrier)
        self.assertEqual(forced, ["x"])

    def test_parent_tagged_receives_child_carrier_event(self):
        root = Context(name="root")
        parent = root.create_scope("agent:parent")
        child = parent.create_scope("subagent:child")
        parent_seen = []
        parent.on("ev", lambda p: parent_seen.append(p))
        carrier = scope_target(object(), child.scope_key)
        root.emit("ev", "y", this_arg=carrier)
        self.assertEqual(parent_seen, ["y"])

    def test_no_carrier_uses_ancestor_walk(self):
        root = Context(name="root")
        agent = root.create_scope("agent:1")
        seen = []
        root.on("ev", lambda p: seen.append(p))
        agent.emit("ev", "z")
        self.assertEqual(seen, ["z"])

    def test_non_carrier_this_arg_uses_flat_hooks(self):
        root = Context(name="root")
        seen = []
        root.on("ev", lambda p: seen.append(p))
        root.emit("ev", "w", this_arg=object())
        self.assertEqual(seen, ["w"])


class TestNamedEntries(unittest.TestCase):
    def test_insert_get_undo(self):
        table = NamedEntries(_dup_error)
        self.assertTrue(table.isEmpty())
        undo = table.insert("a", 1)
        self.assertFalse(table.isEmpty())
        self.assertEqual(table.get("a"), 1)
        self.assertTrue(table.has("a"))
        self.assertEqual(list(table.keys()), ["a"])
        self.assertEqual(list(table.values()), [1])
        self.assertEqual(list(table.entries()), [("a", 1)])
        undo()
        self.assertTrue(table.isEmpty())
        self.assertIsNone(table.get("a"))

    def test_undo_is_idempotent(self):
        table = NamedEntries(_dup_error)
        undo = table.insert("a", 1)
        undo()
        undo()
        self.assertTrue(table.isEmpty())

    def test_duplicate_fails_loud(self):
        table = NamedEntries(_dup_error)
        table.insert("a", 1)
        with self.assertRaisesRegex(RuntimeError, "duplicate entry a"):
            table.insert("a", 2)

    def test_full_drain_detaches_iterators(self):
        table = NamedEntries(_dup_error)
        u1 = table.insert("a", 1)
        u2 = table.insert("b", 2)
        live = list(table.keys())
        u1()
        u2()
        self.assertEqual(live, ["a", "b"])     # 排空前迭代器已捕获表代
        table.insert("c", 3)
        self.assertEqual(list(table.keys()), ["c"])


class TestAnonymousEntries(unittest.TestCase):
    def test_append_undo(self):
        table = AnonymousEntries()
        self.assertTrue(table.isEmpty())
        u1 = table.append("x")
        u2 = table.append("x")                 # 相等值仍是独立注册
        self.assertEqual(sorted(table.values()), ["x", "x"])
        u1()
        self.assertEqual(list(table.values()), ["x"])
        u2()
        self.assertTrue(table.isEmpty())
        u2()                                   # 幂等

    def test_full_drain_detaches_iterators(self):
        table = AnonymousEntries()
        u1 = table.append(1)
        u2 = table.append(2)
        live = list(table.values())
        u1()
        u2()
        self.assertEqual(live, [1, 2])
        table.append(3)
        self.assertEqual(list(table.values()), [3])


class TestScopedLayers(unittest.TestCase):
    def _layers(self):
        return ScopedLayers(
            lambda scope: NamedEntries(_dup_error),
            on_change=lambda: None,
        )

    def test_global_layer_always_present(self):
        layers = self._layers()
        self.assertIsNotNone(layers.global_layer)
        self.assertTrue(layers.global_layer.isEmpty())

    def test_effect_creates_scoped_layer(self):
        root = Context(name="root")
        agent = root.create_scope("agent:1")
        layers = self._layers()
        changes = []
        layers._on_change = lambda: changes.append(True)
        disposer = layers.effect(
            agent.ctx, lambda layer: layer.insert("a", 1), "register")
        self.assertEqual(changes, [True])                     # 通知恰一次（effect 内）
        self.assertEqual(layers.peek(agent.scope_key).get("a"), 1)
        self.assertEqual(layers.merge(agent.scope_key, lambda l: l), {"a": 1})
        disposer()                                            # undo + 层回收 + 通知
        self.assertEqual(changes, [True, True])
        self.assertIsNone(layers.peek(agent.scope_key))

    def test_effect_uses_global_for_untagged(self):
        root = Context(name="root")
        layers = self._layers()
        disposer = layers.effect(root, lambda layer: layer.insert("a", 1), "register")
        self.assertEqual(layers.global_layer.get("a"), 1)
        disposer()
        self.assertTrue(layers.global_layer.isEmpty())

    def test_peek_is_chain_blind(self):
        root = Context(name="root")
        parent = root.create_scope("agent:parent")
        child = parent.create_scope("subagent:child")
        layers = self._layers()
        layers.effect(parent.ctx, lambda layer: layer.insert("a", 1), "p")
        self.assertIsNone(layers.peek(child.scope_key))       # 不捡祖先
        self.assertEqual(layers.merge(child.scope_key, lambda l: l), {"a": 1})

    def test_merge_child_wins(self):
        root = Context(name="root")
        parent = root.create_scope("agent:parent")
        child = parent.create_scope("subagent:child")
        layers = self._layers()
        layers.effect(parent.ctx, lambda layer: layer.insert("a", 1), "p")
        layers.effect(child.ctx, lambda layer: layer.insert("a", 2), "c")
        merged = layers.merge(child.scope_key, lambda l: l)
        self.assertEqual(merged["a"], 2)

    def test_chain_layers_order(self):
        root = Context(name="root")
        parent = root.create_scope("agent:parent")
        child = parent.create_scope("subagent:child")
        layers = self._layers()
        layers.effect(parent.ctx, lambda layer: layer.insert("p", 1), "p")
        layers.effect(child.ctx, lambda layer: layer.insert("c", 1), "c")
        chain = layers.chain_layers(child.scope_key)
        self.assertEqual(len(chain), 2)                       # 祖先在前、精确在后
        self.assertEqual(chain[0].get("p"), 1)
        self.assertEqual(chain[1].get("c"), 1)

    def test_action_throw_recycles_only_empty_created_layer(self):
        root = Context(name="root")
        agent = root.create_scope("agent:1")
        layers = self._layers()

        # 抛出前已插入条目 → 层非空 → 保留（上游 store.ts:253 仅回收 created+empty）
        def bad_with_entry(layer):
            layer.insert("a", 1)
            raise RuntimeError("boom")

        with self.assertRaisesRegex(RuntimeError, "boom"):
            layers.effect(agent.ctx, bad_with_entry, "register")
        self.assertIsNotNone(layers.peek(agent.scope_key))
        self.assertEqual(layers.peek(agent.scope_key).get("a"), 1)

        # 抛出时层仍空 → 回收（created+empty 才回收）
        other = root.create_scope("agent:2")

        def bad_empty(layer):
            raise RuntimeError("boom2")

        with self.assertRaisesRegex(RuntimeError, "boom2"):
            layers.effect(other.ctx, bad_empty, "register")
        self.assertIsNone(layers.peek(other.scope_key))

    def test_effect_returns_ctx_effect_disposer(self):
        root = Context(name="root")
        agent = root.create_scope("agent:1")
        layers = self._layers()
        disposer = layers.effect(agent.ctx, lambda layer: layer.insert("a", 1), "register")
        self.assertTrue(callable(disposer))


if __name__ == "__main__":
    unittest.main()