"""事件不变量：编号从 1 起、坏事件进不来。

上游对照：packages/core/session/src/invariant.ts（nextTurn: 1, nextStep: 1，
每 turn 内 step 重置为 1；SessionEvent 的 seq == log.length 等不变量）。
"""
from __future__ import annotations

from .json import is_json_safe
from .types import KNOWN_TYPES, SURFACE_TYPES

__all__ = ["NEXT_STEP", "NEXT_TURN", "validate_event"]

# 上游 invariant.ts：turn/step 编号从 1 起（每 turn 内 step 重置为 1）
NEXT_TURN = 1
NEXT_STEP = 1


def validate_event(type_, data, surfaceOp, sourceEventSeqs) -> dict:
    """源头校验：未知类型 / 非法 surfaceOp / 不可无损序列化 → 直接抛错。

    这是日志边界的入口不变量：坏事件永远进不了日志（对应上游 invariant.ts
    的 SessionEvent 不可变约束）。
    """
    if type_ not in KNOWN_TYPES:
        raise ValueError(f"未知事件类型: {type_!r}")
    payload: dict = {"type": type_, "data": data if data is not None else {}}
    if type_ in SURFACE_TYPES:
        if surfaceOp is None:
            raise ValueError(f"surface 事件 {type_} 必须带 surfaceOp")
        if surfaceOp != "append":
            if not (isinstance(surfaceOp, dict) and surfaceOp.get("op") == "replace"):
                raise ValueError(f"非法 surfaceOp: {surfaceOp!r}")
        payload["surfaceOp"] = surfaceOp
        if sourceEventSeqs is not None:
            payload["sourceEventSeqs"] = list(sourceEventSeqs)
    elif surfaceOp is not None:
        raise ValueError(f"非 surface 事件 {type_} 不允许携带 surfaceOp")
    if not is_json_safe(payload):
        raise TypeError(f"事件必须可无损 JSON 序列化: {payload!r}")
    return payload