"""pull 式 goal round 驱动：宿主在回合边界同步续跑目标轮次。

上游对照：packages/goal/goal-round-driver/src/index.ts（GoalDriver，事件驱动：
turn/end 触发 continue→attempt→admitted/claimed→assistant/message 复位）+ prompt.ts。

契约（与上游一致）：
  * round 消息 = goal 来源 user 消息（source {kind:'goal', goalId, revision,
    round}），经 Agent.followup() 入队；pre-step 仅做 reservation 校验
    （fail-closed：非 active/armed、revision 不匹配、非下一个轮次、非本驱动
    排队的消息 → 拒绝，turn 以 {kind:'blocked'} 闭合）。
  * 自动续跑按已结算 durable 状态：active+armed 才继续；round 预算用尽 →
    block {code:'round-limit'}；回合被拒 → block {code:'prompt-rejected'}；
    max-tokens → disarm；aborted → pause（对齐上游 cancelled attempt 语义）。
  * 同一会话串行；轮次随日志重放推进 roundsStarted，回放不重建 attempt。

mini 简化（push→pull 重构，须在文档中标注）：宿主必须显式调 continue_rounds()
驱动（上游 turn/end 事件自动续跑）；无 agent 身份断言与 reserved attempt 集
（同步模型下 reservation 只在排队→pre-step 间存活）；deferContext wrapup
注入未复现；连续 block-after 阈值策略在 tool-goal 层近似判定（见 tools.py）。
"""
from __future__ import annotations

import logging
from typing import Callable

from ..core.scope import Context, _maybe_await
from ..core.session.message import create_message
from .prompt import render_goal_round_prompt

__all__ = ["GoalDriver", "install_goal_driver"]


class GoalDriver:
    """同一 ctx 作用域内、按会话推进当前目标轮次的同步续跑驱动。"""

    def __init__(self, ctx: Context, goals):
        self._ctx = ctx
        self._goals = goals
        #: session id -> reservation（排队中的 goal 轮次身份；pre-step 消费后清除）
        self._reservations: dict[int, dict] = {}
        ctx.on("agent/pre-step", self._on_pre_step)

    # ---------- 内部 ----------

    def _reserve(self, loop, message: dict, round_no: int) -> None:
        self._reservations[id(loop.session)] = {
            "message": message,
            "round": round_no,
            "goalId": message["source"]["goalId"],
            "revision": message["source"]["revision"],
        }

    def _valid_reservation(self, agent) -> bool:
        reservation = self._reservations.get(id(agent.session))
        if reservation is None:
            return False
        view = self._goals.get(agent)
        if view is None:
            return False
        if view["phase"] != "active" or view["activation"] != "armed":
            return False
        return (reservation["goalId"] == view["id"]
                and reservation["revision"] == view["revision"]
                and reservation["round"] == view["roundsStarted"] + 1
                and reservation["round"] <= view["maxGoalRounds"])

    async def _on_pre_step(self, payload: dict, next_fn: Callable) -> dict:
        """验证被认领的 goal 轮次消息（fail-closed），再委派下游决策。"""
        claimed = [m for m in payload.get("messages", [])
                   if isinstance(m, dict) and (m.get("source") or {}).get("kind") == "goal"]
        if not claimed:
            return await _maybe_await(next_fn())
        agent = payload.get("agent")
        session_id = id(agent.session)
        if not self._valid_reservation(agent):
            self._reservations.pop(session_id, None)
            return {"kind": "reject"}
        decision = await _maybe_await(next_fn())
        if isinstance(decision, dict) and decision.get("kind") == "reject":
            self._reservations.pop(session_id, None)
            return decision
        # 委派后其它监听可能消费/改写了目标状态，重验一次
        if not self._valid_reservation(agent):
            self._reservations.pop(session_id, None)
            return {"kind": "reject"}
        return decision

    def _last_turn_end_reason(self, loop) -> dict | None:
        for event in reversed(loop.session.events):
            if event["type"] == "turn/end":
                return event["data"].get("reason")
        return None

    def _block_latest(self, loop, code: str, message: str) -> None:
        view = self._goals.get(loop)
        if view is not None and view["phase"] == "active":
            self._goals.block(loop, {"id": view["id"], "revision": view["revision"]},
                              {"code": code, "message": message})

    def _pause_latest(self, loop) -> None:
        view = self._goals.get(loop)
        if view is not None and view["phase"] == "active":
            self._goals.pause(loop, {"id": view["id"], "revision": view["revision"]})

    # ---------- 对外入口 ----------

    def continue_rounds(self, loop, max_rounds: int | None = None) -> bool:
        """宿主导航点：同步推进当前目标的后续轮次，直至相位离开 active 或预算用尽。

        @param loop 已装配 goal 工具的 AgentLoop 实例。
        @param max_rounds 单次调用的轮次上限（防御性护栏）。
        @returns 是否推进了至少一轮。
        """
        ran = 0
        while True:
            if max_rounds is not None and ran >= max_rounds:
                break
            view = self._goals.get(loop)
            if view is None or view["phase"] != "active" or view["activation"] != "armed":
                break
            if view["roundsStarted"] >= view["maxGoalRounds"]:
                self._block_latest(loop, "round-limit",
                                   f"Goal reached its configured limit of {view['maxGoalRounds']} rounds.")
                break
            round_no = view["roundsStarted"] + 1
            message = create_message(
                "user",
                render_goal_round_prompt(view, round_no),
                {"kind": "goal", "goalId": view["id"], "revision": view["revision"],
                 "round": round_no},
            )
            self._reserve(loop, message, round_no)
            try:
                loop.followup(message)
            finally:
                self._reservations.pop(id(loop.session), None)
            ran += 1
            reason = self._last_turn_end_reason(loop)
            if reason is None:
                break
            kind = reason.get("kind")
            if kind == "blocked":
                self._block_latest(loop, "prompt-rejected",
                                   "Goal round was rejected before entering its step.")
                break
            if kind == "max-tokens":
                self._goals.disarm(loop)
                break
            if kind == "aborted":
                self._pause_latest(loop)
                break
            if kind == "error":
                logger = getattr(self._ctx, "logger", None)
                if logger is not None and hasattr(logger, "warn"):
                    logger.warn(f"goal round {round_no} ended in error; stopping continuation: "
                                f"{reason.get('error')}")
                break
        return ran > 0


def install_goal_driver(ctx: Context, goals) -> GoalDriver:
    """装配 goal round 驱动（pre-step reservation 校验）。goals 必须是 GoalService。"""
    return GoalDriver(ctx, goals)
