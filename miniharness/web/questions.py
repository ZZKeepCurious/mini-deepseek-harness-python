"""web 用户提问桥：`user-questions/request` 瀑布 → `$events` 转发 + 结算。

对齐上游 `packages/interaction/user-questions` 的远程应答者（bundle/web-app
cordis.patch.yml:350-351 挂 ui-user-questions）+ `packages/api/remotes` 的
Remote 转发（remote-events.ts:37 `user-questions/request` waterfall）：

  * UserQuestionService 的无应答者瀑布投递经本桥转发给所有 `$events` 客户端；
    request 只带 `{questions, agent}`（agent 投影为 id，wire 简化标注见
    verified-diffs）。首个客户端经 HTTP `$events/result` 结算
    （registry.invoke 返回 kind/value）：
      'result'   → 原样 value（canonical answers）
      'next'     → 委托 `nxt()`（上游 last-resort 转发到下一 answerer）
      'rejected' → 还原 wire 错误：`{name, message, code}` 形状恢复为域错误
                   （ASK_CANCELLED 等）抛回服务面；无 code 形状 → RuntimeError
      'cancelled'→ ASK_ABORTED（registry.dispose 全量结算）
      asyncio 取消 → ASK_ABORTED

mini 简化（须标注）：上游应答者持久挂在 Agent-scoped answerer waterfall 上并
与 AbortSignal 竞争（signal 中断即 abort 请求）；mini 的桥 `install(loop)` 每
loop 注册一次 async answerer，signal 竞争交由其按序 Awaitable（事件循环）承担
——同 approval 桥（web/approvals.py）的 wire 上异步投递。wire 不含 `signal`
（不可序列化）：客户端取消由 `$events` cancel 帧承载——调用方取消（回合 cancel）
经 `RemoteEventRegistry.invoke` 的 CancelledError 分支向客户端发 cancel 帧
（等价上游 signal 中止客户端 pending），网关 dispose 则全量 'cancelled'。
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..interaction.user_questions import (
    UserQuestionError,
    aborted_question,
)

__all__ = ["RemoteQuestionBridge"]


def _restore_wire_error(value: Any) -> BaseException:
    """把客户端 rejected 载荷还原为可抛异常（域形状 → UserQuestionError）。"""
    if (isinstance(value, dict)
            and isinstance(value.get("name"), str)
            and isinstance(value.get("message"), str)
            and isinstance(value.get("code"), str)):
        return UserQuestionError(value["message"], value["code"], cause=value)
    message = value.get("message") if isinstance(value, dict) else None
    return RuntimeError(message if isinstance(message, str) else str(value))


class RemoteQuestionBridge:
    """ctx.userQuestions 服务 → `$events` user-questions/request 瀑布的双向桥。

    `GatewayStreams` 构造时创建（`streams.questions`）；每个会话 loop attach 时
    经 `install(loop)` 在 loop 的 ctx 上注册 async answerer。
    """

    def __init__(self, streams: Any):
        self.streams = streams
        self.api = streams.api
        self._disposers: list[Any] = []

    # ---------- 装配 ----------

    def install(self, loop: Any) -> None:
        """在 loop 的 ctx 上注册 async user-questions/request answerer。"""
        session_id = loop.session.session_id
        ctx = loop.ctx

        async def answer(request: dict, nxt: Any) -> Any:
            wire = {"questions": request["questions"]}
            agent = request.get("agent")
            if agent is not None:
                wire["agent"] = agent.id
            try:
                kind, value = await self.streams.events.invoke(
                    "user-questions/request", session_id, wire)
            except asyncio.CancelledError:
                raise aborted_question() from None
            if kind == "result":
                return value
            if kind == "next":
                return await nxt()
            if kind == "rejected":
                raise _restore_wire_error(value)
            # 'cancelled'（registry.dispose 全量结算）与未知帧：一律按中止处理
            raise aborted_question()

        self._disposers.append(ctx.on("user-questions/request", answer))

    # ---------- 生命周期 ----------

    def dispose(self) -> None:
        for fn in reversed(self._disposers):
            fn()
        self._disposers.clear()