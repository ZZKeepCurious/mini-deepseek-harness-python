"""括号平衡与崩溃恢复。

上游对照：packages/core/session/src/repair.ts（openTurnClosers：先为未匹配
tool call 合成 error 结果，再补 step/end，最后补 turn/end {kind}；seq 延续
日志，时间戳复用最后真实事件）+ fork.ts（buildForkSeed：拷贝含端点前缀 +
补 `session/end-seed{inherited:true}` marker + 以 `forked` cause 关闭开放尾部）。

V4：合成结果消息 role `'tool'` + 顶层 `toolCallId`/`isError`、content 平铺
（V4 前是 user 角色包裹一个 `tool-result` 块）；cause 选 turn/end reason、
模型可见文案与合成 id 前缀（`interrupted-`/`forked-`）。

rc.2：抽出 `ToolCallRecovery`（上游 repair.ts:105-198）——供「失败的 live step」
与崩溃/fork 共用。pending call 钉住 turn+step，只有 `surfaceOp=='append'` 且
turn/step 匹配的 tool/result 才确认；`results()` 幂等（不改状态）。
"""
from __future__ import annotations

from .types import TOOL_NOT_STARTED, TOOL_OUTCOME_UNKNOWN

__all__ = [
    "ToolCallRecovery",
    "build_fork_seed",
    "open_turn_closers",
    "repair_interrupted_turn",
    "turn_balance",
]

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


class ToolCallRecovery:
    """追踪一个会话已提交事件里未回应的 assistant tool 请求（上游 repair.ts:105-198）。

    从自有 step 的起点（或恢复前缀）观察，在其 step 关闭前恢复。状态只保留
    pending 身份，不保留事件历史。`results()` 幂等：调用方提交结果后再观察
    那些提交，才能推进状态。
    """

    def __init__(self, cause_kind: str = "interrupted") -> None:
        if cause_kind not in _CLOSER_TEXT:
            raise ValueError(f"unknown open-turn close cause: {cause_kind!r}")
        self._cause_kind = cause_kind
        self._pending: dict[str, dict] = {}
        self._last: tuple[int, int] | None = None

    def observe(self, event: dict) -> None:
        """消费下一条已提交事件；已关闭的 step 与 turn 边界丢弃 pending 请求。"""
        self._last = (event["seq"], event["time"])
        t = event["type"]
        data = event.get("data") or {}
        if t in ("turn/start", "turn/end", "step/end"):
            self._pending.clear()
        elif t == "assistant/message":
            for block in (data.get("message") or {}).get("content") or []:
                if block.get("type") == "tool-call":
                    self._pending[block["id"]] = {
                        "turn": data.get("turn"), "step": data.get("step"), "callSeq": None,
                    }
        elif t == "tool/call":
            entry = self._pending.get(data.get("callId"))
            if entry is not None:
                entry["callSeq"] = event["seq"]
        elif t == "tool/result":
            call_id = ((data.get("message") or {}).get("source") or {}).get("callId")
            entry = self._pending.get(call_id)
            # 只有 append 且 turn/step 匹配的结果才确认；替换结果或其它 turn/step
            # 复用同 id 不算确认（上游 repair.ts:139-140）。
            if (event.get("surfaceOp") == "append" and entry is not None
                    and entry["turn"] == data.get("turn")
                    and entry["step"] == data.get("step")):
                del self._pending[call_id]

    def results(self) -> list[dict]:
        """按 assistant 顺序构造保守 error 结果，不改变追踪状态（上游 results）。

        seq 沿用最新已观察事件之后；时间戳复用它。调用方提交结果并观察提交后
        才应再次恢复。
        """
        if self._last is None:
            return []
        seq = self._last[0] + 1
        time_ = self._last[1]
        text = _CLOSER_TEXT[self._cause_kind]
        results: list[dict] = []
        for call_id, info in self._pending.items():
            started = info["callSeq"] is not None
            message = {
                "id": f"{self._cause_kind}-tool-result-{call_id}-{seq}",
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
            event = {
                "type": "tool/result",
                "seq": seq, "time": time_,
                "data": {"turn": info["turn"], "step": info["step"],
                         "message": message, "error": error},
                "surfaceOp": "append",
            }
            if started:
                event["sourceEventSeqs"] = [info["callSeq"]]
            results.append(event)
            seq += 1
        return results


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
    recovery = ToolCallRecovery(cause_kind)
    for ev in events:
        recovery.observe(ev)
        t = ev["type"]
        if t == "turn/start":
            open_turn, open_step = ev["data"]["turn"], None
        elif t == "turn/end":
            open_turn = open_step = None
        elif t == "step/start":
            open_step = ev["data"]["step"]
        elif t == "step/end":
            open_step = None

    if open_turn is None or not events:
        return []

    last = events[-1]
    closers: list[dict] = recovery.results()
    seq = last["seq"] + len(closers) + 1
    time_ = last["time"]

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
