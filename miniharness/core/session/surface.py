"""surface → 模型消息投影。

上游对照：packages/core/session/src/surface.ts（SurfaceIntent append / replace
语义：{op:'replace', start, end} 替换 start..end 两个 surface 节点为一个新节点；
deriveEventMessage：空内容 assistant 消息派生为 None，不入转录）。
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any

from .types import SURFACE_TYPES

__all__ = ["derive_messages"]


def _is_replace_op(op: Any) -> bool:
    """surfaceOp 是否为 {op:'replace', start, end}（兼容冻结后的 MappingProxyType）。"""
    return isinstance(op, (dict, MappingProxyType)) and op.get("op") == "replace"


def _project_message(ev: dict) -> dict | None:
    """surface 节点 → 模型消息。空内容 assistant/message（如 max-tokens 只含
    usage 的 step）派生为 None，不入转录（上游 surface.ts deriveEventMessage）。"""
    data = ev["data"]
    if ev["type"] == "user/message":
        return data
    if ev["type"] == "assistant/message":
        message = data.get("message")
        if message and not message.get("content"):
            return None
        return message
    if ev["type"] == "tool/result":
        return data.get("message")
    return None


def derive_messages(events) -> list[dict]:
    """纯投影：沿 surface 节点顺序派生模型消息（不修改日志，可重复调用）。

    replace 节点遮蔽被替换区间（上游 surface.ts：{op:'replace', start, end}
    替换 start..end 两个 surface 节点为一个新节点）。
    """
    surface: list[dict] = []
    for ev in events:
        if ev["type"] not in SURFACE_TYPES:
            continue
        op = ev.get("surfaceOp")
        if op == "append":
            surface.append(ev)
        elif _is_replace_op(op):
            start, end = op["start"], op["end"]
            surface = surface[:start] + [ev] + surface[end + 1:]
    messages = []
    for node in surface:
        msg = _project_message(node)
        if msg is not None:
            messages.append(msg)
    return messages