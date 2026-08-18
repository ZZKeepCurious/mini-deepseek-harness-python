"""议题 8 验收：plan 审查 UI（exit_plan_mode 工具 + /plan 命令 + 审查通道）。

覆盖：工具注册跨模式稳定、plan/headling/agent/通道前置、批准排队 silent 退出、
keep planning/dismiss 反馈、/plan 四态文案、命令可选性、投影 fold。
"""

import asyncio
import unittest
from types import SimpleNamespace

from miniharness.commands import install_commands, route_command
from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.system_prompt import install_system_prompt
from miniharness.core.tools import ToolExec, ToolRegistry
from miniharness.llm import FakeLlmAdapter
from miniharness.plan import (
    APPROVE_LABEL,
    EXIT_PLAN_MODE,
    KEEP_PLANNING_LABEL,
    fold_plan_mode,
    fold_plan_projection,
    install_plan_mode,
    install_plan_review,
)


class _Channel:
    """可编程 userQuestions 通道（sync 回调 .ask(question, agent) -> str | None）。"""

    def __init__(self):
        self.answers = []
        self.asked = []

    def ask(self, question, agent):
        self.asked.append(question)
        return self.answers.pop(0) if self.answers else None


def _make(commands=True):
    ctx = Context(name="plan-review")
    install_system_prompt(ctx)
    if commands:
        install_commands(ctx)
    controller = install_plan_mode(ctx, {"section": "Plan guidance."})
    reg = ToolRegistry(ctx)
    install_plan_review(ctx, controller)
    adapter = FakeLlmAdapter(final_text="完成。")
    loop = AgentLoop(Session("r1"), adapter, reg, ctx)
    return ctx, controller, reg, loop


def _call(reg, args, exec_=None):
    """执行 exit_plan_mode 工具（async 契约 → asyncio.run 包装）。"""
    return asyncio.run(reg.resolve(EXIT_PLAN_MODE).execute(args, exec_ or ToolExec()))


class ExitPlanModeTest(unittest.TestCase):
    def test_registered_even_inactive(self):
        _, _, reg, _ = _make()
        self.assertIsNotNone(reg.resolve(EXIT_PLAN_MODE))

    def test_requires_agent(self):
        _, _, reg, _ = _make()
        with self.assertRaises(ValueError):
            _call(reg, {"plan": "# P"})

    def test_requires_plan_mode(self):
        _, _, reg, loop = _make()
        with self.assertRaises(ValueError) as cm:
            _call(reg, {"plan": "# P"}, ToolExec(agent=loop))
        self.assertIn("only available in plan mode", str(cm.exception))

    def test_requires_heading(self):
        _, controller, reg, loop = _make()
        controller.set(loop, True)
        with self.assertRaises(ValueError):
            _call(reg, {"plan": "no heading"}, ToolExec(agent=loop))

    def test_requires_channel(self):
        ctx, controller, reg, loop = _make()
        controller.set(loop, True)
        with self.assertRaises(ValueError) as cm:
            _call(reg, {"plan": "# Plan"}, ToolExec(agent=loop))
        self.assertIn("no user-questions channel", str(cm.exception))

    def test_dismissed_stays(self):
        ctx, controller, reg, loop = _make()
        channel = _Channel()
        ctx.provide("userQuestions", channel)
        controller.set(loop, True)
        with self.assertRaises(ValueError) as cm:
            _call(reg, {"plan": "# Plan"}, ToolExec(agent=loop))
        self.assertIn("dismissed the plan review", str(cm.exception))
        self.assertTrue(fold_plan_mode(loop.session.events))

    def test_keep_planning_feedback(self):
        ctx, controller, reg, loop = _make()
        channel = _Channel()
        channel.answers.append("make it shorter")
        ctx.provide("userQuestions", channel)
        controller.set(loop, True)
        with self.assertRaises(ValueError) as cm:
            _call(reg, {"plan": "# Plan"}, ToolExec(agent=loop))
        self.assertIn("feedback: make it shorter", str(cm.exception))

    def test_approved_queues_exit(self):
        ctx, controller, reg, loop = _make()
        channel = _Channel()
        channel.answers.append(APPROVE_LABEL)
        ctx.provide("userQuestions", channel)
        controller.set(loop, True)
        out = _call(reg, {"plan": "# Plan"}, ToolExec(agent=loop))
        self.assertIn("Plan approved", out)
        # 批准只排队 silent 选择，durable 状态仍未关闭
        self.assertTrue(fold_plan_mode(loop.session.events))
        # 下一次被接受的 in-turn pre-step 提交关闭
        asyncio.run(ctx.awaterfall("agent/pre-step", {"messages": [], "agent": loop,
                                                      "signal": SimpleNamespace(aborted=False)}))
        self.assertFalse(fold_plan_mode(loop.session.events))

    def test_question_shapes(self):
        ctx, controller, reg, loop = _make()
        channel = _Channel()
        channel.answers.append(APPROVE_LABEL)
        ctx.provide("userQuestions", channel)
        controller.set(loop, True)
        _call(reg, {"plan": "# Plan"}, ToolExec(agent=loop))
        question = channel.asked[0]
        self.assertEqual(question["id"], "plan-review")
        self.assertEqual([o["label"] for o in question["options"]],
                         [APPROVE_LABEL, KEEP_PLANNING_LABEL])


class PlanCommandTest(unittest.TestCase):
    def test_off_when_inactive(self):
        ctx, controller, reg, loop = _make()
        self.assertEqual(route_command("/plan off", loop, ctx),
                         "Plan mode is already inactive.")

    def test_off_committed(self):
        ctx, controller, reg, loop = _make()
        controller.set(loop, True)
        self.assertEqual(route_command("/plan off", loop, ctx), "Plan mode off.")
        self.assertFalse(fold_plan_mode(loop.session.events))

    def test_on_committed(self):
        ctx, controller, reg, loop = _make()
        self.assertEqual(route_command("/plan x", loop, ctx),
                         "Plan mode on. Use /plan off to leave.")

    def test_on_queued(self):
        ctx, controller, reg, loop = _make()
        # 模拟运行中的回合：/plan 命令被记 pending（queued）
        loop.session.append("turn/start", {"turn": 1})
        text = route_command("/plan x", loop, ctx)
        self.assertEqual(text,
                         "Entering plan mode (applies from the next step). Use /plan off to leave.")
        # steer 的下一输入在 accepted in-turn pre-step 提交 pending
        self.assertTrue(fold_plan_mode(loop.session.events))

    def test_command_events_pair(self):
        ctx, controller, reg, loop = _make()
        route_command("/plan x", loop, ctx)
        types = [e["type"] for e in loop.session.events]
        self.assertIn("command/run", types)
        self.assertIn("command/done", types)


class PlanProjectionTest(unittest.TestCase):
    def test_fold_plan_projection(self):
        session = Session("p")
        self.assertEqual(fold_plan_projection(session.events), {"active": False, "pending": False})
        session.append("plan/mode", {"active": True})
        self.assertEqual(fold_plan_projection(session.events), {"active": True, "pending": False})
        # 用户已落盘 /plan off 选择：active 仍 True，pending 表达退出意图
        session.append("command/run", {"commandId": "c1", "name": "plan", "args": " off",
                                       "source": {"kind": "user"}})
        session.append("command/done", {"commandId": "c1", "kind": "success", "text": "ok"})
        self.assertEqual(fold_plan_projection(session.events), {"active": True, "pending": True})
        session.append("plan/mode", {"active": False})
        self.assertEqual(fold_plan_projection(session.events), {"active": False, "pending": False})

    def test_fold_plan_projection_skips_malformed(self):
        session = Session("p")
        session.append("command/run", {"commandId": "c1", "name": "nope", "args": None,
                                       "source": {"kind": "user"}})
        self.assertEqual(fold_plan_projection(session.events), {"active": False, "pending": False})


if __name__ == "__main__":
    unittest.main()
