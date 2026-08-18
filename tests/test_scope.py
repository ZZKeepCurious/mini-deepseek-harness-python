"""地基①：scope 层叠/生命周期（对齐上游 dsh-scope + SessionStore owner 路由）。

覆盖：create_scope 身份键、owner scope 路由（兄弟/后代隔离、祖先接收）、
作用域拆解（dispose 逆序回滚 + 拒绝注册）、AgentLoop 自有作用域、嵌套子作用域。
"""
from __future__ import annotations

import unittest

from miniharness.core.scope import Context
from miniharness.core.session_store import SessionStore
from miniharness.core.session import Session, create_message, text_block
from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.llm import FakeLlmAdapter
from miniharness.core.tools import ToolRegistry


def _store_on(ctx: Context) -> SessionStore:
    store = SessionStore(ctx)
    ctx.provide("sessions", store)
    return store


def _user_message(text: str = "hi") -> dict:
    return create_message("user", [text_block(text)], {"kind": "user"})


class TestCreateScope(unittest.TestCase):
    def test_scope_gets_identity_key(self):
        root = Context(name="root")
        scope = root.create_scope("agent:1")
        self.assertIsNone(root.scope_key)
        self.assertIsNotNone(scope.scope_key)
        self.assertTrue(scope.is_scope())
        self.assertFalse(root.is_scope())
        self.assertIs(scope.parent, root)

    def test_scope_inherits_services_and_listeners(self):
        root = Context(name="root")
        root.provide("jobs", object())
        scope = root.create_scope("agent:1")
        self.assertIsNotNone(scope.inject("jobs"))

        seen = []
        root.on("event", lambda p: seen.append(p))
        scope.emit("event", "x")   # 子作用域派发 → 祖先监听器收到
        self.assertEqual(seen, ["x"])

    def test_scope_dispose_unwinds_registrations(self):
        root = Context(name="root")
        scope = root.create_scope("agent:1")
        served = []
        scope.on("agent/status", lambda p: served.append(p))
        scope.emit("agent/status", "a")
        self.assertEqual(served, ["a"])
        scope.dispose()
        with self.assertRaisesRegex(RuntimeError, "已销毁"):
            scope.on("agent/status", lambda p: None)

    def test_scope_dispose_leaves_parent_alive(self):
        root = Context(name="root")
        scope = root.create_scope("agent:1")
        scope.dispose()
        root.on("e", lambda p: None)   # 父不受影响


class TestSessionStoreOwnerRouting(unittest.TestCase):
    def test_sibling_scopes_are_isolated(self):
        root = Context(name="root")
        store = _store_on(root)
        scope_a = root.create_scope("agent:a")
        scope_b = root.create_scope("agent:b")

        seen_a: list[str] = []
        seen_b: list[str] = []
        global_seen: list[str] = []
        root.on("session/event", lambda p: global_seen.append(p["session"].session_id))
        scope_a.on("session/event", lambda p: seen_a.append(p["session"].session_id))
        scope_b.on("session/event", lambda p: seen_b.append(p["session"].session_id))

        s1 = store.create("s1", owner_ctx=scope_a)
        s1.append("user/message", _user_message(), surfaceOp="append")
        self.assertEqual(seen_a, ["s1"])
        self.assertEqual(seen_b, [])                # 兄弟 scope 不收到

        s2 = store.create("s2", owner_ctx=scope_b)
        s2.append("user/message", _user_message(), surfaceOp="append")
        self.assertEqual(seen_a, ["s1"])            # a 不收到 b 的事件
        self.assertEqual(seen_b, ["s2"])
        # 全局（祖先）监听器收到全部
        self.assertEqual(global_seen, ["s1", "s2"])

    def test_global_listener_receives_all_owners(self):
        root = Context(name="root")
        store = _store_on(root)
        scope_a = root.create_scope("agent:a")
        scope_b = root.create_scope("agent:b")
        created: list[str] = []
        root.on("session/created", lambda p: created.append(p["session"].session_id))
        store.create("s1", owner_ctx=scope_a)
        store.create("s2", owner_ctx=scope_b)
        self.assertEqual(created, ["s1", "s2"])

    def test_default_owner_is_store_ctx(self):
        root = Context(name="root")
        store = _store_on(root)
        seen = []
        root.on("session/created", lambda p: seen.append(p["session"].session_id))
        s = store.create("s1")
        self.assertEqual(seen, ["s1"])

    def test_flush_and_disposed_route_by_owner(self):
        root = Context(name="root")
        store = _store_on(root)
        scope = root.create_scope("agent:a")
        flushes = []
        disposals = []
        scope.on("session/flush", lambda p: flushes.append(p["session"].session_id))
        scope.on("session/disposed", lambda p: disposals.append(p["session"].session_id))
        s = store.prepare("s1")
        detach = store.enter(s, owner_ctx=scope)
        store.announce(s)
        self.assertEqual(store.flush(s), 1)
        detach()
        self.assertEqual(flushes, ["s1"])
        self.assertEqual(disposals, ["s1"])

    def test_subagent_scope_nests_and_sees_parent_events(self):
        root = Context(name="root")
        store = _store_on(root)
        parent_scope = root.create_scope("agent:parent")
        child_scope = parent_scope.create_scope("subagent:child")
        parent_seen = []
        child_seen = []
        parent_scope.on("session/event", lambda p: parent_seen.append(p["session"].session_id))
        child_scope.on("session/event", lambda p: child_seen.append(p["session"].session_id))
        s = store.create("s1", owner_ctx=child_scope)
        s.append("user/message", _user_message(), surfaceOp="append")
        # 父作用域监听器（祖先）也收到子作用域会话的事件
        self.assertIn("s1", parent_seen)
        self.assertIn("s1", child_seen)


class TestAgentLoopScope(unittest.TestCase):
    def _loop(self, ctx: Context) -> AgentLoop:
        reg = ToolRegistry(ctx)
        return AgentLoop(Session("s1"), FakeLlmAdapter(final_text="ok"), reg, ctx)

    def test_loop_owns_a_scope(self):
        ctx = Context(name="root")
        loop = self._loop(ctx)
        self.assertIsNotNone(loop.ctx.scope_key)
        self.assertIs(loop.ctx.parent, ctx)
        self.assertIsNot(loop.ctx, ctx)

    def test_loop_scope_reaches_entry_listeners(self):
        ctx = Context(name="root")
        loop = self._loop(ctx)
        seen = []
        ctx.on("agent/pre-step", lambda p, nxt: seen.append("entry"))
        # 事件在 loop.ctx（作用域）上派发时，祖先链监听器照常收到
        loop.ctx.waterfall("agent/pre-step", {})
        self.assertEqual(seen, ["entry"])

    def test_loop_dispose_rejects_further_registration(self):
        ctx = Context(name="root")
        loop = self._loop(ctx)
        loop.dispose()
        with self.assertRaisesRegex(RuntimeError, "已销毁"):
            loop.ctx.on("agent/pre-step", lambda p, nxt: None)
