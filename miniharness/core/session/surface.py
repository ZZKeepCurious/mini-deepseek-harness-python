"""surface → 模型消息投影 + 事件级 surface 契约校验。

上游对照：packages/core/session/src/surface.ts（SurfaceIntent append / replace
语义：{op:'replace', start, end} 以 start/end 两个 **surface 节点的 seq** 命名区间，
替换为一个新节点；deriveEventMessage：空内容 assistant 消息派生为 None，不入转录）。
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any

from .seq_ranges import decode_seq_ranges
from .types import SURFACE_TYPES

__all__ = [
    "assert_provenance",
    "assert_tool_result_rewrite",
    "derive_event_message",
    "derive_messages",
    "is_replace_op",
    "surface_op_of",
]


def _is_event_seq(value: Any) -> bool:
    """是否为非负安全整数事件 seq（上游 surface.ts isEventSeq）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_replace_op(op: Any) -> bool:
    """surfaceOp 是否为精确的 {op:'replace', start, end}（兼容冻结后的
    MappingProxyType；start/end 必须是非负安全整数，键集恰为三键——上游
    surface.ts isReplaceOp）。"""
    if not isinstance(op, (dict, MappingProxyType)):
        return False
    keys = set(op.keys())
    if keys != {"op", "start", "end"}:
        return False
    return op.get("op") == "replace" and _is_event_seq(op.get("start")) \
        and _is_event_seq(op.get("end"))


def surface_op_of(type_: str, surfaceOp: Any) -> Any:
    """校验事件本地的 surface 契约并返回操作，非法抛错（上游 surfaceOpOf）。

    * 非 surface 类型不能带 surfaceOp / sourceEventSeqs
    * surface 类型必须带 surfaceOp（'append' 或精确 replace 形状）
    """
    if type_ not in SURFACE_TYPES:
        if surfaceOp is not None:
            raise ValueError(f'session event "{type_}" is not surface-eligible and cannot carry surfaceOp')
        return None
    if surfaceOp is None:
        raise ValueError(f'session event "{type_}" is surface-eligible and requires a surfaceOp marker')
    if surfaceOp == "append":
        return surfaceOp
    if surfaceOp is None or isinstance(surfaceOp, (str, bytes)) or not isinstance(surfaceOp, (dict, MappingProxyType)) \
            or not is_replace_op(surfaceOp):
        raise ValueError(f'session event "{type_}" carries an invalid surfaceOp')
    return surfaceOp


def assert_provenance(type_: str, source_event_seqs: Any, seq: int,
                      shadowed_seqs: list[int]) -> None:
    """校验 sourceEventSeqs 血统（上游 surface.ts assertProvenance）。

    数组非空、元素非负安全整数、无重复、全部早于事件 seq；replace 操作必须
    覆盖全部被遮蔽 surface 节点。V2：`assistant/message` 内嵌其源流，**禁止**
    携带 sourceEventSeqs（上游 assertProvenance 首段）。

    输入可能是存储态区间编码（上游 seq-ranges.ts），先 decode 展开为内存态
    列表（对齐上游 persistence 读路径 expandProvenanceFromStorage 后再
    assert 的语义）；decode 完成形状校验（非负 / [start,end] / end>=start）。
    """
    if type_ == "assistant/message" and source_event_seqs is not None:
        raise ValueError(
            "assistant/message embeds its source stream and cannot carry sourceEventSeqs")
    if source_event_seqs is None:
        if shadowed_seqs:
            raise ValueError(
                f"surface replace: sourceEventSeqs must include every shadowed surface node; "
                f"missing {', '.join(str(s) for s in shadowed_seqs)}"
            )
        return
    sources = decode_seq_ranges(source_event_seqs)
    if len(sources) == 0:
        raise ValueError("sourceEventSeqs must not be empty")
    seen: set[int] = set()
    non_earlier = None
    for source in sources:
        if source in seen:
            raise ValueError("sourceEventSeqs must not contain duplicates")
        seen.add(source)
        if non_earlier is None and source >= seq:
            non_earlier = source
    if non_earlier is not None:
        raise ValueError(
            f"sourceEventSeqs must reference earlier events: {non_earlier} >= current seq {seq}"
        )
    missing = [s for s in shadowed_seqs if s not in seen]
    if missing:
        raise ValueError(
            f"surface replace: sourceEventSeqs must include every shadowed surface node; "
            f"missing {', '.join(str(s) for s in missing)}"
        )


def _json_equal(a: Any, b: Any) -> bool:
    """JSON 值域深比较（null/bool/num/str、数组、普通对象；兼容冻结结构）。"""
    if isinstance(a, MappingProxyType):
        a = dict(a)
    if isinstance(b, MappingProxyType):
        b = dict(b)
    if type(a) is not type(b) and not (
        (isinstance(a, (dict, MappingProxyType)) and isinstance(b, (dict, MappingProxyType)))
        or (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)))
    ):
        return False
    if isinstance(a, (dict, MappingProxyType)) and isinstance(b, (dict, MappingProxyType)):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_json_equal(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(_json_equal(x, y) for x, y in zip(a, b))
    return a == b


def assert_tool_result_rewrite(event: dict, shadowed_seqs: list[int],
                               events: list[dict]) -> None:
    """tool/result surface replace 重写规则（上游 assertToolResultRewrite）：

    恰好遮蔽 1 个节点、被遮蔽节点必须是 tool/result、除 content 外其余字段深相等
    （只允许改结果 content——崩溃恢复合成/改写结果内容的唯一通道）。
    """
    if event["type"] != "tool/result":
        return
    if len(shadowed_seqs) != 1:
        raise ValueError("tool/result surface replacement must rewrite exactly one current node")
    for original_seq in shadowed_seqs:
        original = events[original_seq] if 0 <= original_seq < len(events) else None
        if original is None or original["type"] != "tool/result":
            raise ValueError("tool/result surface replacement must target a current tool/result")
        original_data = dict(original["data"])
        replacement_data = dict(event["data"])
        original_message = dict(original_data.get("message") or {})
        replacement_message = dict(replacement_data.get("message") or {})
        original_content = (original_message.get("content") or [])
        replacement_content = (replacement_message.get("content") or [])
        if not original_content or not replacement_content:
            raise ValueError("tool/result surface replacement must target content-bearing tool/result")
        original_rest = dict(original_message)
        replacement_rest = dict(replacement_message)
        original_rest["content"] = [dict(original_content[0], content=None)]
        replacement_rest["content"] = [dict(replacement_content[0], content=None)]
        original_data["message"] = original_rest
        replacement_data["message"] = replacement_rest
        if not _json_equal(original_data, replacement_data):
            raise ValueError("tool/result surface replacement may change only content")


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
        elif is_replace_op(op):
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