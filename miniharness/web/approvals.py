"""web 审批桥：`tools/ask` 闸门 → `$events` approval/request waterfall。

对齐上游 `packages/interaction/user-approval` + `packages/api/remotes` 的远程
approval 转发（last-resort 转发到浏览器 approval/request），mini 只落浏览器：

  * 工具的 `tools/ask` 闸门（core/tools.py pipeline_policy：pre-execute 判
    kind=='ask'）挂起 pending、以 `approval/request` waterfall 投递给所有
    `$events` 客户端。request 为上游 ApprovalRequest
    （user-approval/src/types.ts: `{toolName, callId?, reason?, displayReason?}`）：
    闸门载荷里 ask 决策带的 `reason`/`displayReason` 原样转发，`callId` 取本次
    工具执行（供前端把审批关联到对应工具卡片）。
  * 首个客户端经 HTTP `$events/result` 结算（registry.invoke 返回 kind/value）：
      'result' value ∈ APPROVAL_OUTCOMES → 原样；否则 'unavailable'（fail-closed）
      'rejected' → 'unavailable'（fail-closed）
      'next'     → 委托 `nxt()`（上游 last-resort 转发到下一 answerer）
      'cancelled'→ 'cancelled'（registry.dispose 全量结算）
    只有 'allowed-once' 是真授限（闸门返回 True）。
  * 每次问询自落审计对 approval/asked + approval/decided（turn-enclosed、
    log-only，与 ApprovalService 同款形状）。

mini 接线点与上游不同（教学简化，须在 AGENTS.md 标注）：上游 approval 入口是
`ctx.on('approval/request')`（宿主从会话日志倒扫最新未决 approval/asked 认领
callId 对称配对），且 answerer 链是 async waterfall + AbortSignal 竞争；mini 的
核心 ApprovalService（interaction/approval.py）是同步独立教学面，本桥仍挂在
async `tools/ask` 管线闸门（core/tools.py pipeline_policy），wire 上以
`approval/request` waterfall 事件呈现。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

__all__ = ["RemoteApprovalBridge", "APPROVAL_OUTCOMES"]

#: 上游 ApprovalOutcome 闭集（user-approval/src/types.ts:32）
APPROVAL_OUTCOMES = frozenset(
    ("allowed-once", "rejected", "cancelled", "unavailable"))


class RemoteApprovalBridge:
    """tools/ask → `$events` approval/request waterfall 的双向桥。

    `GatewayStreams` 构造时创建（`streams.approvals`）；每个会话 loop attach 时
    经 `install(loop)` 在 loop 的 ctx 上注册 async `tools/ask` answerer。
    """

    def __init__(self, streams: Any):
        self.streams = streams
        self.api = streams.api
        self._disposers: list[Any] = []

    # ---------- 装配 ----------

    def install(self, loop: Any) -> None:
        """在 loop 的 ctx 上注册 async tools/ask answerer（web 会话恒 async 管线）。"""
        session = loop.session
        session_id = session.session_id
        ctx = loop.ctx

        async def ask(payload: dict, nxt: Any) -> bool:
            tool_name = payload.get("tool") or "unknown"
            exec_ = payload.get("exec")
            call_id = getattr(exec_, "call_id", None)
            reason = payload.get("reason")
            display_reason = payload.get("displayReason")
            approval_id = str(uuid.uuid4())
            asked: dict = {"id": approval_id, "toolName": tool_name}
            if call_id is not None:
                asked["callId"] = call_id
            if reason is not None:
                asked["reason"] = reason
            session.append("approval/asked", asked)
            outcome = await self._request(
                session_id, tool_name, nxt, call_id, reason, display_reason)
            session.append("approval/decided", {"id": approval_id, "outcome": outcome})
            return outcome == "allowed-once"

        self._disposers.append(ctx.on("tools/ask", ask))

    async def _request(self, session_id: str, tool_name: str, nxt: Any,
                       call_id: str | None = None, reason: str | None = None,
                       display_reason: dict | None = None) -> str:
        """投递 approval/request waterfall 并归一化结局（本地 answerer 同款形状）。"""
        request: dict = {"toolName": tool_name}
        if call_id is not None:
            request["callId"] = call_id
        if reason is not None:
            request["reason"] = reason
        if display_reason is not None:
            request["displayReason"] = display_reason
        try:
            kind, value = await self.streams.events.invoke(
                "approval/request", session_id, request)
        except asyncio.CancelledError:
            return "cancelled"
        if kind == "cancelled":
            return "cancelled"
        if kind == "next":
            return await nxt()
        if kind == "rejected":
            return "unavailable"
        # wire 边界：outcome.value 是客户端任意 JSON，非字符串一律 fail-closed
        # （裸字符串才是上游约定，见 user-approval/src/types.ts:32）。
        if isinstance(value, str) and value in APPROVAL_OUTCOMES:
            return value
        return "unavailable"

    # ---------- 生命周期 ----------

    def dispose(self) -> None:
        for fn in reversed(self._disposers):
            fn()
        self._disposers.clear()
