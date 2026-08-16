"""A5 验收：plan mode（log-only 状态 + prompt section 注入）。

覆盖：resolve_config fail-loud、fold_plan_mode、plan/mode 词汇表与回放、
systemPrompt 分节服务、PlanModeController set 四态、in-turn pre-step 提交、
reject/aborted 不提交、plan:policy 节注入、模式切换叙述、循环集成。
"""

import unittest
from types import SimpleNamespace

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import KNOWN_TYPES, Session
from miniharness.core.system_prompt import SYSTEM_PROMPT_SERVICE, SystemPromptService, install_system_prompt
from miniharness.core.tools import Tool, ToolRegistry
from miniharness.llm import FakeLlmAdapter
from miniharness.plan import (
    PLAN_POLICY_ORDER,
    PLAN_POLICY_SECTION,
    fold_plan_mode,
    install_plan_mode,
    resolve_config,
)

SECTION_TEXT = "Plan guidance: think step by step before acting."


def _seed_open_turn(session: Session) -> None:
    """写一个打开中的回合（turn/start 无 turn/end）作为 pending 提交的前置。"""
    session.append("turn/start", {"turn": 1})


def _capture_adapter(tool_call=None):
    """记录每次请求 system 文本的假适配器。"""
    class Capture(FakeLlmAdapter):
        provider = "fake"
        model = "fake-model"

        def __init__(self, tool_call=None):
            super().__init__(tool_call=tool_call, final_text="搞定。")
            self.systems = []

        def stream(self, messages, tools):
            system = next(m["content"][0]["text"] for m in messages if m["role"] == "system")
            self.systems.append(system)
            return super().stream(messages, tools)
    return Capture(tool_call=tool_call)


# ---------- 配置 ----------

class ResolveConfigTest(unittest.TestCase):
    def test_valid(self):
        self.assertEqual(resolve_config({"section": "指引"}), {"section": "指引"})

    def test_missing_section(self):
        with self.assertRaises(ValueError):
            resolve_config({})

    def test_blank_section(self):
        with self.assertRaises(ValueError):
            resolve_config({"section": "   "})

    def test_non_string_section(self):
        with self.assertRaises(ValueError):
            resolve_config({"section": 5})

    def test_unknown_key(self):
        with self.assertRaises(ValueError):
            resolve_config({"section": "x", "extra": 1})


# ---------- fold ----------

class FoldPlanModeTest(unittest.TestCase):
    def test_empty_inactive(self):
        self.assertFalse(fold_plan_mode(Session("f").events))

    def test_last_wins(self):
        session = Session("f")
        session.append("plan/mode", {"active": True})
        session.append("plan/mode", {"active": False})
        session.append("plan/mode", {"active": True})
        self.assertTrue(fold_plan_mode(session.events))

    def test_end_prefix(self):
        session = Session("f")
        session.append("plan/mode", {"active": True})
        session.append("plan/mode", {"active": False})
        # end 为前缀上界：end=1 → 只看第一条（active）；end=2 → 两条（最后 inactive）
        self.assertTrue(fold_plan_mode(session.events, end=1))
        self.assertFalse(fold_plan_mode(session.events, end=2))


class PlanModeVocabularyTest(unittest.TestCase):
    def test_known_types_includes_plan_mode(self):
        self.assertIn("plan/mode", KNOWN_TYPES)

    def test_seed_roundtrip_fail_closed(self):
        session = Session("v")
        session.append("plan/mode", {"active": True})
        replayed = Session("v2", seed=list(session.events))
        self.assertTrue(fold_plan_mode(replayed.events))
        bad = list(session.events)
        bad[0] = {**bad[0], "type": "plan/nope"}
        with self.assertRaises(ValueError):
            Session("v3", seed=bad)

    def test_plan_mode_is_log_only(self):
        session = Session("l")
        event = session.append("plan/mode", {"active": True})
        self.assertNotIn("surfaceOp", event)
        self.assertEqual(event["data"], {"active": True})


# ---------- systemPrompt 分节服务 ----------

class SystemPromptServiceTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="sp")

    def test_install_idempotent(self):
        first = install_system_prompt(self.ctx)
        second = install_system_prompt(self.ctx)
        self.assertIs(first, second)
        self.assertIs(self.ctx.inject(SYSTEM_PROMPT_SERVICE), first)

    def test_render_sorted_by_order_skips_empty(self):
        service = SystemPromptService(self.ctx)
        service.section("b", 50, "")
        service.section("a", -100, "id")
        service.section("c", 0, lambda ctx: ctx.get("agent_name", "anon"))
        rendered = service.render({"agent_name": "mini"})
        self.assertEqual([s["name"] for s in rendered], ["a", "c"])
        self.assertEqual(rendered[1]["text"], "mini")

    def test_stable_ties_by_registration(self):
        service = SystemPromptService(self.ctx)
        service.section("first", 10, "1")
        service.section("second", 10, "2")
        self.assertEqual([s["text"] for s in service.render({})], ["1", "2"])

    def test_duplicate_name_fails(self):
        service = SystemPromptService(self.ctx)
        service.section("plan:policy", 50, "x")
        with self.assertRaises(ValueError):
            service.section("plan:policy", 51, "y")

    def test_invalid_args(self):
        service = SystemPromptService(self.ctx)
        with self.assertRaises(ValueError):
            service.section("", 0, "x")
        with self.assertRaises(ValueError):
            service.section("x", "fifty", "x")
        with self.assertRaises(TypeError):
            service.section("x", 0, 42)

    def test_disposer_removes(self):
        service = SystemPromptService(self.ctx)
        service.section("x", 0, "x")
        self.assertEqual(len(service.render({})), 1)
        disposer = service.section("y", 1, "y")
        self.assertEqual(len(service.render({})), 2)
        disposer()
        self.assertEqual([s["name"] for s in service.render({})], ["x"])


# ---------- PlanModeController ----------

class PlanModeControllerTest(unittest.TestCase):
    def _make(self, section=SECTION_TEXT):
        ctx = Context(name="plan")
        install_system_prompt(ctx)
        controller = install_plan_mode(ctx, {"section": section})
        reg = ToolRegistry(ctx)
        adapter = _capture_adapter()
        loop = AgentLoop(Session("p1"), adapter, reg, ctx)
        return ctx, controller, loop, adapter

    def test_install_requires_system_prompt(self):
        ctx = Context(name="bare")
        with self.assertRaises(KeyError):
            install_plan_mode(ctx, {"section": SECTION_TEXT})

    def test_install_rejects_bad_config(self):
        ctx = Context(name="plan")
        install_system_prompt(ctx)
        with self.assertRaises(ValueError):
            install_plan_mode(ctx, {"section": "", "nope": 1})

    def test_set_idle_commits(self):
        ctx, controller, loop, _ = self._make()
        self.assertEqual(controller.set(loop, True), "committed")
        self.assertTrue(fold_plan_mode(loop.session.events))
        self.assertEqual(controller.get(loop), {"active": True})

    def test_set_noop(self):
        ctx, controller, loop, _ = self._make()
        controller.set(loop, True)
        self.assertEqual(controller.set(loop, True), "noop")

    def test_set_open_turn_queued(self):
        ctx, controller, loop, _ = self._make()
        _seed_open_turn(loop.session)
        self.assertEqual(controller.set(loop, True), "queued")
        # pending 未提交前：fold 仍是 inactive，get 报告 pending
        self.assertFalse(fold_plan_mode(loop.session.events))
        self.assertEqual(controller.get(loop), {"active": False, "pending": True})

    def test_set_open_turn_cancelled(self):
        ctx, controller, loop, _ = self._make()
        controller.set(loop, True)  # committed（idle）
        _seed_open_turn(loop.session)
        self.assertEqual(controller.set(loop, True), "noop")
        self.assertEqual(controller.set(loop, False), "queued")
        # 对已 pending 的同状态重复选择 → cancelled
        self.assertEqual(controller.set(loop, False), "cancelled")
        # pending 覆盖：同 turn 内改选 True → queued
        self.assertEqual(controller.set(loop, True), "queued")

    def test_reject_does_not_commit(self):
        ctx, controller, loop, _ = self._make()
        _seed_open_turn(loop.session)
        controller.set(loop, True)
        ctx.on("agent/pre-step", lambda p, nxt: {"kind": "reject"})
        decision = ctx.waterfall("agent/pre-step", {"messages": [], "agent": loop,
                                                    "signal": SimpleNamespace(aborted=False)})
        self.assertEqual(decision["kind"], "reject")
        self.assertNotIn("plan/mode", [e["type"] for e in loop.session.events])

    def test_aborted_does_not_commit(self):
        ctx, controller, loop, _ = self._make()
        _seed_open_turn(loop.session)
        controller.set(loop, True)
        ctx.waterfall("agent/pre-step", {"messages": [], "agent": loop,
                                         "signal": SimpleNamespace(aborted=True)})
        self.assertNotIn("plan/mode", [e["type"] for e in loop.session.events])

    def test_policy_section_active_only(self):
        ctx, controller, loop, adapter = self._make()
        # inactive：渲染跳过
        self.assertEqual(controller._policy_text(loop), "")
        controller.set(loop, True)
        self.assertEqual(controller._policy_text(loop), SECTION_TEXT)
        # pending 也视为 active（上游 (pending?.active ?? fold)）
        ctx2 = Context(name="plan2")
        install_system_prompt(ctx2)
        controller2 = install_plan_mode(ctx2, {"section": SECTION_TEXT})
        other = AgentLoop(Session("p2"), _capture_adapter(), ToolRegistry(ctx2), ctx2)
        _seed_open_turn(other.session)
        controller2.set(other, True)
        self.assertEqual(controller2._policy_text(other), SECTION_TEXT)


class PlanLoopIntegrationTest(unittest.TestCase):
    def test_policy_section_injected_when_active(self):
        ctx = Context(name="plan")
        install_system_prompt(ctx)
        controller = install_plan_mode(ctx, {"section": SECTION_TEXT})
        adapter = _capture_adapter()
        reg = ToolRegistry(ctx)
        loop = AgentLoop(Session("i1"), adapter, reg, ctx)
        controller.set(loop, True)
        loop.followup("按计划来")
        self.assertIn(SECTION_TEXT, adapter.systems[0])
        types = [e["type"] for e in loop.session.events]
        self.assertIn("plan/mode", types)
        self.assertEqual(loop.session.events[-1]["data"]["reason"], {"kind": "completed"})

    def test_queued_commits_at_in_turn_pre_step(self):
        """turn 运行中 set() → queued；工具续步（claimed=None）的 accepted
        pre-step 提交 plan/mode，并注入模式切换叙述。"""
        ctx = Context(name="plan")
        install_system_prompt(ctx)
        controller = install_plan_mode(ctx, {"section": SECTION_TEXT})
        adapter = _capture_adapter(tool_call={"name": "switch", "arguments": {}})
        reg = ToolRegistry(ctx)
        loop = AgentLoop(Session("i2"), adapter, reg, ctx)
        outcome = {}

        def execute(args, exec_):
            outcome["result"] = controller.set(loop, True)
            return "queued ok"

        reg.register(Tool(name="switch", description="switch", parameters={}, execute=execute))
        loop.followup("先跑工具")
        self.assertEqual(outcome["result"], "queued")
        types = [e["type"] for e in loop.session.events]
        self.assertIn("plan/mode", types)
        plan_event = next(e for e in loop.session.events if e["type"] == "plan/mode")
        self.assertTrue(plan_event["data"]["active"])
        # 提交发生在工具续步的 pre-step：plan/mode 先于该 step/start，晚于第一 request/header
        header_seq = next(e["seq"] for e in loop.session.events if e["type"] == "request/header")
        self.assertGreater(plan_event["seq"], header_seq)
        # 叙述：最近 header 描述 inactive → 切换 active → 注入 user 消息（模型可见 ⟺ 已记录）
        narrations = [
            e for e in loop.session.events
            if e["type"] == "user/message"
            and e["data"]["source"].get("plugin") == "plan-mode"
        ]
        self.assertEqual(len(narrations), 1)
        self.assertIn("plan mode", narrations[0]["data"]["content"][0]["text"].lower())
        # 第二步请求的 system 已含指引
        self.assertGreaterEqual(adapter.calls, 2)
        self.assertIn(SECTION_TEXT, adapter.systems[-1])

    def test_committed_narration_injected_to_inbox(self):
        """idle 提交的叙述经 agent.inject 入 inbox，下个回合作为 user/message 落日志。"""
        ctx = Context(name="plan")
        install_system_prompt(ctx)
        controller = install_plan_mode(ctx, {"section": SECTION_TEXT})
        loop = AgentLoop(Session("i3"), _capture_adapter(), ToolRegistry(ctx), ctx)
        # 先有 request/header（plan inactive）→ 切换会产生叙述
        loop.session.append("request/header",
                            {"header": {"provider": "fake", "model": "fake-model"},
                             "reason": "initial"})
        controller.set(loop, True)
        self.assertEqual(len(loop.inbox), 1)  # 叙述已入 inbox，未开回合
        loop.followup("开始")
        user_msgs = [e for e in loop.session.events if e["type"] == "user/message"]
        plugin = user_msgs[0]["data"]["source"]
        self.assertEqual(plugin, {"kind": "plugin", "plugin": "plan-mode"})
        self.assertIn("plan mode", user_msgs[0]["data"]["content"][0]["text"].lower())

    def test_no_narration_when_last_header_matches(self):
        ctx = Context(name="plan")
        install_system_prompt(ctx)
        controller = install_plan_mode(ctx, {"section": SECTION_TEXT})
        loop = AgentLoop(Session("i4"), _capture_adapter(), ToolRegistry(ctx), ctx)
        # 上次 header 时已是 inactive → 切 inactive 无叙述
        loop.session.append("request/header",
                            {"header": {"provider": "fake", "model": "fake-model"},
                             "reason": "initial"})
        controller.set(loop, True)
        loop.inbox.clear()
        controller.set(loop, False)
        self.assertEqual(len(loop.inbox), 0)
        self.assertFalse(fold_plan_mode(loop.session.events))


if __name__ == "__main__":
    unittest.main()
