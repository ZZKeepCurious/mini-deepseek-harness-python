"""议题 8 验收：goal 域（事件溯源 fold + GoalService + round 驱动 + 工具）。

覆盖：goal/change 词汇表与严格重放、round 消息准入、GoalService
compare-and-set 生命周期与激活、pull 式 continue_rounds、tool-goal 三工具。
"""

import asyncio
import json
import unittest
from types import SimpleNamespace

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import KNOWN_TYPES, Session, create_message
from miniharness.core.system_prompt import install_system_prompt
from miniharness.core.tools import ToolExec, ToolRegistry
from miniharness.goal import (
    GoalError,
    apply_goal_change,
    apply_goal_event,
    decode_goal_change,
    fold_goal,
    install_goal_driver,
    install_goals,
    register_goal_tools,
)
from miniharness.llm import FakeLlmAdapter


def _ctx():
    ctx = Context(name="goal")
    install_system_prompt(ctx)
    return ctx


def _loop(ctx, adapter=None, reg=None):
    reg = reg or ToolRegistry(ctx)
    adapter = adapter or FakeLlmAdapter(final_text="完成。")
    return AgentLoop(Session("goal1"), adapter, reg, ctx)


class GoalVocabularyTest(unittest.TestCase):
    def test_known_types_includes_goal_change(self):
        self.assertIn("goal/change", KNOWN_TYPES)

    def test_goal_change_is_log_only(self):
        session = Session("g")
        event = session.append("goal/change", {
            "kind": "goal/change", "version": 1, "operation": "create",
            "goal": {"id": "goal-1", "revision": 1, "objective": "o",
                     "phase": "active", "maxGoalRounds": 3},
            "roundsStarted": 0, "createdAt": 1, "updatedAt": 1,
        })
        self.assertNotIn("surfaceOp", event)

    def test_decode_goal_change_ignores_unrelated(self):
        self.assertIsNone(decode_goal_change({"kind": "user/message"}))

    def test_decode_goal_change_rejects_unsupported_version(self):
        with self.assertRaises(ValueError):
            decode_goal_change({"kind": "goal/change", "version": 2,
                                "operation": "create", "goal": {}})

    def test_decode_goal_change_rejects_malformed(self):
        with self.assertRaises(ValueError):
            decode_goal_change({"kind": "goal/change", "version": 1,
                                "operation": "create"})  # 缺快照字段
        with self.assertRaises(ValueError):
            decode_goal_change({"kind": "goal/change", "version": 1,
                                "operation": "nope", "goal": {}})  # 非法操作
        with self.assertRaises(ValueError):
            decode_goal_change({"kind": "goal/change", "version": 1,
                                "operation": "create",
                                "goal": {"id": "g", "revision": 0, "objective": "o",
                                         "phase": "active", "maxGoalRounds": 3},
                                "roundsStarted": 0, "createdAt": 1, "updatedAt": 1})  # revision 0


class GoalFoldTest(unittest.TestCase):
    def _session(self):
        session = Session("f")
        session.append("goal/change", {
            "kind": "goal/change", "version": 1, "operation": "create",
            "goal": {"id": "goal-1", "revision": 1, "objective": "o",
                     "phase": "active", "maxGoalRounds": 3},
            "roundsStarted": 0, "createdAt": 1, "updatedAt": 1,
        })
        return session

    def test_create_fold(self):
        folded = fold_goal(self._session().events)
        self.assertEqual(folded["goal"]["id"], "goal-1")
        self.assertEqual(folded["roundsStarted"], 0)

    def test_round_message_admission(self):
        session = self._session()
        message = create_message(
            "user", [{"type": "text", "text": "round"}],
            {"kind": "goal", "goalId": "goal-1", "revision": 1, "round": 1})
        session.append("user/message", message, surfaceOp="append")
        folded = fold_goal(session.events)
        self.assertEqual(folded["roundsStarted"], 1)

    def test_round_admission_fail_closed(self):
        session = self._session()
        message = create_message(
            "user", [{"type": "text", "text": "stale"}],
            {"kind": "goal", "goalId": "goal-1", "revision": 1, "round": 2})  # 跳轮
        session.append("user/message", message, surfaceOp="append")
        with self.assertRaises(ValueError):
            fold_goal(session.events)

    def test_clear_tombstone(self):
        session = self._session()
        session.append("goal/change", {
            "kind": "goal/change", "version": 1, "operation": "clear",
            "cleared": {"id": "goal-1", "revision": 2}, "clearedAt": 2,
        })
        folded = fold_goal(session.events)
        self.assertNotIn("goal", folded)
        self.assertEqual(folded["lastRef"], {"id": "goal-1", "revision": 2})

    def test_bad_change_fail_closed(self):
        session = self._session()
        session.append("goal/change", {
            "kind": "goal/change", "version": 1, "operation": "pause",
            "goal": {"id": "goal-1", "revision": 2, "objective": "o",
                     "phase": "paused", "maxGoalRounds": 3},
            "roundsStarted": 0, "createdAt": 1, "updatedAt": 1,
        })
        session.append("goal/change", {
            "kind": "goal/change", "version": 1, "operation": "block",
            "goal": {"id": "goal-1", "revision": 3, "objective": "o",
                     "phase": "blocked", "maxGoalRounds": 3,
                     "blockedReason": {"code": "model-reported", "message": "x"}},
            "roundsStarted": 0, "createdAt": 1, "updatedAt": 1,
        })  # block from paused 非法（block 只允许 active）
        with self.assertRaises(ValueError):
            fold_goal(session.events)

    def test_apply_goal_change_preserves_timestamps(self):
        state = {"goal": None, "roundsStarted": 0, "createdAt": None,
                 "updatedAt": None, "lastRef": None, "seenGoalIds": set()}
        change = decode_goal_change({
            "kind": "goal/change", "version": 1, "operation": "create",
            "goal": {"id": "g", "revision": 1, "objective": "o",
                     "phase": "active", "maxGoalRounds": 3},
            "roundsStarted": 0, "createdAt": 5, "updatedAt": 5,
        })
        apply_goal_change(state, change)
        self.assertEqual(state["goal"]["id"], "g")
        self.assertEqual(state["createdAt"], 5)


class GoalServiceTest(unittest.TestCase):
    def _make(self):
        ctx = _ctx()
        goals = install_goals(ctx)
        loop = _loop(ctx)
        return ctx, goals, loop

    def test_create_arms(self):
        _, goals, loop = self._make()
        view = goals.create(loop, {"objective": "ship", "maxGoalRounds": 4})
        self.assertEqual(view["phase"], "active")
        self.assertEqual(view["activation"], "armed")
        self.assertEqual(view["roundsStarted"], 0)
        self.assertEqual(view["maxGoalRounds"], 4)

    def test_create_default_rounds(self):
        _, goals, loop = self._make()
        view = goals.create(loop, {"objective": "x"})
        self.assertEqual(view["maxGoalRounds"], 256)

    def test_create_invalid(self):
        _, goals, loop = self._make()
        with self.assertRaises(GoalError) as cm:
            goals.create(loop, {"objective": "  "})
        self.assertEqual(cm.exception.code, "GOAL_INVALID_OBJECTIVE")
        with self.assertRaises(GoalError) as cm:
            goals.create(loop, {"objective": "x", "maxGoalRounds": 0})
        self.assertEqual(cm.exception.code, "GOAL_INVALID_MAX_ROUNDS")

    def test_create_conflict(self):
        _, goals, loop = self._make()
        goals.create(loop, {"objective": "a"})
        with self.assertRaises(GoalError) as cm:
            goals.create(loop, {"objective": "b"})
        self.assertEqual(cm.exception.code, "GOAL_ALREADY_EXISTS")

    def test_edit_advances_revision(self):
        _, goals, loop = self._make()
        created = goals.create(loop, {"objective": "a", "maxGoalRounds": 3})
        edited = goals.edit(loop, {"id": created["id"], "revision": 1},
                            {"objective": "b"})
        self.assertEqual(edited["revision"], 2)
        self.assertEqual(edited["objective"], "b")
        self.assertEqual(edited["phase"], "active")

    def test_edit_requires_change(self):
        _, goals, loop = self._make()
        created = goals.create(loop, {"objective": "a"})
        with self.assertRaises(GoalError) as cm:
            goals.edit(loop, {"id": created["id"], "revision": 1}, {})
        self.assertEqual(cm.exception.code, "GOAL_INVALID_EDIT")

    def test_stale_ref(self):
        _, goals, loop = self._make()
        created = goals.create(loop, {"objective": "a"})
        goals.edit(loop, {"id": created["id"], "revision": 1}, {"objective": "b"})
        with self.assertRaises(GoalError) as cm:
            goals.edit(loop, {"id": created["id"], "revision": 1}, {"objective": "c"})
        self.assertEqual(cm.exception.code, "GOAL_STALE_REVISION")

    def test_lifecycle_transitions(self):
        _, goals, loop = self._make()
        created = goals.create(loop, {"objective": "a", "maxGoalRounds": 3})
        ref = {"id": created["id"], "revision": created["revision"]}

        paused = goals.pause(loop, ref)
        self.assertEqual(paused["phase"], "paused")
        self.assertEqual(paused["activation"], "disarmed")
        with self.assertRaises(GoalError) as cm:
            goals.pause(loop, {"id": created["id"], "revision": 2})  # 已暂停
        self.assertEqual(cm.exception.code, "GOAL_INVALID_TRANSITION")

        resumed = goals.resume(loop, {"id": created["id"], "revision": 2})
        self.assertEqual(resumed["phase"], "active")
        self.assertEqual(resumed["activation"], "armed")
        with self.assertRaises(GoalError) as cm:
            goals.resume(loop, {"id": created["id"], "revision": 3})  # 已 armed
        self.assertEqual(cm.exception.code, "GOAL_INVALID_TRANSITION")

        blocked = goals.block(loop, {"id": created["id"], "revision": 3},
                              {"code": "model-reported", "message": "stuck"})
        self.assertEqual(blocked["phase"], "blocked")
        self.assertEqual(blocked["blockedReason"],
                         {"code": "model-reported", "message": "stuck"})

        completed = goals.complete(loop, {"id": created["id"], "revision": 4})
        self.assertEqual(completed["phase"], "complete")

        tombstone = goals.clear(loop, {"id": created["id"], "revision": 5})
        self.assertEqual(tombstone["revision"], 6)
        self.assertIsNone(goals.get(loop))

        # 替换：complete 后允许 create 新目标
        second = goals.create(loop, {"objective": "b"})
        self.assertEqual(second["objective"], "b")

    def test_block_reason_validation(self):
        _, goals, loop = self._make()
        created = goals.create(loop, {"objective": "a"})
        with self.assertRaises(GoalError) as cm:
            goals.block(loop, {"id": created["id"], "revision": 1},
                        {"code": "Bad Code", "message": "x"})
        self.assertEqual(cm.exception.code, "GOAL_INVALID_BLOCK_REASON")

    def test_disarm(self):
        _, goals, loop = self._make()
        created = goals.create(loop, {"objective": "a"})
        self.assertEqual(goals.get(loop)["activation"], "armed")
        goals.disarm(loop)
        self.assertEqual(goals.get(loop)["activation"], "disarmed")

    def test_durable_events_and_goal_changed(self):
        ctx, goals, loop = self._make()
        seen = []
        ctx.on("goal/changed", lambda payload: seen.append(payload["change"]["operation"]))
        created = goals.create(loop, {"objective": "a"})
        goals.pause(loop, {"id": created["id"], "revision": 1})
        self.assertEqual(seen, ["create", "pause"])
        changes = [e for e in loop.session.events if e["type"] == "goal/change"]
        self.assertEqual(len(changes), 2)
        self.assertEqual(fold_goal(loop.session.events)["goal"]["phase"], "paused")


class GoalDriverTest(unittest.TestCase):
    def _make(self, max_rounds=3):
        ctx = _ctx()
        goals = install_goals(ctx)
        reg = ToolRegistry(ctx)
        register_goal_tools(reg, goals, ctx)
        adapter = FakeLlmAdapter(final_text="完成。")
        loop = AgentLoop(Session("d1"), adapter, reg, ctx)
        driver = install_goal_driver(ctx, goals)
        created = goals.create(loop, {"objective": "do the thing", "maxGoalRounds": max_rounds})
        return ctx, goals, loop, driver, created

    def test_continue_rounds_round_limit_blocks(self):
        _, goals, loop, driver, created = self._make(max_rounds=3)
        self.assertTrue(driver.continue_rounds(loop))
        view = goals.get(loop)
        self.assertEqual(view["phase"], "blocked")
        self.assertEqual(view["blockedReason"]["code"], "round-limit")
        self.assertEqual(view["roundsStarted"], 3)
        # round 消息确实是 goal 来源且推进了 durable 状态
        goal_msgs = [e for e in loop.session.events
                     if e["type"] == "user/message"
                     and e["data"]["source"].get("kind") == "goal"]
        self.assertEqual(len(goal_msgs), 3)

    def test_continue_rounds_idle_when_not_armed(self):
        _, goals, loop, driver, created = self._make()
        goals.pause(loop, {"id": created["id"], "revision": 1})
        self.assertFalse(driver.continue_rounds(loop))
        self.assertEqual(goals.get(loop)["roundsStarted"], 0)

    def test_unreserved_goal_message_rejected(self):
        _, goals, loop, driver, created = self._make()
        # 未经过驱动排队的 goal 来源消息 → pre-step fail-closed 拒绝 → turn blocked
        message = create_message(
            "user", [{"type": "text", "text": "forged round"}],
            {"kind": "goal", "goalId": created["id"], "revision": 1, "round": 1})
        loop.followup(message)
        last_turn_end = [e for e in loop.session.events if e["type"] == "turn/end"][-1]
        self.assertEqual(last_turn_end["data"]["reason"], {"kind": "blocked"})
        # 该消息未进日志，round 计数未动
        self.assertEqual(goals.get(loop)["roundsStarted"], 0)
        self.assertEqual(goals.get(loop)["phase"], "active")

    def test_prompt_rejected_blocks_goal(self):
        ctx, goals, loop, driver, created = self._make(max_rounds=3)
        # 另一个 pre-step 监听器拒绝 → 驱动把目标 block 为 prompt-rejected
        ctx.on("agent/pre-step", lambda p, nxt: {"kind": "reject"})
        driver.continue_rounds(loop)
        view = goals.get(loop)
        self.assertEqual(view["phase"], "blocked")
        self.assertEqual(view["blockedReason"]["code"], "prompt-rejected")

    def test_aborted_round_pauses_goal(self):
        ctx, goals, loop, driver, created = self._make(max_rounds=3)
        def aborting(payload, nxt):
            if any((m.get("source") or {}).get("kind") == "goal"
                   for m in payload.get("messages", [])):
                payload["agent"].cancel("user")
            return nxt()
        ctx.on("agent/pre-step", aborting)
        driver.continue_rounds(loop)
        self.assertEqual(goals.get(loop)["phase"], "paused")

    def test_max_rounds_guard(self):
        _, goals, loop, driver, created = self._make(max_rounds=5)
        driver.continue_rounds(loop, max_rounds=1)
        self.assertEqual(goals.get(loop)["roundsStarted"], 1)
        self.assertEqual(goals.get(loop)["phase"], "active")


class GoalToolsTest(unittest.TestCase):
    def _make(self):
        ctx = _ctx()
        goals = install_goals(ctx)
        reg = ToolRegistry(ctx)
        register_goal_tools(reg, goals, ctx)
        loop = _loop(ctx, reg=reg)
        return ctx, goals, reg, loop

    def _call(self, reg, name, args, exec_):
        """执行 goal 工具（async 契约 → asyncio.run 包装）。"""
        return asyncio.run(reg.resolve(name).execute(args, exec_))

    def test_policy_section_registered(self):
        ctx, _, _, _ = self._make()
        names = [s["name"] for s in ctx.inject("systemPrompt").render({})]
        self.assertIn("tool:goal", names)

    def test_get_goal_empty(self):
        _, _, reg, loop = self._make()
        out = self._call(reg, "get_goal", {}, ToolExec(agent=loop))
        self.assertEqual(json.loads(out), {"goal": None})

    def test_create_and_read(self):
        _, goals, reg, loop = self._make()
        out = self._call(reg, "create_goal",
                         {"objective": "build it", "max_goal_rounds": 5}, ToolExec(agent=loop))
        value = json.loads(out)
        self.assertEqual(value["goal"]["objective"], "build it")
        self.assertEqual(value["goal"]["phase"], "active")
        self.assertEqual(value["activation"], "armed")
        out = self._call(reg, "get_goal", {}, ToolExec(agent=loop))
        self.assertEqual(json.loads(out)["goal"]["revision"], 1)

    def test_create_invalid_objective_code(self):
        _, _, reg, loop = self._make()
        with self.assertRaises(GoalError) as cm:
            self._call(reg, "create_goal", {"objective": ""}, ToolExec(agent=loop))
        self.assertEqual(cm.exception.code, "GOAL_INVALID_OBJECTIVE")

    def test_update_complete(self):
        _, goals, reg, loop = self._make()
        self._call(reg, "create_goal", {"objective": "a"}, ToolExec(agent=loop))
        out = self._call(reg, "update_goal",
                         {"goal_id": goals.get(loop)["id"], "revision": 1, "action": "complete"},
                         ToolExec(agent=loop))
        self.assertEqual(json.loads(out)["goal"]["phase"], "complete")

    def test_update_invalid_ref(self):
        _, goals, reg, loop = self._make()
        self._call(reg, "create_goal", {"objective": "a"}, ToolExec(agent=loop))
        with self.assertRaises(GoalError) as cm:
            self._call(reg, "update_goal",
                       {"goal_id": "nope", "revision": 0, "action": "pause"},
                       ToolExec(agent=loop))
        self.assertEqual(cm.exception.code, "GOAL_TOOL_INVALID_UPDATE")
        # 格式合法但与当前目标不匹配 → 服务层 stale ref
        with self.assertRaises(GoalError) as cm:
            self._call(reg, "update_goal",
                       {"goal_id": "nope", "revision": 1, "action": "pause"},
                       ToolExec(agent=loop))
        self.assertEqual(cm.exception.code, "GOAL_STALE_REVISION")

    def test_update_param_mismatch(self):
        _, goals, reg, loop = self._make()
        self._call(reg, "create_goal", {"objective": "a"}, ToolExec(agent=loop))
        with self.assertRaises(GoalError) as cm:
            self._call(reg, "update_goal",
                       {"goal_id": goals.get(loop)["id"], "revision": 1, "action": "complete",
                        "objective": "b"},
                       ToolExec(agent=loop))
        self.assertEqual(cm.exception.code, "GOAL_TOOL_INVALID_UPDATE")

    def test_blocked_requires_reason(self):
        _, goals, reg, loop = self._make()
        self._call(reg, "create_goal", {"objective": "a"}, ToolExec(agent=loop))
        with self.assertRaises(GoalError) as cm:
            self._call(reg, "update_goal",
                       {"goal_id": goals.get(loop)["id"], "revision": 1, "action": "blocked"},
                       ToolExec(agent=loop))
        self.assertEqual(cm.exception.code, "GOAL_TOOL_INVALID_UPDATE")

    def test_blocked_threshold_in_goal_round(self):
        _, goals, reg, loop = self._make()
        created = goals.create(loop, {"objective": "a"})
        # 模拟进行中的 goal 轮次：goal 来源消息已入日志（round 1）
        message = create_message(
            "user", [{"type": "text", "text": "round"}],
            {"kind": "goal", "goalId": created["id"], "revision": 1, "round": 1})
        loop.session.append("user/message", message, surfaceOp="append")
        with self.assertRaises(GoalError) as cm:
            self._call(reg, "update_goal",
                       {"goal_id": created["id"], "revision": 1, "action": "blocked",
                        "blocked_reason": "stuck"},
                       ToolExec(agent=loop))
        self.assertEqual(cm.exception.code, "GOAL_TOOL_BLOCK_THRESHOLD")

    def test_blocked_threshold_passed(self):
        _, goals, reg, loop = self._make()
        created = goals.create(loop, {"objective": "a"})
        # 三连 goal 轮次（round 1..3）后允许 blocked
        for round_no in (1, 2, 3):
            message = create_message(
                "user", [{"type": "text", "text": "round"}],
                {"kind": "goal", "goalId": created["id"], "revision": 1, "round": round_no})
            loop.session.append("user/message", message, surfaceOp="append")
        out = self._call(reg, "update_goal",
                         {"goal_id": created["id"], "revision": 1, "action": "blocked",
                          "blocked_reason": "still stuck"},
                         ToolExec(agent=loop))
        value = json.loads(out)
        self.assertEqual(value["goal"]["phase"], "blocked")
        self.assertEqual(value["goal"]["blockedReason"]["code"], "model-reported")

    def test_requires_agent(self):
        _, _, reg, loop = self._make()
        with self.assertRaises(GoalError):
            self._call(reg, "get_goal", {}, ToolExec())


if __name__ == "__main__":
    unittest.main()
