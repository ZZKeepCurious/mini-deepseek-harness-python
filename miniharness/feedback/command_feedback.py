"""会话级人工反馈（/feedback 命令 + sessionFeedback Remote）。

上游：packages/feedback/command-feedback/src/index.ts（127 行）+ types.ts。

语义（已核实）：
  * `FEEDBACK_CATEGORIES`：共享分类词表（message-feedback 也用它）。
  * `record_feedback(session, entry)`：text = entry.text.trim() ?? ''；空 text
    省略、无 category 省略（无两者仍记 `{}`——该事件本身授权日志投递）；
    只追加一条 `feedback/record`。
  * `/feedback` 命令：`recordInput: false`（command/run 不带 args——域事件
    拥有 payload）；空/纯空白输入 → error `Feedback text is required.
    Usage: /feedback <text>`；否则记录并回成功两行
    `Feedback recorded for session <id>\nAnonymous user: <id>.`（命令路径
    铸/取匿名用户 id）。
  * `sessionFeedback` Remote：record(sessionId, text?, category?)——会话不在
    → `{ok:false, error:{code:'session-not-found', sessionId}}`；在则记录并
    返回 `{ok:true, value:{recorded:true}}`。Remote 路径不碰匿名用户 id。
  * `feedback/record` 是 log-only 非 surface 事件（无 surfaceOp），不进模型。

载体差异：上游命令路径经 commands 注册表（definitionId）+ Typert Remote；
mini 用 CommandRegistry（record_input=False）+ 本文件 Service 承载 Remote
方法（web/api.py 代理调用），语义不变。
"""
from __future__ import annotations

from typing import Any

from ..commands import CommandInvocation
from ..core.scope import Context, Service
from ..core.session import Session
from ..identity import get_or_create_anonymous_user_id

__all__ = [
    "FEEDBACK_CATEGORIES",
    "FEEDBACK_USAGE",
    "install_command_feedback",
    "record_feedback",
]

#: 共享分类词表（message-feedback 同款，index.ts:30-38）。
FEEDBACK_CATEGORIES = (
    "task-result",
    "instruction-following",
    "product-interaction",
    "service-stability",
    "resource-cost",
    "security-privacy-permission",
    "other",
)

FEEDBACK_USAGE = "Usage: /feedback <text>"

_COMMAND_DESCRIPTION = "Record feedback about this session"


def record_feedback(session: Session, entry: dict) -> None:
    """记录一条会话级反馈（index.ts:58-64）：`feedback/record` 事件。

    text = entry.text.trim() ?? ''；空 text 省略、无 category 省略；两者皆无
    仍记 `{}`（该事件本身授权日志投递）。eager 但不强制 flush——ack 意味
    "已记日志"，非"已落盘"（README）。
    """
    text = (entry.get("text") or "").strip()
    payload: dict[str, Any] = {}
    if text:
        payload["text"] = text
    category = entry.get("category")
    if category is not None:
        payload["category"] = category
    session.append("feedback/record", payload)


def _execute_feedback_command(invocation: CommandInvocation) -> dict:
    """/feedback 命令 handler（index.ts:73-82）。"""
    raw = invocation.raw_input
    if raw.strip() == "":
        return {"kind": "error", "text": f"Feedback text is required. {FEEDBACK_USAGE}"}
    record_feedback(invocation.agent.session, {"text": raw})
    user_id = get_or_create_anonymous_user_id()
    return {
        "kind": "success",
        "text": f"Feedback recorded for session {invocation.agent.session.session_id}"
                f"\nAnonymous user: {user_id}.",
    }


class SessionFeedbackService(Service):
    """`sessionFeedback` Remote 方法面（index.ts:85-110）。"""

    provide = "sessionFeedback"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "sessionFeedback")

    def record(self, request: dict) -> dict:
        """Remote `record`：会话不存在 → 业务失败码；在则记录并确认。"""
        session_id = request.get("sessionId")
        session = self.ctx.get("sessions").get(session_id)
        if session is None:
            raise SessionFeedbackError(session_id)
        record_feedback(session, request)
        return {"recorded": True}


class SessionFeedbackError(Exception):
    """`feedback/record` 目标会话不存在（code='session-not-found'）。"""

    def __init__(self, session_id: str):
        super().__init__(f'session "{session_id}" not found')
        self.code = "session-not-found"
        self.details = {"sessionId": session_id}


def install_command_feedback(ctx: Context) -> dict:
    """装配 command-feedback：`/feedback` 命令（commands 在场时）+ sessionFeedback Remote。

    幂等（重复装返回既有实例）。命令注册仅在 commands 服务在场时进行（上游
    command-feedback `apply` 注入 commands；web 组合未挂 commands 时仅提供
    Remote 面，命令缺席——对齐上游「命令仅在 install_commands 提供时注册」）。
    """
    existing = ctx.get("sessionFeedback")
    if existing is not None:
        return {"command": None, "remote": existing}

    command = None
    commands = ctx.get("commands")
    if commands is not None:
        command = commands.register(
            "feedback", _COMMAND_DESCRIPTION, _execute_feedback_command,
            input_hint="<text>", record_input=False)
    remote = SessionFeedbackService(ctx)
    return {"command": command, "remote": remote}