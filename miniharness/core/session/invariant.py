"""事件不变量：编号从 1 起、坏事件进不来。

上游对照：packages/core/session/src/invariant.ts（nextTurn: 1, nextStep: 1，
每 turn 内 step 重置为 1；SessionEvent 的 seq == log.length 等不变量）。
"""
from __future__ import annotations

from .json import is_json_safe
from .surface import assert_provenance, surface_op_of
from .types import KNOWN_TYPES

__all__ = ["NEXT_STEP", "NEXT_TURN", "validate_event"]

# 上游 invariant.ts：turn/step 编号从 1 起（每 turn 内 step 重置为 1）
NEXT_TURN = 1
NEXT_STEP = 1


def validate_event(type_, data, surfaceOp, sourceEventSeqs) -> dict:
    """源头校验：未知类型 / 非法 surfaceOp / 不可无损序列化 → 直接抛错。

    这是日志边界的入口不变量：坏事件永远进不了日志（对应上游 invariant.ts
    的 SessionEvent 不可变约束）。surface 契约校验对齐上游 surface.ts
    surfaceOpOf + assertProvenance（非 surface 类型不能带 surfaceOp /
    sourceEventSeqs；surface 类型必须带合法 surfaceOp；sourceEventSeqs 血统
    约束由调用方在具备 seq 与 surface 上下文时执行——此处只查不依赖上下文的
    形状部分，血统全量校验在 Session.append / _replay_seed 落地）。
    """
    if type_ not in KNOWN_TYPES:
        raise ValueError(f"未知事件类型: {type_!r}")
    op = surface_op_of(type_, surfaceOp)
    if sourceEventSeqs is not None:
        # 非 surface 事件禁止携带 sourceEventSeqs（surface_op_of 已拦 surfaceOp，
        # 这里对称拦 sourceEventSeqs——上游 surfaceOpOf 一并处理两字段）
        if op is None:
            raise ValueError(f"非 surface 事件 {type_} 不允许携带 sourceEventSeqs")
        if not isinstance(sourceEventSeqs, (list, tuple)) or not all(
            isinstance(s, int) and not isinstance(s, bool) and s >= 0
            for s in sourceEventSeqs
        ):
            raise ValueError(f"sourceEventSeqs 必须是非负整数序列: {sourceEventSeqs!r}")
        if len(sourceEventSeqs) == 0 and type_ != "assistant/message":
            raise ValueError("sourceEventSeqs must not be empty except on assistant/message")
        if len(set(sourceEventSeqs)) != len(sourceEventSeqs):
            raise ValueError("sourceEventSeqs must not contain duplicates")
    payload: dict = {"type": type_, "data": data if data is not None else {}}
    if op is not None:
        payload["surfaceOp"] = surfaceOp
        if sourceEventSeqs is not None:
            payload["sourceEventSeqs"] = list(sourceEventSeqs)
    elif surfaceOp is not None:
        raise ValueError(f"非 surface 事件 {type_} 不允许携带 surfaceOp")
    if not is_json_safe(payload):
        raise TypeError(f"事件必须可无损 JSON 序列化: {payload!r}")
    return payload