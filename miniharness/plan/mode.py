"""Plan mode 状态机：log-only `plan/mode` 状态 + prompt section 注入。

上游对照：packages/plan/plan-mode/src/index.ts（PlanModeController）。

契约（与上游一致）：
  * 状态唯一事实来源是会话日志 `plan/mode {active: boolean}`（log-only、
    非 surface、整值替换）；生效状态 = foldPlanMode 对日志前缀折叠，最后一条
    胜出（index.ts:46-55,129-138）。resume/fork 无需 live mirror。
  * 写路径：set() 在 idle（无 open turn）时立即 append；turn 运行中只记录
    pending intent，在下一个"被接受的 in-turn pre-step"（agent/pre-step 决策
    非 reject、未中止）提交（index.ts:425-460）。被拒绝/中止的 step 不提交。
  * 模式切换的"叙述"（narration）：仅当最近一次 request/header 描述的是
    另一模式时，把一句 user 消息注入 step 输入（模型可见 ⟺ 已记录）。
  * plan:policy prompt section（order 50）：plan mode 生效期间向每次模型请求
    注入部署方指引文本（index.ts:225-233）。

mini 简化（教学范围，须在文档中标注）：审查 UI（/plan 命令、exit_plan_mode 工具、
userQuestions 审查通道、plan 投影单元）见 review.py / projection.py（install_plan_review
装配；headless/ACP/SDK 自动化表面不接线）；本模块仍只负责状态机与 prompt section。
system-prompt 的 assemble waterfall、contexts/tools 提供器、variables 插值、
scope 层叠未复现（仅保留 section 注册/渲染）。install_plan_mode 要求 ctx 已提供
systemPrompt 服务（缺失抛 KeyError，fail loud）。
"""
from __future__ import annotations

import logging
from typing import Any, Callable

from ..core.scope import Context, _maybe_await
from ..core.session import Session, create_message, text_block
from .config import resolve_config

__all__ = ["PLAN_POLICY_SECTION", "PlanModeController", "fold_plan_mode", "install_plan_mode"]

logger = logging.getLogger("miniharness.plan")

#: plan:policy prompt section 名（上游 index.ts:225 同款）。
PLAN_POLICY_SECTION = "plan:policy"
#: plan:policy 的 order（上游 index.ts:226 同款，介于人设 0 与工具指引 100-199 之间）。
PLAN_POLICY_ORDER = 50


def fold_plan_mode(events: tuple, end: int | None = None) -> bool:
    """沿日志前缀折叠 plan 状态：最后一条 `plan/mode` 胜出，无则 inactive。

    @param events - 会话日志或其任意前缀。
    @param end - 只折叠 events[0, end)，默认全量。
    @returns 是否处于 plan mode。
    """
    active = False
    for index, event in enumerate(events):
        if end is not None and index >= end:
            break
        if event["type"] == "plan/mode":
            active = event["data"]["active"]
    return active


def _has_open_turn(events: tuple) -> bool:
    open_ = False
    for event in events:
        if event["type"] == "turn/start":
            open_ = True
        elif event["type"] == "turn/end":
            open_ = False
    return open_


def _plan_mode_at_last_header(events: tuple) -> bool | None:
    """最近一次 request/header 处的生效状态；尚无 header 返回 None。

    上游 planModeAtLastHeader（index.ts:168-177）：叙述只在该 header 描述
    另一模式时发出。
    """
    last_header = -1
    for index, event in enumerate(events):
        if event["type"] == "request/header":
            last_header = index
    if last_header < 0:
        return None
    return fold_plan_mode(events, last_header + 1)


class PlanModeController:
    """持有 per-session pending intent 与 plan:policy 节，接线 agent/pre-step。"""

    def __init__(self, ctx: Context, config: dict | None = None):
        self._ctx = ctx
        self._section = resolve_config(config)["section"]
        # id(session) -> {"active": bool, "narrate": bool}（latest selection）
        self._pending: dict[int, dict] = {}
        # plan:policy 节（order 50）；要求先 install_system_prompt（与上游
        # PlanModeController 声明 inject systemPrompt 一致，缺失 fail loud）。
        system_prompt = ctx.inject("systemPrompt")
        system_prompt.section(
            PLAN_POLICY_SECTION,
            PLAN_POLICY_ORDER,
            lambda context: self._policy_text(context.get("agent")),
        )
        ctx.on("agent/pre-step", self._on_pre_step)

    # ---------- 内部 ----------

    def _pending_for(self, session: Session) -> dict | None:
        return self._pending.get(id(session))

    def _policy_text(self, agent: Any) -> str:
        """plan mode 生效（含 pending 选择）时返回部署方指引，否则空串。"""
        if agent is None:
            return ""
        session = agent.session
        pending = self._pending_for(session)
        active = pending["active"] if pending is not None else fold_plan_mode(session.events)
        return self._section if active else ""

    async def _on_pre_step(self, payload: dict, next_fn: Callable) -> dict:
        """被接受的 in-turn pre-step 提交 pending 选择；拒绝/中止不提交。"""
        decision = await _maybe_await(next_fn())
        agent = payload.get("agent")
        signal = payload.get("signal")
        if agent is None:
            return decision
        pending = self._pending_for(agent.session)
        if (isinstance(decision, dict) and decision.get("kind") == "reject") \
                or (signal is not None and getattr(signal, "aborted", False)) \
                or pending is None:
            return decision
        narration = self._narration(agent.session, pending["active"])
        try:
            self._commit_pending(agent.session)
        except Exception as error:  # noqa: BLE001 - 追加失败保持 pending，下次 accepted 重试
            logger.warning("dsh-plan-mode: failed to append selected plan mode at step start: %s", error)
            return decision
        if not pending["narrate"] or narration is None:
            return decision
        messages = list(decision.get("messages") or []) if isinstance(decision, dict) else []
        messages.append(narration)
        return {**decision, "messages": messages}

    def _commit_pending(self, session: Session) -> None:
        """追加一次 pending 选择并清空 intent（上游 _commitPending 同款）。"""
        pending = self._pending_for(session)
        if pending is None:
            return
        session.append("plan/mode", {"active": pending["active"]})
        # 追加成功才清 pending：失败的 durable 写在下次 accepted pre-step 可重试
        del self._pending[id(session)]

    def _narration(self, session: Session, target: bool):
        """最近 header 描述另一模式时构造一句 user 叙述（无则 None）。"""
        told = _plan_mode_at_last_header(session.events)
        if told is None or told == target:
            return None
        text = "The user switched this session to plan mode." if target \
            else "The user switched this session back to the default mode."
        return create_message(
            "user", [text_block(text)],
            {"kind": "plugin", "plugin": "plan-mode"},
        )

    # ---------- 公开 API（对齐上游 PlanModeController.get/set） ----------

    def get(self, agent: Any) -> dict:
        """读取生效状态与（如有）pending 选择。"""
        active = fold_plan_mode(agent.session.events)
        pending = self._pending_for(agent.session)
        if pending is None:
            return {"active": active}
        return {"active": active, "pending": pending["active"]}

    def _queue_exit(self, agent: Any) -> None:
        """exit_plan_mode 批准后排队 silent 选择（narrate=False，结果已叙述）。

        上游 index.ts:379（this.pendingIntents.set(agent.session,
        {active:false, narrate:false})）：本次 assistant 工具批次的剩余步骤
        保持 plan 指引，下次被接受的 in-turn pre-step 提交。
        """
        self._pending[id(agent.session)] = {"active": False, "narrate": False}

    def set(self, agent: Any, active: bool) -> str:
        """选择 plan mode 状态；返回 committed/queued/cancelled/noop（上游 index.ts:425-445）。

        noop = 选择与当前（生效或已 pending）状态一致；
        queued = turn 运行中记 pending，下次被接受的 in-turn pre-step 提交；
        cancelled = 反向 pending 选择被清除、生效状态已匹配目标（上游同语义）；
        committed = 无 open turn，立即 append 并叙述。
        """
        session = agent.session
        fold = fold_plan_mode(session.events)
        pending = self._pending_for(session)
        target = pending["active"] if pending is not None else fold
        if active == target:
            return "noop"
        if _has_open_turn(session.events):
            self._pending[id(session)] = {"active": active, "narrate": True}
            return "cancelled" if fold == active else "queued"
        # 无 open turn
        if active == fold:
            if pending is not None:
                del self._pending[id(session)]
            return "cancelled"
        session.append("plan/mode", {"active": active})
        # 追加成功才清 pending：失败的 durable 写在下次 accepted pre-step 可重试
        if pending is not None:
            del self._pending[id(session)]
        narration = self._narration(session, active)
        if narration is not None:
            agent.inject(narration["content"][0]["text"], source="plan-mode")
        return "committed"


def install_plan_mode(ctx: Context, config: dict | None = None) -> PlanModeController:
    """安装 plan mode：注册 plan:policy 节 + agent/pre-step 提交接线。

    要求 ctx 已提供 systemPrompt 服务（先 install_system_prompt；缺失抛
    KeyError，fail loud）。不注册任何工具/命令——审查 UI 后置（报告 04 议题 8）。
    """
    return PlanModeController(ctx, config)
