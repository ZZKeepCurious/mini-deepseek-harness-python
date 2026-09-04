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

mini 对齐说明（push→pull 重构，2026-08-22 部分对齐）：
   * driver 模式（web/ACP/SDK 的 async 表面）下，GoalDriver 订阅
     `agent/status`(idle) 与 `goal/changed`，在 idle 且目标 active+armed 时
     自动排下一个 round（对齐上游 goal-round-driver 的 turn/end → continue
     事件驱动续跑）；reservation 持久到该 round 回合结束（idle 到达才清除），
     避免 driver 模式 followup 只入队、pre-step 仍需 reservation 的竞态。
   * 同步门面（demo/headless 的 run()）无嵌套 asyncio.run，仍由宿主显式
     continue_rounds() 驱动（保持原 pull 式契约）；driver 模式的 idle 监听在
     `_driver is None` 时 no-op，两条路径互不干扰。
   * 未复现项（保留简化，须在 AGENTS.md 标注）：无 agent 身份断言（R4 已补
     registry assert_live 边界）与编排入口级 withInitiator 归因（仅保留 turn
     执行栈内 `current_initiator()` 最小载体，供 host-pause 判别）；competingQueued
     竞争提示护栏未实现（armed 目标在任意 idle 都会续跑，不区分是否刚有人类
     提示）；deferContext wrapup 注入未复现；连续 block-after 阈值策略在
     tool-goal 层近似判定（见 tools.py）。alpha.1（2026-09-05）已对齐：
     RoundAttempt 记录 + idle pause fence（ref 钉住被弃 attempt）+ host pause
     中止 live turn（上游 index.ts:259-294）。
"""
from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Callable

from ..core.agents import current_initiator
from ..core.scope import Context, _maybe_await
from ..core.session.message import create_message
from .prompt import render_goal_round_prompt

__all__ = ["GoalDriver", "install_goal_driver"]


class GoalDriver:
    """同一 ctx 作用域内、按会话推进当前目标轮次的同步续跑驱动。"""

    def __init__(self, ctx: Context, goals):
        self._ctx = ctx
        self._goals = goals
        #: session id (int, id(session)) -> reservation（排队中的 goal 轮次身份；
        # pre-step 消费后 / 回合结束 idle 时清除）
        self._reservations: dict[int, dict] = {}
        #: session id (str) -> AgentLoop（driver 模式自动续跑用；agent/session-start 登记）
        self._loops: dict[str, Any] = {}
        #: session id (int, id(session)) -> RoundAttempt（排队/认领/已入志的 goal 轮次
        # 记录，上游 RoundAttempt 集的 mini 载体：identity + phase + cancelled；
        # idle pause fence 依据它钉住被弃 attempt 的确切 ref）
        self._attempts: dict[int, dict] = {}
        ctx.on("agent/pre-step", self._on_pre_step)
        ctx.on("agent/session-start", self._on_session_start)
        ctx.on("agent/status", self._on_status)
        ctx.on("goal/changed", self._on_goal_changed)
        ctx.on("agent/inbox/claimed", self._on_inbox_claimed)
        ctx.on("session/event", self._on_session_event)

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

    # ---------- idle pause fence（alpha.1，上游 index.ts:259-282） ----------

    def _on_inbox_claimed(self, payload: dict) -> None:
        """排队 attempt 被认领 → phase 'claimed'（上游 agent/inbox/claimed 监听）。"""
        agent = payload.get("agent")
        message = payload.get("message")
        if agent is None or not isinstance(message, Mapping):
            return
        attempt = self._attempts.get(self._sid(agent))
        if attempt is not None and message.get("id") == attempt["messageId"]:
            attempt["phase"] = "claimed"

    def _on_session_event(self, payload: dict) -> None:
        """日志结算（上游 session/event 监听）：user/message → admitted；
        turn/end aborted → claimed/admitted attempt 置 cancelled。

        冻结载体注意：session/event 的 event 是 mappingproxy，判形用
        `Mapping`（runtime_context 同款坑）。"""
        session = payload.get("session")
        event = payload.get("event")
        if session is None or not isinstance(event, Mapping):
            return
        loop = self._loops.get(session.session_id)
        if loop is None:
            return
        attempt = self._attempts.get(self._sid(loop))
        if attempt is None:
            return
        if event["type"] == "user/message":
            if event["data"].get("id") == attempt["messageId"]:
                attempt["phase"] = "admitted"
        elif event["type"] == "turn/end":
            reason = event["data"].get("reason") or {}
            if (reason.get("kind") == "aborted"
                    and attempt["phase"] in ("claimed", "admitted")):
                attempt["cancelled"] = True

    def _pause_dropped(self, loop) -> None:
        """pause fence（alpha.1）：pause 只钉在被弃 attempt 的确切 ref 上——
        attempt 处于 queued/claimed 或已 cancelled，且 goalId+revision 与当前
        live 目标均匹配、目标 active+armed 才落 pause。resume 会推高 revision，
        故「宿主 pause 后立即 resume（被中止回合尚未收敛到 idle）」不会把已
        复活的目标再次误杀。pause 失败 → warn + disarm（上游 catch 分支）。"""
        attempt = self._attempts.get(self._sid(loop))
        if attempt is None:
            return
        if not (attempt["phase"] in ("queued", "claimed") or attempt["cancelled"]):
            return
        view = self._goals.get(loop)
        if (view is None or view["phase"] != "active" or view["activation"] != "armed"
                or attempt["goalId"] != view["id"] or attempt["revision"] != view["revision"]):
            return
        self._attempts.pop(self._sid(loop), None)
        try:
            self._goals.pause(loop, {"id": view["id"], "revision": view["revision"]})
        except Exception as error:
            logger = getattr(self._ctx, "logger", None)
            if logger is not None and hasattr(logger, "warn"):
                logger.warn(f'goal-round-driver: could not pause cancelled goal for agent '
                            f'"{loop.id}": {error}')
            self._goals.disarm(loop)

    # ---------- 事件驱动续跑（driver 模式，对齐上游 goal-round-driver） ----------

    @staticmethod
    def _sid(loop) -> int:
        return id(loop.session)

    def _on_session_start(self, payload: dict) -> None:
        """登记 loop 供 driver 模式自动续跑（`agent/session-start` 载波）；
        在途 attempt 一并清除（上游 session-start → attempt=undefined）。"""
        agent = payload.get("agent")
        if agent is not None and getattr(agent, "session", None) is not None:
            self._loops[agent.session.session_id] = agent
            self._attempts.pop(self._sid(agent), None)

    def _on_status(self, payload: dict) -> None:
        """idle 且处于 driver 模式时触发一次续跑（上游 agent/status idle → drive）。

        idle 到达先走 pause fence（被弃 attempt 钉 ref），再请求续跑。"""
        if payload.get("status") != "idle":
            return
        loop = payload.get("agent")
        if loop is None or getattr(loop, "_driver", None) is None:
            return
        self._pause_dropped(loop)
        self._drive(loop)

    def _on_goal_changed(self, payload: dict) -> None:
        """目标变更即请求续跑（上游 goal/changed → requestDrive）：armed 时排首轮。

        host-pause 边界（alpha.1，index.ts:283-294）：宿主发起的 pause 在回合
        运行中时中止 live turn（keepInbox）——模型不能在已被叫停的目标里继续
        行事或同回合复活；模型自身回合内的 pause（update_goal 工具）正常走完。
        判据 = `current_initiator() is not agent`（turn 执行栈内为模型自身）。
        """
        change = (payload or {}).get("change") or {}
        agent = (payload or {}).get("agent")
        if (change.get("operation") == "pause" and agent is not None
                and getattr(agent, "status", None) == "running"
                and current_initiator() is not agent):
            agent.cancel("user", keep_inbox=True)
        for loop in list(self._loops.values()):
            if getattr(loop, "_driver", None) is not None:
                self._drive(loop)

    def _drive(self, loop) -> None:
        """driver 模式单次续跑（对齐上游 goal-round-driver drive）：

        在途 attempt 先按 token 消费（一次 drive 只吃一个：回合运行中再触发
        只置「待重排」而不叠加排队，对齐上游 drive 的 attempt 早退 + 再请求）；
        无在飞且目标 active+armed 时排恰好一个下一轮。round 预算用尽 / 回合
        拒绝 / 终止按上游语义 block/disarm/pause；aborted 回合的 pause 由 idle
        fence 按被弃 attempt 的 ref 决定（resume 复活的目标 revision 已变，
        不再误杀）。reservation 持久到该 round 回合结束（idle 清除），因
        driver 模式 followup 只入队、pre-step 仍需 reservation 校验。
        """
        if getattr(loop, "_driver", None) is None:
            return
        # 上游 readyToDrive：仅在静默（idle）时驱动——回合运行中的触发只置
        # 「待重排」，在途 attempt 留到 idle 由 agent/status 触发再消费
        if getattr(loop, "status", None) != "idle":
            return
        sid = self._sid(loop)
        # 上游 drive()：在途 attempt 先消费即返回（token 语义），同一触发内
        # 立即重入一次以排下一轮（对齐 requestDrive 的 while requested 循环）
        attempt = self._attempts.pop(sid, None)
        self._reservations.pop(sid, None)
        if attempt is not None:
            return self._drive(loop)
        view = self._goals.get(loop)
        if view is None or view["phase"] != "active" or view["activation"] != "armed":
            return
        reason = self._last_turn_end_reason(loop)
        if reason is not None:
            kind = reason.get("kind")
            if kind == "max-tokens":
                self._goals.disarm(loop)
                return
            if kind == "aborted":
                # pause 已由 idle fence 按被弃 attempt ref 决定（或有意跳过：
                # resume 复活的目标 revision 已变）——此处继续按 active+armed
                # 排下一轮（对齐上游 drive()：无 aborted 特判）
                pass
            if kind == "blocked":
                self._block_latest(loop, "prompt-rejected",
                                   "Goal round was rejected before entering its step.")
                return
            if kind == "error":
                logger = getattr(self._ctx, "logger", None)
                if logger is not None and hasattr(logger, "warn"):
                    logger.warn(f"goal round ended in error; stopping continuation: "
                                f"{reason.get('error')}")
                return
        if view["roundsStarted"] >= view["maxGoalRounds"]:
            self._block_latest(loop, "round-limit",
                               f"Goal reached its configured limit of {view['maxGoalRounds']} rounds.")
            return
        round_no = view["roundsStarted"] + 1
        message = create_message(
            "user",
            render_goal_round_prompt(view, round_no),
            {"kind": "goal", "goalId": view["id"], "revision": view["revision"],
             "round": round_no},
        )
        self._reserve(loop, message, round_no)
        self._attempts[sid] = {
            "goalId": view["id"], "revision": view["revision"], "round": round_no,
            "messageId": message["id"], "phase": "queued", "cancelled": False,
        }
        # 不在此清除 reservation：driver 模式回合稍后运行，pre-step 仍需它；
        # 下一轮 idle 到达时由本方法起始处清除。
        loop.followup(message)

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
