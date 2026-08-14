"""第 1 章：事件溯源会话（event-sourced session）。

对应 dsh 真实源码：packages/core/session —— Session 是 SessionEvent 的
追加式事件日志，模型历史由 deriveMessages() 投影派生，绝不另存副本。

本章三个不变量：
  1. seq == len(events) - 1（追加式、永不修改历史）
  2. 坏事件进不来（未知类型 / 非 JSON 序列化 → 直接抛错）
  3. 模型可见 ⟺ 已记录（投影纯函数，无第二份副本）
"""
from __future__ import annotations

import json
from types import MappingProxyType
from typing import Any

KNOWN_TYPES = frozenset({
    "turn/start", "turn/end", "step/start", "step/end",
    "user/message", "assistant/message", "assistant/chunk",
    "tool/call", "tool/result",
})

SURFACE_TYPES = frozenset({"user/message", "assistant/message", "tool/result"})


def is_json_safe(value: Any) -> bool:
    """无损 JSON 强制：无法序列化的值（含非有限浮点数）直接判非法。"""
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return True
    except (TypeError, ValueError):
        return False


def deep_freeze(value: Any) -> Any:
    """深度冻结：dict → 只读代理，list → tuple。冻结后任何修改都抛 TypeError。"""
    if isinstance(value, dict):
        return MappingProxyType({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(deep_freeze(v) for v in value)
    return value


class Session:
    """追加式事件日志：唯一事实来源。"""

    def __init__(self, session_id: str):
        self.session_id = session_id
        self._events: list[dict[str, Any]] = []

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        """只读视图：外部永远拿不到可变的内部列表。"""
        return tuple(self._events)

    def append(self, event: dict[str, Any]) -> dict[str, Any]:
        """源头校验 + 冻结：坏事件永远进不了日志。"""
        if not isinstance(event, dict) or "type" not in event:
            raise ValueError(f"事件必须是含 type 的 dict: {event!r}")
        etype = event["type"]
        if etype not in KNOWN_TYPES:
            raise ValueError(f"未知事件类型: {etype!r}")
        if etype in SURFACE_TYPES:
            op = event.get("surfaceOp")
            if op not in ("append", "replace"):
                raise ValueError(f"surface 事件 {etype} 必须带 surfaceOp=append|replace，得到 {op!r}")
        if not is_json_safe(event):
            raise TypeError(f"事件必须可无损 JSON 序列化: {event!r}")
        record = dict(event, seq=len(self._events))
        self._events.append(deep_freeze(record))
        return record


def derive_messages(events) -> list[dict[str, str]]:
    """纯投影：按 seq 顺序派生模型历史（不修改日志，可重复调用）。"""
    messages: list[dict[str, str]] = []
    for ev in events:
        etype = ev["type"]
        if etype == "user/message":
            _apply_surface(messages, "user", ev.get("content", ""), ev.get("surfaceOp", "append"))
        elif etype == "assistant/message":
            _apply_surface(messages, "assistant", ev.get("content", ""), ev.get("surfaceOp", "append"))
        elif etype == "tool/result":
            if ev.get("isError"):
                content = f"[工具 {ev.get('name')} 失败] {ev.get('error')}"
            else:
                content = f"[工具 {ev.get('name')} 结果] {ev.get('content')}"
            _apply_surface(messages, "tool", content, ev.get("surfaceOp", "append"))
        # turn/* step/* assistant/chunk tool/call 不参与投影
    return messages


def _apply_surface(messages, role: str, content: str, op: str) -> None:
    if op == "append":
        messages.append({"role": role, "content": content})
    elif op == "replace":
        # 压缩替换：整体替换最近一条同 role 的消息；没有则退化为 append
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == role:
                messages[i] = {"role": role, "content": content}
                return
        messages.append({"role": role, "content": content})
    else:
        raise ValueError(f"非法 surfaceOp: {op!r}")


def turn_balance(events) -> int:
    """括号平衡不变量：返回未闭合 turn 数（>=0）。为负说明日志被破坏。"""
    balance = 0
    for ev in events:
        if ev["type"] == "turn/start":
            balance += 1
        elif ev["type"] == "turn/end":
            balance -= 1
            if balance < 0:
                raise ValueError("turn/end 出现在没有对应 turn/start 的位置，日志不平衡")
    return balance


def repair_interrupted_turn(events) -> list[dict]:
    """崩溃恢复：为未闭合的 turn 合成 turn/end { reason: "interrupted" }。"""
    repaired = [dict(ev) for ev in events]
    for _ in range(turn_balance(repaired)):
        repaired.append({"type": "turn/end", "reason": "interrupted"})
    return repaired