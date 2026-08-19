"""无损 JSON 与深度冻结：日志边界的序列化不变量。

上游对照：packages/core/session/src/json.ts（无损序列化 + 冻结语义）。
"""
from __future__ import annotations

import json
import time
from types import MappingProxyType
from typing import Any

__all__ = ["deep_freeze", "is_json_safe", "now_ms", "thaw"]


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


def now_ms() -> int:
    """Unix epoch 毫秒（与上游事件 time 字段一致）。"""
    return int(time.time() * 1000)


def thaw(value: Any) -> Any:
    """解冻：MappingProxyType → dict，tuple/list → list。持久化前还原为普通 JSON 结构。

    必须覆盖 list 分支：冻结结构里 list 变 tuple，但实时代码会在冻结值外层套
    新建 list（如 web 流把冻结 splice 的 inserted 重投影进新 items 数组），
    不解 list 就漏掉内层冻结项。
    """
    if isinstance(value, MappingProxyType):
        value = dict(value)
    if isinstance(value, dict):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [thaw(v) for v in value]
    return value