"""web 审批桥：把 `tools/ask` 问询接入 mux 帧 + `POST /api/respond`。

对齐上游 `packages/host/apiproxy` 的 approval 通道（api-proxy.ts + api/approvals.ts）：

  * 可应答交互的 wire 形态是 `server-request`（method = 'approval/requested'，
    rpcId = pending 稳定 id）+ 回显 rpcId 的 `client-response`（`POST /api/respond`
    的 body）。HTTP 响应体是载体层 `RpcReceipt`：{accepted:true} | {accepted:false,
    reason:'not-pending'|'bad-response'}。
  * `POST /api/respond` 的 payload：{sessionId, approvalId, outcome}，outcome ∈
    {'allowed-once','rejected'}；sessionId/approvalId 与 pending 不符、result 不
    合法、outcome 非词汇表 → 'bad-response'；rpcId 无对应 pending → 'not-pending'。
  * 网关 dispose → 全部 pending 以 'cancelled' 结算（无浏览器应答的挂起问询
    不悬挂；answerer 对 'cancelled' fail-closed 为拒绝）。

mini 接线点与上游不同（教学简化，须在 AGENTS.md 标注）：上游的 approval 入口
是 `ctx.on('approval/request')`（宿主从会话日志倒扫最新未决 approval/asked 认领
callId 对称配对）；mini 的审批能力（ApprovalService + approval/request 瀑布）是
独立教学面，web 桥直接接在**工具管线闸门** `tools/ask`（core/tools.py
pipeline_policy_async，web 会话恒走 async 管线）——工具被 `tools/pre-execute`
判为 'ask' 时，answerer 挂起 pending、广播 approval/requested、await 用户应答。
桥为每次问询同步落盘审计对 approval/asked + approval/decided（turn-enclosed、
log-only，与 ApprovalService 同款形状）。
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any

from .envelope import rpc_receipt_accepted, rpc_receipt_rejected, rpc_id

__all__ = ["ApprovalBridge", "PendingApproval"]

#: POST /api/respond 接受的 outcome 词汇表（上游 ApprovalOutcome 子集：
#: 浏览器可投出 allowed-once / rejected；cancelled/unavailable 归服务端）
_ANSWERABLE_OUTCOMES = ("allowed-once", "rejected")


class PendingApproval:
    """一次未决的浏览器问询：稳定 rpcId 供应答路由 + future 供 answerer 等待。"""

    def __init__(self, rpc_id_: str, session_id: str, approval_id: str,
                 tool_name: str, future: Any):
        self.rpcId = rpc_id_
        self.sessionId = session_id
        self.approvalId = approval_id
        self.toolName = tool_name
        self.future = future
        self.settled = False


class ApprovalBridge:
    """tools/ask → mux 帧 → /api/respond 的双向桥。

    WebApi 在构造时创建本桥（`api.approvals`）；StreamHub 构造时把自身挂到
    `bridge.hub` 供广播/重放。每个会话 loop attach 时经 `install(loop)` 在
    loop 的 ctx 上注册 async `tools/ask` 监听器。
    """

    def __init__(self, api: Any):
        self.api = api
        self.hub: Any = None          # StreamHub 构造时挂入（广播/重放通道）
        self._pending: dict[str, PendingApproval] = {}
        self._disposers: list[Any] = []

    # ---------- 装配 ----------

    def install(self, loop: Any) -> None:
        """在 loop 的 ctx 上注册 async tools/ask answerer（web 会话恒 async 管线）。"""
        session = loop.session
        session_id = session.session_id
        ctx = loop.ctx

        async def ask(payload: dict, nxt: Any) -> bool:
            tool_name = payload.get("tool") or "unknown"
            approval_id = str(uuid.uuid4())
            pending_id = rpc_id()
            future = asyncio.get_running_loop().create_future()
            pending = PendingApproval(pending_id, session_id, approval_id, tool_name, future)
            self._pending[pending_id] = pending
            # 审计对：approval/asked 先落盘，decided 在结算后落盘（turn-enclosed）
            session.append("approval/asked", {"id": approval_id, "toolName": tool_name})
            self._broadcast({
                "type": "approval/requested", "rpcId": pending_id,
                "sessionId": session_id, "approvalId": approval_id,
                "toolName": tool_name,
            })
            try:
                outcome = await future
            finally:
                self._pending.pop(pending_id, None)
                pending.settled = True
            session.append("approval/decided", {"id": approval_id, "outcome": outcome})
            self._broadcast({
                "type": "approval/resolved", "rpcId": pending_id,
                "sessionId": session_id, "approvalId": approval_id, "outcome": outcome,
            })
            return outcome == "allowed-once"

        self._disposers.append(ctx.on("tools/ask", ask))

    def _broadcast(self, frame: dict) -> None:
        if self.hub is not None:
            self.hub.emit_mux(frame)

    # ---------- 应答入口（POST /api/respond 的载体回调） ----------

    def respond(self, message: dict) -> dict:
        """处理一条 client-response，返回载体层 RpcReceipt。"""
        pending = self._lookup(message)
        if pending is None:
            return rpc_receipt_rejected("not-pending")
        value = self._answer_value(pending, message)
        if value is None:
            return rpc_receipt_rejected("bad-response")
        self._resolve(pending, value["outcome"])
        return rpc_receipt_accepted()

    def _lookup(self, message: dict) -> PendingApproval | None:
        rpc_id_ = message.get("rpcId")
        if not isinstance(rpc_id_, str):
            return None
        return self._pending.get(rpc_id_)

    @staticmethod
    def _answer_value(pending: PendingApproval, message: dict) -> dict | None:
        """校验 client-response 的 result.value：{sessionId, approvalId, outcome}。

        sessionId/approvalId 必须与 pending 逐字一致（应答不可偷换问询目标），
        outcome 必须在浏览器可投词汇表内；否则视为坏响应（bad-response）。
        """
        result = message.get("result")
        if not isinstance(result, dict) or result.get("ok") is not True:
            return None
        value = result.get("value")
        if not isinstance(value, dict):
            return None
        if value.get("outcome") not in _ANSWERABLE_OUTCOMES:
            return None
        if value.get("sessionId") != pending.sessionId or value.get("approvalId") != pending.approvalId:
            return None
        return value

    def _resolve(self, pending: PendingApproval, outcome: str) -> None:
        if pending.future.done():
            return
        loop = pending.future.get_loop()
        loop.call_soon_threadsafe(pending.future.set_result, outcome)

    # ---------- mux 重连重放 ----------

    def replay_mux(self, queue: asyncio.Queue) -> None:
        """重连基线：补发仍挂起的 approval/requested（复用原 rpcId，对齐 mux-open）。"""
        for pending in list(self._pending.values()):
            if pending.settled:
                continue
            try:
                queue.put_nowait({
                    "type": "approval/requested", "rpcId": pending.rpcId,
                    "sessionId": pending.sessionId, "approvalId": pending.approvalId,
                    "toolName": pending.toolName,
                })
            except asyncio.QueueFull:
                pass

    # ---------- 生命周期 ----------

    def dispose(self) -> None:
        for fn in reversed(self._disposers):
            fn()
        self._disposers.clear()
        for pending in list(self._pending.values()):
            self._resolve(pending, "cancelled")
            pending.settled = True
        self._pending.clear()