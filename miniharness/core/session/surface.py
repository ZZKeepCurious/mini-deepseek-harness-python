"""surface → 模型消息投影。

上游对照：packages/core/session/src/surface.ts（SurfaceIntent append / replace
语义：{op:'replace', start, end} 以 start/end 两个 **surface 节点的 seq** 命名区间，
替换为一个新节点；deriveEventMessage：空内容 assistant 消息派生为 None，不入转录）。
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any

from .types import SURFACE_TYPES

__all__ = ["derive_event_message", "derive_messages"]


def _is_replace_op(op: Any) -> bool:
    """surfaceOp 是否为 {op:'replace', start, end}（兼容冻结后的 MappingProxyType）。"""
    return isinstance(op, (dict, MappingProxyType)) and op.get("op") == "replace"


def derive_event_message(ev: dict) -> dict | None:
    """单事件 → 模型消息：surface 节点投影规则（上游 surface.ts deriveEventMessage）。

    空内容 assistant/message（如 max-tokens 只含 usage 的 step）派生为 None，
    不入转录；非 surface 事件派生为 None。
    """
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


def _surface_nodes(events) -> list[dict]:
    """沿事件日志折叠当前 surface 节点（含 seq，模型可见顺序）。

    对齐上游 surface.ts 的 foldSurface：append 追加尾部；replace 按
    start/end 两个 seq 在当前 surface 上定位区间并整体替换。seq 不在
    当前 surface 上（区间非法/日志损坏）→ fail loud（上游同语义）。
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
            start_idx = next((i for i, n in enumerate(surface) if n["seq"] == start), None)
            end_idx = next((i for i, n in enumerate(surface) if n["seq"] == end), None)
            if start_idx is None or end_idx is None:
                raise ValueError(
                    f"surface replace at seq {ev['seq']}: 区间 {start}-{end} 不在当前 surface 上"
                )
            if start_idx > end_idx:
                raise ValueError(
                    f"surface replace at seq {ev['seq']}: 区间 {start}-{end} 顺序颠倒"
                )
            surface = surface[:start_idx] + [ev] + surface[end_idx + 1:]
    return surface


def derive_messages(events) -> list[dict]:
    """纯投影：沿 surface 节点顺序派生模型消息（不修改日志，可重复调用）。

    replace 节点遮蔽被替换区间（上游 surface.ts：{op:'replace', start, end}
    以当前 surface 上的 seq 定位区间并整体替换）。
    """
    messages = []
    for node in _surface_nodes(events):
        msg = derive_event_message(node)
        if msg is not None:
            messages.append(msg)
    return messages