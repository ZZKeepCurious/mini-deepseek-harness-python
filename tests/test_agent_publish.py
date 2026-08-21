"""Phase 4b：publish/detach 生命周期 + agent 事件载波路由（对齐上游 agent-loop）。

覆盖：publish 进店/公告/agent/session-start、announce 抛错回滚、缺 sessions
服务 fail loud、dispose 取消在途回合 + 拆 scope + detach 会话（幂等）；
agent/* 事件经 loop 载波派发——root 与祖先作用域监听器接收，兄弟作用域隔离。
"""
from __future__ import annotations

import unittest

from miniharness.core.scope import Context
from miniharness.core.session_store import install_sessions
from miniharness.core.session import Session
from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.llm import FakeLlmAdapter
from miniharness.core.tools import ToolRegistry


def _loop(ctx: Context, session_id: str = "s1") -> AgentLoop:
    reg = ToolRegistry(ctx)
    return AgentLoop(Session(session_id), FakeLlmAdapter(final_text="ok"), reg, ctx,
                     system_prompt="你是代理。")


class TestPublishLifecycle(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        self.store = install_sessions(self.ctx)

    def test_publish_enters_store_and_announces(self):
        loop = _loop(self.ctx)
        created = []
        self.ctx.on("session/created", lambda p: created.append(p["session"].session_id))
        returned = loop.publish()
        self.assertIs(returned, loop)
        self.assertIs(self.store.get("s1"), loop.session)
        self.assertIn("s1", created)

    def test_publish_emits_session_start_with_source(self):
        loop = _loop(self.ctx)
        starts = []
        self.ctx.on("agent/session-start",
                    lambda p: starts.append((p["agent"], p["source"])))
        loop.publish(source="resume")
        self.assertEqual(len(starts), 1)
        self.assertIs(starts[0][0], loop)
        self.assertEqual(starts[0][1], "resume")

    def test_dispose_detaches_session_and_emits_disposed(self):
        loop = _loop(self.ctx)
        disposed = []
        self.ctx.on("session/disposed", lambda p: disposed.append(p["session"].session_id))
        loop.publish()
        loop.dispose()
        self.assertIsNone(self.store.get("s1"))
        self.assertIn("s1", disposed)

    def test_announce_throw_rolls_back_enter(self):
        loop = _loop(self.ctx)
        disposer = self.ctx.on("session/created", lambda p: (_ for _ in ()).throw(RuntimeError("boom")))
        with self.assertRaisesRegex(RuntimeError, "boom"):
            loop.publish()
        self.assertIsNone(self.store.get("s1"))   # enter 已回滚：会话未进店
        disposer()
        loop.publish()                            # 回滚后可重新发布
        self.assertIs(self.store.get("s1"), loop.session)
        loop.dispose()

    def test_publish_without_sessions_service_fails_loud(self):
        ctx = Context(name="bare")
        loop = _loop(ctx)
        with self.assertRaisesRegex(RuntimeError, "publish requires the sessions service"):
            loop.publish()

    def test_dispose_idempotent_and_unpublished_ok(self):
        reg = ToolRegistry(self.ctx)
        bare = AgentLoop(Session("s1"), FakeLlmAdapter(final_text="ok"), reg, self.ctx)
        bare.dispose()                            # 未 publish：dispose 仅拆 scope
        bare.dispose()                            # 幂等
        self.assertIsNone(self.store.get("s1"))
        published = AgentLoop(Session("s2"), FakeLlmAdapter(final_text="ok"),
                              reg, self.ctx).publish()
        published.dispose()
        published.dispose()                       # detach 单发 + scope.dispose 幂等
        self.assertIsNone(self.store.get("s2"))


class TestAgentEventCarrierRouting(unittest.TestCase):
    """agent/* 事件经 loop 载波（scopeTarget(agent, loop scope 键)）派发：
    未打标 root 监听器全收；打标监听器按"载波键或其祖先"接纳——兄弟隔离。"""

    def _run_turn(self, ctx: Context, mid: Context | None = None) -> AgentLoop:
        base = mid if mid is not None else ctx
        loop = _loop(base)
        loop.followup("hi")
        return loop

    def test_root_listener_receives_sibling_isolated(self):
        ctx = Context(name="root")
        install_sessions(ctx)
        sibling = ctx.create_scope("unrelated")
        seen_root, seen_sibling = [], []
        ctx.on("agent/status", lambda p: seen_root.append(p["status"]))
        sibling.on("agent/status", lambda p: seen_sibling.append(p["status"]))
        self._run_turn(ctx)
        self.assertIn("running", seen_root)
        self.assertEqual(seen_sibling, [])        # 兄弟作用域不收他人事件

    def test_loop_own_scope_listener_receives_own_events(self):
        ctx = Context(name="root")
        install_sessions(ctx)
        mid = ctx.create_scope("mid")
        seen_mid, seen_own = [], []
        mid.on("agent/status", lambda p: seen_mid.append(p["status"]))
        loop = _loop(mid)
        loop.ctx.on("agent/status", lambda p: seen_own.append(p["status"]))
        loop.followup("hi")
        self.assertTrue(seen_own)                 # 自身作用域监听器收到
        self.assertTrue(seen_mid)                 # 祖先作用域（载波键链上）收到

    def test_carrier_events_reach_global_listeners_only_upward(self):
        ctx = Context(name="root")
        install_sessions(ctx)
        mid = ctx.create_scope("mid")
        other = ctx.create_scope("other")
        seen_other = []
        other.on("agent/error", lambda p: seen_other.append(p))
        loop = _loop(mid)

        class Boom(FakeLlmAdapter):
            async def stream(self, messages, tools, signal=None):
                raise RuntimeError("x")
                yield  # pragma: no cover

        loop.adapter = Boom(final_text="ok")
        with self.assertRaises(Exception):
            loop.followup("hi")
        self.assertEqual(seen_other, [])          # 兄弟作用域不收 error
