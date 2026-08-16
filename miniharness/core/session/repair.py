"""括号平衡与崩溃恢复。

上游对照：packages/core/session/src/repair.ts（interruptedTurnClosers：先为未匹配
tool call 合成 error 结果，再补 step/end，最后补 turn/end {kind:'interrupted'}；
seq 延续日志，时间戳复用最后真实事件）。
"""
from __future__ import annotations

from .types import TOOL_NOT_STARTED, TOOL_OUTCOME_UNKNOWN

__all__ = ["repair_interrupted_turn", "turn_balance"]


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


def repair_interrupted_turn(events: list) -> list[dict]:
    """崩溃恢复：为未闭合的尾部 turn 合成确定性闭包事件（上游 interruptedTurnClosers）。

    顺序与上游一致：先为未匹配的 tool call 补 error 结果（TOOL_NOT_STARTED /
    TOOL_OUTCOME_UNKNOWN），再补 step/end，最后补 turn/end {kind:'interrupted'}；
    seq 延续日志，时间戳复用最后真实事件。已平衡的日志返回空列表。
    """
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

    # 先关调用再关 step：provider 拒绝悬挂的 assistant tool call
    for call_id, info in pending_calls.items():
        started = info["callSeq"] is not None
        message = {
            "id": f"interrupted-tool-result-{call_id}-{seq}",
            "role": "user",
            "source": {"kind": "tool", "callId": call_id},
            "content": [{
                "type": "tool-result",
                "toolCallId": call_id,
                "isError": True,
                "content": [{"type": "text", "text": (
                    "The tool call was interrupted after it was recorded, but no result was durably "
                    "recorded. Its outcome is unknown. Decide whether to retry from the tool semantics: "
                    "retry only if the operation is read-only or idempotent; if it may have side effects, "
                    "first verify external state or ask the user. Do not retry blindly."
                    if started else
                    "The tool call was interrupted before the Harness recorded it as started. "
                    "Retry it if it is still needed."
                )}],
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
                    "data": {"turn": open_turn, "reason": {"kind": "interrupted"}}})
    return closers