"""Phase 4c（R4）：轻量 agent 实例注册表 ctx.agents + assertLive 边界。

对齐 packages/core/agent/src/index.ts 的 AgentRegistry：
  * install_agents 幂等装配（重复 provide / 收养现存服务）
  * publish() 注册 live 实例；id 与会话不符 / 同 id 碰撞 → fail loud
  * get/list/roots/is_owned_by 查询面；agent/created、agent/disposed 事件
  * dispose 自动注销（scope effect + 显式 disposer，agent/disposed 补发）
  * assert_live：登记实例通过、陈旧/重复实例 AgentNotLive
  * 边界：未安装 agents 服务的裸装配不强制（assert_live_agent 为 no-op）
  * jobs/_assert_access、goal/_prepare_mutation 共用 assertLive 边界
"""
from __future__ import annotations

import threading
import unittest

from miniharness.core.scope import Context
from miniharness.core.session_store import install_sessions
from miniharness.core.agents import (
    AgentNotLive,
    AgentRegistry,
    assert_live_agent,
    install_agents,
)
from miniharness.core.session import Session
from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.llm import FakeLlmAdapter
from miniharness.core.tools import ToolRegistry
from miniharness.jobs import install_jobs


def _loop(ctx: Context, session_id: str = "s1") -> AgentLoop:
    reg = ctx.get("tools") or ToolRegistry(ctx)
    return AgentLoop(Session(session_id), FakeLlmAdapter(final_text="ok"), reg, ctx,
                     system_prompt="你是代理。")


class _Shim:
    """register() 最小鸭子对象：暴露 id / session / ctx / carrier。"""

    def __init__(self, aid: str, session_id: str):
        self.id = aid
        self.session = type("S", (), {"session_id": session_id})()
        self.ctx = Context(name="shim")
        self._carrier = self.ctx


class TestInstallAgents(unittest.TestCase):
    def test_idempotent_installs_once(self):
        ctx = Context(name="root")
        a1 = install_agents(ctx)
        a2 = install_agents(ctx)
        self.assertIs(a1, a2)
        self.assertIs(ctx.get("agents"), a1)

    def test_adopts_existing_service(self):
        ctx = Context(name="root")
        reg = AgentRegistry(ctx)
        self.assertIs(install_agents(ctx), reg)
        self.assertIs(ctx.get("agents"), reg)


class TestRegisterQuery(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        install_sessions(self.ctx)
        install_agents(self.ctx)

    def test_register_then_query(self):
        loop = _loop(self.ctx)
        loop.publish()
        agents = self.ctx.get("agents")
        self.assertIs(agents.get("s1"), loop)
        self.assertEqual(agents.list(), [loop])
        self.assertEqual(agents.roots(), [loop])
        self.assertTrue(agents.is_owned_by("s1", None))
        loop.dispose()

    def test_register_emits_created_and_disposed(self):
        loop = _loop(self.ctx, "s1")
        created, disposed = [], []
        self.ctx.on("agent/created", lambda p: created.append(p["agent"].id))
        self.ctx.on("agent/disposed", lambda p: disposed.append(p["agent"].id))
        loop.publish()
        self.assertEqual(created, ["s1"])
        loop.dispose()
        self.assertEqual(disposed, ["s1"])
        self.assertIsNone(self.ctx.get("agents").get("s1"))

    def test_register_id_mismatch_fails_loud(self):
        agents = self.ctx.get("agents")
        shim = _Shim(aid="s1", session_id="s9")      # id 与会话不符
        with self.assertRaisesRegex(RuntimeError, "does not match session id"):
            agents.register(shim)
        self.assertIsNone(agents.get("s1"))

    def test_register_collision_fails_loud(self):
        agents = self.ctx.get("agents")
        detach = agents.register(_Shim("s1", "s1"))
        with self.assertRaisesRegex(RuntimeError, "is already registered"):
            agents.register(_Shim("s1", "s1"))
        self.assertIsNotNone(agents.get("s1"))   # 首次登记保留
        detach()
        self.assertIsNone(agents.get("s1"))

    def test_dispose_auto_unregisters_even_without_explicit_disposer(self):
        loop = _loop(self.ctx, "s1")
        loop.publish()
        self.assertIsNotNone(self.ctx.get("agents").get("s1"))
        loop.dispose()
        self.assertIsNone(self.ctx.get("agents").get("s1"))


class TestAssertLive(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        install_sessions(self.ctx)
        install_agents(self.ctx)

    def test_live_registered_passes(self):
        loop = _loop(self.ctx, "s1")
        loop.publish()
        assert_live_agent(loop)          # not raise
        self.ctx.get("agents").assert_live(loop)
        loop.dispose()

    def test_stale_instance_rejected(self):
        live = _loop(self.ctx, "s1")
        live.publish()
        stale = _loop(self.ctx, "s1")    # 未发布/被占：非 registry 登记实例
        with self.assertRaises(AgentNotLive):
            assert_live_agent(stale)
        with self.assertRaises(AgentNotLive):
            assert_live_agent(live if False else stale)
        live.dispose()

    def test_disposed_instance_rejected(self):
        loop = _loop(self.ctx, "s1")
        loop.publish()
        loop.dispose()
        with self.assertRaises(AgentNotLive):
            assert_live_agent(loop)

    def test_bare_ctx_without_agents_is_noop(self):
        ctx = Context(name="bare")
        install_sessions(ctx)
        loop = _loop(ctx, "s1")
        loop.publish()                   # 未安装 agents：publish 跳过注册
        assert_live_agent(loop)          # no-op，不抛
        self.assertIsNone(ctx.get("agents"))
        loop.dispose()


class TestJobsGoalBoundary(unittest.TestCase):
    def test_jobs_reject_stale_caller(self):
        ctx = Context(name="root")
        install_sessions(ctx)
        install_agents(ctx)
        registry = install_jobs(ctx)
        live = _loop(ctx, "s1")
        live.publish()
        # 直接注入一个完整形态的 job 条目（等价 registry.start 注册结果）
        job: dict = {
            "id": "bash-1", "kind": "bash", "label": "x", "outputLimitBytes": None,
            "owner": None, "cancel": lambda *a: None, "readOutput": None,
            "status": "running", "detail": None, "output": None,
            "startedAt": 0, "finishedAt": None, "reported": False, "waiters": 0,
            "settled": threading.Event(), "done": None,
        }
        registry._store["bash-1"] = job
        registry.get("bash-1", live)          # live 调用方通过
        stale = _loop(ctx, "s1")              # 同 id 未登记实例
        with self.assertRaises(AgentNotLive):
            registry.get("bash-1", stale)
        with self.assertRaises(AgentNotLive):
            registry.kill("bash-1", stale)
        with self.assertRaises(AgentNotLive):
            registry.read("bash-1", stale)
        with self.assertRaises(AgentNotLive):
            registry.wait("bash-1", 5, stale)
        live.dispose()

    def test_goal_reject_stale_agent(self):
        from miniharness.core.agents import assert_live_agent as al
        ctx = Context(name="root")
        install_sessions(ctx)
        install_agents(ctx)
        from miniharness.goal.service import GoalService
        svc = GoalService(ctx)
        live = _loop(ctx, "s1")
        live.publish()
        stale = _loop(ctx, "s1")
        with self.assertRaises(AgentNotLive):
            svc._prepare_mutation(stale)
        svc._prepare_mutation(live)      # live 通过
        live.dispose()


if __name__ == "__main__":
    unittest.main()
