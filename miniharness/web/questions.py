"""web 用户提问桥：`user-questions/request` 瀑布 → `$events` waterfall。

对齐上游 `packages/api/remotes` 的 `user-questions/request` 转发 +
`packages/api/gateway` 的 request 投影（去掉 Agent 对象与 signal，agent 写成
id）。服务 `ask()` 走 `ctx.awaterfall`；本桥挂在会话 loop 的 ctx 上，把请求
投递给 `$events` 客户端，经 `$events/result` 结算：

  * `result` value → 原样回给 ask（`{answers:[...]}`）
  * `next` → 委托 `nxt()`（链尾无应答者则 NO_PROVIDER）
  * `rejected` 带 UserQuestionError 形状 → 还原为域错误
  * `rejected` 无 code → RuntimeError（消息透传）
  * `cancelled` / dispose → ASK_ABORTED
"""
from __future__ import annotations

import asyncio
from typing import Any

from ..interaction.user_questions import (
    UserQuestionError,
    aborted_question,
    restore_user_question_error,
)
from .stream_protocol import is_remote_json_value

__all__ = ["RemoteQuestionBridge"]


def _agent_id(payload: dict, session_id: str) -> str:
    agent = payload.get("agent")
    identity = getattr(agent, "id", None)
    if isinstance(identity, str) and identity != "":
        return identity
    session = getattr(agent, "session", None)
    session_identity = getattr(session, "session_id", None)
    if isinstance(session_identity, str) and session_identity != "":
        return session_identity
    return session_id


def _project_request(payload: dict, agent_id: str) -> dict:
    """Host 请求 → wire：去掉 agent 对象与 signal，agent 写成 id。"""
    questions = payload.get("questions")
    if not is_remote_json_value(questions):
        raise TypeError("user-questions/request questions must be lossless JSON")
    request = {"questions": questions, "agent": agent_id}
    wait = payload.get("wait")
    if wait is not None:
        if not is_remote_json_value(wait):
            raise TypeError("user-questions/request wait must be lossless JSON")
        request["wait"] = wait
    if not is_remote_json_value(request):
        raise TypeError("user-questions/request is not lossless JSON data")
    return request


class RemoteQuestionBridge:
    """`user-questions/request` → `$events` waterfall 的双向桥。

    `GatewayStreams` 构造时创建（`streams.questions`）；每个会话 loop attach
    时经 `install(loop)` 在 loop 的 ctx 上注册 async waterfall 应答者。
    """

    def __init__(self, streams: Any):
        self.streams = streams
        self.api = streams.api
        self._disposers: list[Any] = []

    def install(self, loop: Any) -> None:
        session_id = loop.session.session_id
        ctx = loop.ctx

        async def answer(payload: dict, nxt: Any) -> Any:
            agent_id = _agent_id(payload, session_id)
            request = _project_request(payload, agent_id)
            try:
                kind, value = await self.streams.events.invoke(
                    "user-questions/request",
                    agent_id,
                    request,
                    signal=payload.get("signal"),
                )
            except asyncio.CancelledError:
                raise aborted_question() from None
            if kind == "cancelled":
                raise aborted_question(value)
            if kind == "next":
                return await nxt()
            if kind == "rejected":
                restored = restore_user_question_error(value)
                if isinstance(restored, UserQuestionError):
                    raise restored
                message = value.get("message") if isinstance(value, dict) else str(value)
                raise RuntimeError(message)
            return value

        self._disposers.append(ctx.on("user-questions/request", answer))

    def dispose(self) -> None:
        for fn in reversed(self._disposers):
            fn()
        self._disposers.clear()
