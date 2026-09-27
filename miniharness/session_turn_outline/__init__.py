"""会话回合大纲投影单元（turnOutline，对齐 session-turn-outline 的 turnOutline 单元）。

上游：packages/session/session-turn-outline/src/projection.ts（137 行）+ types.ts。

`turnOutline` 是 `ctx.sessionProjections` 上的一个 wire 投影单元：把
turn/start 边界、首个人类 prompt 与最终 assistant 回复折成整日志的回合
大纲（聊天轨道对分页事件窗口之外的回合渲染用）。`turn/start`（而非
prompt 的 user/message）锚定每一条目，因为其 seq 是跳转的 load-through
目标：loop 在回合的 prompt 与 steps 之前落 turn/start，向后翻到该 seq
的窗口包含整个回合。

fold 语义（projection.ts:90-132）：
  * turn/start：order guard——不前进 turn 号的边界保持大纲有序（重试回合
    的预览落在常驻条目上），否则 append {turn, seq, prompt:'', response:''}
    并重置 draft。
  * user/message：仅人类来源（source.kind === 'user'）；只有最新回合还能
    等开首人类 prompt（last.prompt === ''）；preview 首块即胜（steering
    保留第一个预览）；空预览不落。
  * assistant/message：最新含文本消息胜（draft 缓冲，turn/end 才提交）；
    draft-only 变更保持 `turns` 数组身份（注册表 is 变更门据此让变更 feed
    安静——每回合至多三次推送：边界、prompt、落定回复）。
  * turn/end：draft 非空才提交到条目 response 并清空；无 draft 静默。

state（types.ts）：{turns: [{turn, seq, prompt, response}], draft}；
wire view = state.turns（裸数组）；stateVersion 2。

载体差异：上游 zod schema（superRefine 严格递增 turn）→ mini 运行时
state_schema/view_schema 可调用校验器（抛 ValueError 拒绝坏检查点）。
"""
from __future__ import annotations

import re
from types import MappingProxyType
from typing import Any, Callable

from ..session_projection import ProjectionDefinition

__all__ = [
    "PROMPT_PREVIEW_LIMIT",
    "RESPONSE_PREVIEW_LIMIT",
    "TURN_OUTLINE_STATE_VERSION",
    "install_turn_outline",
    "preview",
    "turn_outline_projection",
]

#: prompt 预算：一条轨道卡片行（上游 projection.ts:29）。
PROMPT_PREVIEW_LIMIT = 50
#: response 预算：三条轨道卡片行（上游 projection.ts:31）。
RESPONSE_PREVIEW_LIMIT = 120
#: 持久缓存失效版本（上游 stateVersion: 2）。
TURN_OUTLINE_STATE_VERSION = 2

_WS_RE = re.compile(r"\s+", re.UNICODE)


def preview(content: Any, limit: int) -> str:
    """文本块 space-join + 空白折叠 + 超限省略号截断（projection.ts:36-59）。

    逐块有界：fold 在每条消息事件上跑，多兆字节单块不得整块拼接（和
    regex 归一）只为一条短预览。块裁剪到 `limit*2` 字符；拼接文本超
    `limit-1` → 前 `limit-1` 字符 rstrip 后加 `…`；未超但读到被裁剪块 →
    原文后加 `…`。
    """
    text = ""
    unread = False
    for block in content or []:
        if not isinstance(block, (dict, MappingProxyType)) or block.get("type") != "text":
            continue
        raw = block.get("text")
        if not isinstance(raw, str):
            continue
        if len(text) >= limit * 2:
            unread = True
            break
        clipped = len(raw) > limit * 2
        chunk = raw[: limit * 2] if clipped else raw
        text += chunk if text == "" else f" {chunk}"
        if clipped:
            unread = True
            break
    normalized = _WS_RE.sub(" ", text).strip()
    if len(normalized) > limit - 1:
        return normalized[: limit - 1].rstrip() + "\u2026"
    return f"{normalized}\u2026" if unread else normalized


# ---------- 校验器（对齐 zod schema） ----------


def _validate_entries(value: Any) -> Any:
    if not isinstance(value, (list, tuple)):
        raise ValueError("turn outline wire view must be an array of entries")
    previous = -1
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("turn outline entry must be an object")
        _validate_entry(entry)
        turn = entry["turn"]
        if turn <= previous:
            raise ValueError("turn outline entries must be strictly increasing by turn")
        previous = turn
    return value


def _validate_entry(entry: dict) -> None:
    if not isinstance(entry.get("turn"), int) or isinstance(entry.get("turn"), bool) \
            or entry["turn"] < 0:
        raise ValueError("turn outline entry turn must be a non-negative integer")
    if not isinstance(entry.get("seq"), int) or isinstance(entry.get("seq"), bool) \
            or entry["seq"] < 0:
        raise ValueError("turn outline entry seq must be a non-negative integer")
    prompt = entry.get("prompt")
    if not isinstance(prompt, str) or len(prompt) > PROMPT_PREVIEW_LIMIT:
        raise ValueError("turn outline entry prompt must be a string within the prompt limit")
    response = entry.get("response")
    if not isinstance(response, str) or len(response) > RESPONSE_PREVIEW_LIMIT:
        raise ValueError("turn outline entry response must be a string within the response limit")


def _validate_state(value: Any) -> Any:
    if not isinstance(value, dict):
        raise ValueError("turn outline state must be an object")
    turns = value.get("turns")
    if not isinstance(turns, (list, tuple)):
        raise ValueError("turn outline state turns must be an array")
    _validate_entries(turns)
    draft = value.get("draft")
    if not isinstance(draft, str) or len(draft) > RESPONSE_PREVIEW_LIMIT:
        raise ValueError("turn outline state draft must be a string within the response limit")
    return value


# ---------- fold ----------


def _init(header: Any, inherited_event_count: int) -> dict:
    return {"turns": [], "draft": ""}


def _apply(state: dict, event: dict) -> dict:
    etype = event.get("type")
    data = event.get("data") or {}
    if etype == "turn/start":
        last = state["turns"][-1] if state["turns"] else None
        turn = data.get("turn")
        if last is not None and isinstance(turn, int) and turn <= last["turn"]:
            return state
        return {
            "turns": [*state["turns"], {
                "turn": turn, "seq": event.get("seq"), "prompt": "", "response": "",
            }],
            "draft": "",
        }
    if etype == "user/message":
        source = data.get("source") or {}
        if source.get("kind") != "user":
            return state
        last = state["turns"][-1] if state["turns"] else None
        if last is None or last["prompt"] != "":
            return state
        prompt = preview(data.get("content"), PROMPT_PREVIEW_LIMIT)
        if prompt == "":
            return state
        return {"turns": [*state["turns"][:-1], {**last, "prompt": prompt}],
                "draft": state["draft"]}
    if etype == "assistant/message":
        message = data.get("message") or {}
        draft = preview(message.get("content"), RESPONSE_PREVIEW_LIMIT)
        if draft == "" or draft == state["draft"]:
            return state
        return {"turns": state["turns"], "draft": draft}
    if etype == "turn/end":
        if state["draft"] == "":
            return state
        last = state["turns"][-1] if state["turns"] else None
        if last is None or last["response"] == state["draft"]:
            return {"turns": state["turns"], "draft": ""}
        return {"turns": [*state["turns"][:-1], {**last, "response": state["draft"]}],
                "draft": ""}
    return state


def turn_outline_projection() -> ProjectionDefinition:
    """`turnOutline` 单元定义（stateVersion 2，wire view = turns 数组）。"""
    return ProjectionDefinition(
        "turnOutline",
        init=_init,
        apply=_apply,
        state_version=TURN_OUTLINE_STATE_VERSION,
        state_schema=_validate_state,
        view=lambda state: state["turns"],
        view_schema=_validate_entries,
    )


def install_turn_outline(ctx) -> Callable[[], None]:
    """把 `turnOutline` 单元注册进 `ctx.sessionProjections`；返回注销 disposer。

    与上游 session-turn-outline 函数插件一致：仅依赖 `sessionProjections`
    注册表（缺省 fail loud，对齐 time-context 的装配纪律）。
    """
    registry = ctx.get("sessionProjections")
    if registry is None:
        raise RuntimeError(
            "session-turn-outline: the sessionProjections service is required "
            "(install the M7 registry)")
    return registry.register(turn_outline_projection())