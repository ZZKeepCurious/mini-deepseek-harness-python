"""括号平衡与崩溃恢复。

上游对照：packages/core/session/src/repair.ts（openTurnClosers：先为未匹配
tool call 合成 error 结果，再补 step/end，最后补 turn/end {kind}；seq 延续
日志，时间戳复用最后真实事件）+ fork.ts（buildForkSeed：拷贝含端点前缀 +
补 `session/end-seed{inherited:true}` marker + 以 `forked` cause 关闭开放尾部）。

V4：合成结果消息 role `'tool'` + 顶层 `toolCallId`/`isError`、content 平铺
（V4 前是 user 角色包裹一个 `tool-result` 块）；cause 选 turn/end reason、
模型可见文案与合成 id 前缀（`interrupted-`/`forked-`）。
"""
from __future__ import annotations

from .types import TOOL_NOT_STARTED, TOOL_OUTCOME_UNKNOWN

__all__ = ["build_fork_seed", "open_turn_closers", "repair_interrupted_turn", "turn_balance"]

# 合成 error 结果的模型可见文案（上游 repair.ts CLOSER_TEXT，逐字）。
_CLOSER_TEXT = {
    "interrupted": {
        "started": (
            "The tool call was interrupted after it was recorded, but no result was durably "
            "recorded. Its outcome is unknown. Decide whether to retry from the tool semantics: "
            "retry only if the operation is read-only or idempotent; if it may have side effects, "
            "first verify external state or ask the user. Do not retry blindly."
        ),
        "notStarted": (
            "The tool call was interrupted before the Harness recorded it as started. "
            "Retry it if it is still needed."
        ),
    },
    "forked": {
        "started": (
            "The history inherited by this branch records this tool call starting but does not "
            "include its result. The parent session may have completed it after the fork point. "
            "Decide whether to retry from the tool semantics: retry only if the operation is "
            "read-only or idempotent; if it may have side effects, first verify external state or "
            "ask the user. Do not retry blindly."
        ),
        "notStarted": (
            "The history inherited by this branch has no record of this tool call starting. "
            "The parent session may have executed it after the fork point. Decide whether to retry "
            "from the tool semantics: retry only if the operation is read-only or idempotent; if it "
            "may have side effects, first verify external state or ask the user. Do not retry blindly."
        ),
    },
}


def turn_balance(events) -> int:
    """括号平衡硬性规定：返回未闭合 turn 数（>=0）。为负说明日志被破坏。"""
    balance = 0
    for ev in events:
        if ev["type"] == "turn/start":
            balance += 1
        elif ev["type"] == "turn/end":
            balance -= 1
            if balance < 0:
                raise ValueError("turn/end 出现在没有对应 turn/start 的位置，日志不平衡")
    return balance


def open_turn_closers(events: list, cause_kind: str) -> list[dict]:
    """合成关闭开放尾部 turn 的确定性事件（上游 openTurnClosers）。

    cause_kind 为 `'interrupted'`（崩溃恢复）或 `'forked'`（fork 前缀在源开放
    turn 内切割）；选 turn/end reason、模型可见文案与合成 id 前缀。顺序：
    先为未匹配 tool call 补 error 结果（TOOL_NOT_STARTED / TOOL_OUTCOME_UNKNOWN），
    再补 step/end，最后补 turn/end；seq 延续日志，时间戳复用最后真实事件。
    已平衡的日志返回空列表。
    """
    if cause_kind not in _CLOSER_TEXT:
        raise ValueError(f"unknown open-turn close cause: {cause_kind!r}")
    open_turn: int | None = None
    open_step: int | None = None
    pending_calls: dict[str, dict] = {}
    for ev in events:
        t = ev["type"]
        data = ev["data"]
        if t == "turn/start":
            open_turn, open_step, pending_calls = data["turn"], None, {}
        elif t == "turn/end":
            open_turn = open_step = None
            pending_calls = {}
        elif t == "step/start":
            open_step = data["step"]
        elif t == "step/end":
            open_step = None
            pending_calls = {}
        elif t == "assistant/message":
            for block in data["message"]["content"]:
                if block["type"] == "tool-call":
                    pending_calls[block["id"]] = {"step": data["step"], "callSeq": None}
        elif t == "tool/call":
            entry = pending_calls.get(data["callId"])
            if entry is not None:
                entry["callSeq"] = ev["seq"]
        elif t == "tool/result":
            pending_calls.pop(data["message"]["source"]["callId"], None)

    if open_turn is None or not events:
        return []

    seq = events[-1]["seq"] + 1
    time_ = events[-1]["time"]
    closers: list[dict] = []
    text = _CLOSER_TEXT[cause_kind]

    # 先关调用再关 step：provider 拒绝悬挂的 assistant tool call
    for call_id, info in pending_calls.items():
        started = info["callSeq"] is not None
        message = {
            "id": f"{cause_kind}-tool-result-{call_id}-{seq}",
            "role": "tool",
            "toolCallId": call_id,
            "isError": True,
            "source": {"kind": "tool", "callId": call_id},
            "content": [{
                "type": "text",
                "text": text["started"] if started else text["notStarted"],
            }],
        }
        error = {
            "name": "ToolOutcomeUnknownError" if started else "ToolNotStartedError",
            "code": TOOL_OUTCOME_UNKNOWN if started else TOOL_NOT_STARTED,
        }
        closers.append({
            "type": "tool/result",
            "seq": seq, "time": time_,
            "data": {"turn": open_turn, "step": info["step"], "message": message, "error": error},
            "surfaceOp": "append",
            **({"sourceEventSeqs": [info["callSeq"]]} if started else {}),
        })
        seq += 1

    if open_step is not None:
        closers.append({"type": "step/end", "seq": seq, "time": time_,
                        "data": {"turn": open_turn, "step": open_step}})
        seq += 1

    closers.append({"type": "turn/end", "seq": seq, "time": time_,
                    "data": {"turn": open_turn, "reason": {"kind": cause_kind}}})
    return closers


def repair_interrupted_turn(events: list) -> list[dict]:
    """崩溃恢复入口：合成关闭被中断尾部 turn 的事件（上游 interruptedTurnClosers）。"""
    return open_turn_closers(events, "interrupted")


def build_fork_seed(events: list, boundary: int) -> list[dict]:
    """构造 fork seed：含端点前缀 + 继承 marker + `forked` 尾部闭包（上游 buildForkSeed）。

    boundary 是子会话继承到的含端点源事件 seq（调用方校验其存在且连续）。
    返回新列表：保留源事件对象，随后是 `session/end-seed{inherited:true}` marker
    （不计入 inheritedEventCount），再是以 `forked` cause 关闭开放尾部的合成闭包。
    """
    prefix = list(events[: boundary + 1])
    marker = {
        "type": "session/end-seed",
        "seq": boundary + 1,
        "time": events[boundary]["time"],
        "data": {"inherited": True},
    }
    prefix.append(marker)
    return prefix + open_turn_closers(prefix, "forked")
