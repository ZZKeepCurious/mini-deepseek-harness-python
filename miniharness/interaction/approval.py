"""第 9 章：审批 —— ApprovalPolicy 两档 + 审计事件对。

对应 dsh 真实源码：packages/interaction/user-approval（ApprovalService）。

上游语义（已核实，src/index.ts + src/types.ts）：
  * ApprovalPolicy = 'ask' | 'never'。'ask'（默认）委托给组合 answerers，
    无 answerer 时 fail-closed 为 'unavailable'；'never' 在交互式派发之前
    确定性拒绝（每个 ask 解析 'rejected'，不提示任何人）。
  * ApprovalOutcome = 'allowed-once' | 'rejected' | 'cancelled' | 'unavailable'
    （closed union；'allowed-once' 是唯一授权，调用方对 'unavailable' fail closed）。
  * 审计对：approval/asked（id, toolName, callId?, reason?）+ approval/decided
    （id, outcome）——log-only，不是 surface 事件（无 surfaceOp），模型只看到
    由此产生的工具结果，看不见审计本身。
  * approval/policy 是 durable 可重放策略开关，最后一条胜出（纯 fold，resume
    无需任何追赶机制——重放日志即状态）。
  * request() 前置条件：必须处于 open turn（turn/start 未闭合），审计对必须
    turn-enclosed，否则抛错且不追加任何东西（turn 是日志的 commit/replay
    边界，两次 turn 之间的裸事件在重载时与崩溃尾部无法区分，会被静默丢弃）。
  * decide 顺序（index.ts:304-344）：signal aborted → 'cancelled'；policy
    'never' → 'rejected'（在派发前由服务自身决定，保证注册顺序无关的确定性）；
    否则 waterfall 派发 'approval/request'，无监听器/抛错 → 'unavailable'，
    非词汇表返回值归一化 'unavailable'。
  * setApprovalPolicy 无效值在日志变更前抛 TypeError。

载体简化：上游派发是 async waterfall + AbortSignal 竞争（abort 赢 → 'cancelled'，
迟到 answer 被丢弃）；mini 用同步 waterfall 近似，abort 只在派发前检查。
"""
from __future__ import annotations

import uuid

from ..core.scope import Context
from ..core.session import Session

APPROVAL_OUTCOMES = ("allowed-once", "rejected", "cancelled", "unavailable")
APPROVAL_POLICIES = ("ask", "never")

#: 策略上下文句子（上游 index.ts:73-75，逐字）。
_NEVER_SENTENCE = (
    "Approval prompts are disabled in this session: actions that require "
    "approval are rejected automatically — do not request sandbox escalation "
    "(do not set `sandbox_permissions`)."
)
_ASK_SENTENCE = (
    "Approval policy: ask. Operations that require approval may ask through "
    "the configured answerers; without an available answerer, the request "
    "fails closed."
)


def approval_policy_context_text(policy: str) -> str:
    """按策略渲染 `approval:policy` 上下文正文（index.ts:162-174 同款）。"""
    if policy == "never":
        return _NEVER_SENTENCE
    if policy == "ask":
        return _ASK_SENTENCE
    raise ValueError(f"unknown approval policy: {policy!r}")


def effective_approval_policy(events: list | tuple) -> str | None:
    """会话级策略覆盖：日志中最后一条 approval/policy 的 policy，无则 None。

    纯 fold（上游 index.ts:112-118 同构）：resume 无需追赶，重放日志即状态。
    """
    for event in reversed(events):
        if event["type"] == "approval/policy":
            return event["data"]["policy"]
    return None


def set_approval_policy(session: Session, policy: str) -> None:
    """追加一次会话策略覆盖；无效值在日志变更前抛 TypeError。"""
    if policy not in APPROVAL_POLICIES:
        raise TypeError('approval policy must be one of "ask" or "never"')
    session.append("approval/policy", {"policy": policy})


def has_open_turn(events: list | tuple) -> bool:
    """日志当前是否处于 open turn（turn/start 未闭合）。从后往前扫描。"""
    for event in reversed(events):
        if event["type"] == "turn/start":
            return True
        if event["type"] == "turn/end":
            return False
    return False


class ApprovalService:
    """审批能力：策略先行 + answerer 瀑布 + 审计对（对齐 ApprovalService）。

    Config：`policy`（缺省 'ask'）。装配时挂 `approval:policy` 系统提示词
    context（按会话决议当前策略，上游 index.ts:162-174 同款）。
    """

    def __init__(self, ctx: Context, policy: str = "ask"):
        if policy not in APPROVAL_POLICIES:
            raise TypeError('approval policy must be one of "ask" or "never"')
        self.ctx = ctx
        self._config_policy = policy
        # 装配系统提示词 context：每会话按 effective 策略渲染（上游挂
        # `approval:policy` + getContextOrder('APPROVAL_POLICY')）。
        ctx.inject(["systemPrompt"], self._mount_prompt_context)

    def _mount_prompt_context(self, ctx: Context, _config=None) -> None:
        prompt = ctx.get("systemPrompt")
        if prompt is None:
            return
        prompt.context(
            name="approval:policy",
            order=120,
            text=lambda context: self._policy_context_text(context),
        )

    def _policy_context_text(self, context: dict) -> str:
        session = (context or {}).get("session")
        if session is None:
            return ""
        policy = self.effective_policy(session)
        return approval_policy_context_text(policy)

    # ---------- 策略 ----------

    def effective_policy(self, session: Session) -> str:
        """会话自己的 approval/policy 覆盖，否则回退配置默认。"""
        return effective_approval_policy(session.events) or self._config_policy

    def set_policy(self, agent: Any, policy: str) -> None:
        """运行时切换策略：写入 durable 覆盖 + 注入模型可见变更通知。

        对齐上游 setPolicy（index.ts:184-195）：previous = effective；无变化
        no-op；否则 setApprovalPolicy + `agent.inject` 一条
        `The approval policy changed from "<prev>" to "<policy>" (changed by
        the user).` 通知（source `{kind:'user-approval'}`）排进下个模型 step。
        """
        previous = self.effective_policy(agent.session)
        if previous == policy:
            return
        set_approval_policy(agent.session, policy)
        from ..core.agent_loop.agent import AgentLoop  # noqa: F401 - 只做类型提示
        from ..core.session.message import create_message, text_block
        notice = create_message(
            "user",
            [text_block(f'The approval policy changed from "{previous}" to '
                        f'"{policy}" (changed by the user).')],
            {"kind": "user-approval"},
        )
        agent.inject(notice)

    # ---------- 请求 ----------

    def request(self, session: Session, tool_name: str,
                call_id: str | None = None, reason: str | None = None,
                signal: object | None = None) -> str:
        """问一次决策，返回 closed outcome；'allowed-once' 是唯一授权。

        前置条件：open turn（审计对必须 turn-enclosed）。每个 ask 都追加
        approval/asked + approval/decided 一对（id 配对），审计追加前失败
        直接抛错——绝不返回一项未记录的决定。
        """
        if not has_open_turn(session.events):
            raise RuntimeError(
                "approval.request() outside an open turn: the approval/asked "
                "+ approval/decided audit pair must be turn-enclosed "
                "(a bare event between turns is crash-tail garbage on reload)."
            )
        req_id = str(uuid.uuid4())
        asked: dict = {"id": req_id, "toolName": tool_name}
        if call_id is not None:
            asked["callId"] = call_id
        if reason is not None:
            asked["reason"] = reason
        session.append("approval/asked", asked)
        outcome = self._decide(session, tool_name, call_id, reason, signal)
        session.append("approval/decided", {"id": req_id, "outcome": outcome})
        return outcome

    # ---------- 内部 ----------

    def _decide(self, session: Session, tool_name: str,
                call_id: str | None, reason: str | None, signal: object | None) -> str:
        if signal is not None and getattr(signal, "aborted", False):
            return "cancelled"
        # 'never' 在这里、在任何派发之前决定：监听器形状的拦截器无法保证
        # 注册顺序无关的确定性，只有服务自己的 request 路径可以（上游注释）。
        if self.effective_policy(session) == "never":
            return "rejected"
        req = {"toolName": tool_name}
        if call_id is not None:
            req["callId"] = call_id
        if reason is not None:
            req["reason"] = reason
        try:
            answer = self.ctx.waterfall("approval/request", req)
        except Exception:
            return "unavailable"
        if answer not in APPROVAL_OUTCOMES:
            return "unavailable"
        return answer