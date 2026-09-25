"""surface → 模型消息投影 + 事件级 surface 契约校验。

上游对照：packages/core/session/src/surface.ts（SurfaceIntent append / replace
语义：{op:'replace', startSeq, endSeq} 以 startSeq/endSeq 两个 **surface 节点的
seq** 命名区间（V3 由 start/end 改名），替换为一个新节点；
deriveEventMessage：空内容 assistant/system 消息派生为 None，不入转录；
assertSystemHeadRewrite：node 0 的 system 头只允许被恰指该节点的
system/message replace 改写；validateSessionEventData：canonical 载荷规则）。
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any

from .seq_ranges import decode_seq_ranges
from .types import SURFACE_TYPES

__all__ = [
    "assert_developer_header",
    "assert_provenance",
    "assert_system_head_rewrite",
    "assert_tool_result_rewrite",
    "derive_event_message",
    "derive_messages",
    "is_replace_op",
    "surface_op_of",
    "validate_session_event_data",
]


def _is_event_seq(value: Any) -> bool:
    """是否为非负安全整数事件 seq（上游 surface.ts isEventSeq）。"""
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def is_replace_op(op: Any) -> bool:
    """surfaceOp 是否为精确的 {op:'replace', startSeq, endSeq}（兼容冻结后的
    MappingProxyType；startSeq/endSeq 必须是非负安全整数，键集恰为三键——上游
    surface.ts isReplaceOp，V3 起端点名 startSeq/endSeq）。"""
    if not isinstance(op, (dict, MappingProxyType)):
        return False
    keys = set(op.keys())
    if keys != {"op", "startSeq", "endSeq"}:
        return False
    return op.get("op") == "replace" and _is_event_seq(op.get("startSeq")) \
        and _is_event_seq(op.get("endSeq"))


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


def assert_system_head_rewrite(event_type: str, start_idx: int | None,
                               shadowed_seqs: list[int],
                               surface_nodes: list[dict]) -> None:
    """node 0 系统头保护（上游 surface.ts assertSystemHeadRewrite）。

    替换起点不在 surface 首位（start_idx != 0）→ 无保护；首位节点不是
    system/message → 无保护；否则替换事件必须是 system/message 且恰好遮蔽
    该单节点（后续 system 节点无保护，压缩区间可以遮蔽它们）。
    """
    if start_idx != 0:
        return
    head = surface_nodes[0] if surface_nodes else None
    if head is None:
        return
    events_head_type = head.get("type")
    if events_head_type != "system/message":
        return
    if event_type != "system/message" or len(shadowed_seqs) != 1:
        raise ValueError(
            "surface replace: node 0 holds the system prompt and may be "
            "rewritten only by a system/message over exactly that node")


def _plain(value: Any) -> Any:
    """冻结载体的浅展开（MappingProxyType → dict；其余原样）。"""
    return dict(value) if isinstance(value, MappingProxyType) else value


def validate_session_event_data(event_type: str, data: Any) -> None:
    """canonical 载荷规则（上游 surface.ts validateSessionEventData）。

    * request/header：data/header 必须是对象；**禁止 header.system**（系统
      提示词是 system/message 事件）；空 tools 数组与空 adapterDefaults
      对象必须省略。
    * surface 消息：developer/message ⇔ role 'developer'；工具增删块要求
      developer 角色；tool-addition/tool-removal 要求非空 toolName；
      tool-addition 必须省略内联 `tool`；headerSeq 恰在有新增块时出现。
    * tool/result：data.error 存在时 message.isError 必须 === true
      （矛盾即拒、绝不补写）。

    兼容冻结事件（data 可能是 MappingProxyType，先浅展开）。
    """
    data = _plain(data)
    if event_type in SURFACE_TYPES and isinstance(data, dict):
        message = data if event_type == "user/message" else _plain(data.get("message"))
        if isinstance(message, dict):
            if (event_type == "developer/message") != (message.get("role") == "developer"):
                raise ValueError(
                    "developer/message and developer role must occur together")
            content = message.get("content")
            if message.get("role") != "developer" and isinstance(content, list) \
                    and any(isinstance(_plain(b), dict)
                            and _plain(b).get("type") in ("tool-addition", "tool-removal")
                            for b in content):
                raise ValueError("tool-change blocks require developer role")
            if event_type == "developer/message" and isinstance(content, list):
                has_additions = False
                for raw in content:
                    block = _plain(raw)
                    if not isinstance(block, dict) \
                            or block.get("type") not in ("tool-addition", "tool-removal"):
                        continue
                    tool_name = block.get("toolName")
                    if not isinstance(tool_name, str) or tool_name == "":
                        raise ValueError(f"{block.get('type')} requires a nonempty toolName")
                    if block.get("type") == "tool-addition":
                        has_additions = True
                        if "tool" in block:
                            raise ValueError("tool-addition must omit inline tool definitions")
                if has_additions != _is_event_seq(data.get("headerSeq")):
                    if has_additions:
                        raise ValueError("developer/message requires headerSeq when tool additions are present")
                    raise ValueError("developer/message must omit headerSeq when there are no tool additions")
    if event_type == "request/header":
        if not isinstance(data, dict):
            raise ValueError("request/header data must be an object")
        header = _plain(data.get("header"))
        if not isinstance(header, dict):
            raise ValueError("request/header header must be an object")
        if "system" in header:
            raise ValueError("request/header must omit header.system; use system/message")
        if isinstance(header.get("tools"), list) and len(header["tools"]) == 0:
            raise ValueError("request/header must omit empty tools")
        defaults = _plain(header.get("adapterDefaults"))
        if isinstance(defaults, dict) and len(defaults) == 0:
            raise ValueError("request/header must omit empty adapterDefaults")
    elif event_type == "tool/result":
        if not isinstance(data, dict):
            raise ValueError("tool/result data must be an object")
        if data.get("error") is None:
            return
        message = _plain(data.get("message"))
        if not isinstance(message, dict) or message.get("isError") is not True:
            raise ValueError("tool/result error requires message.isError === true")


def assert_developer_header(event: dict, events: list) -> None:
    """developer/message 的 headerSeq 绑定校验（上游 surface.ts assertDeveloperHeader）。

    headerSeq 必须指向更早的 request/header；每个 tool-addition 的 toolName
    必须在该 header 的工具定义中恰好命中一个，且该定义含 string description
    与 object parameters；被引用工具的 deferLoading 若存在必须为 true。
    """
    if event["type"] != "developer/message":
        return
    data = _plain(event["data"])
    validate_session_event_data("developer/message", data)
    if not isinstance(data, dict):
        return
    header_seq = data.get("headerSeq")
    if header_seq is None:
        return
    if not _is_event_seq(header_seq) or header_seq >= event["seq"] \
            or header_seq >= len(events):
        raise ValueError("developer/message headerSeq must reference an earlier request/header")
    header_event = events[header_seq]
    if not isinstance(header_event, (dict, MappingProxyType)) \
            or header_event.get("type") != "request/header":
        raise ValueError("developer/message headerSeq must reference an earlier request/header")
    header_data = _plain(header_event.get("data"))
    header = _plain(header_data.get("header")) if isinstance(header_data, dict) else None
    tools = header.get("tools") if isinstance(header, dict) else None
    tools = tools if isinstance(tools, list) else []
    message = _plain(data.get("message"))
    content = message.get("content") if isinstance(message, dict) else None
    for raw in content or []:
        block = _plain(raw)
        if not isinstance(block, dict) or block.get("type") != "tool-addition":
            continue
        tool_name = block.get("toolName")
        definitions = [_plain(t) for t in tools
                       if isinstance(_plain(t), dict) and _plain(t).get("name") == tool_name]
        if len(definitions) != 1:
            raise ValueError(
                f'developer/message tool-addition "{tool_name}" must name exactly one '
                f"tool in headerSeq {header_seq}")
        definition = definitions[0]
        if not isinstance(definition.get("description"), str) \
                or not isinstance(_plain(definition.get("parameters")), dict):
            raise ValueError(
                f'developer/message tool-addition "{tool_name}" requires a complete '
                f"tool definition in headerSeq {header_seq}")
        if "deferLoading" in definition and definition.get("deferLoading") is not True:
            raise ValueError("developer/message referenced tool deferLoading must be true when present")


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

    恰好遮蔽 1 个节点、被遮蔽节点必须是 tool/result、除 message.content 外其余
    字段深相等（只允许改结果 content——崩溃恢复合成/改写结果内容的唯一通道）。
    V4：content 是平铺的块数组，整体置 null 后比较。
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
        original_message["content"] = None
        replacement_message["content"] = None
        original_data["message"] = original_message
        replacement_data["message"] = replacement_message
        if not _json_equal(original_data, replacement_data):
            raise ValueError("tool/result surface replacement may change only content")


def derive_event_message(ev: dict) -> dict | None:
    """单事件 → 模型消息：surface 节点投影规则（上游 surface.ts deriveEventMessage）。

    空内容 assistant/message（如 max-tokens 只含 usage 的 step）、空内容
    system/message（「无系统提示词」节点）与空内容 developer/message（工具
    增删的占位节点）派生为 None，不入转录；非 surface 事件派生为 None。
    """
    data = ev["data"]
    if ev["type"] == "user/message":
        return data
    if ev["type"] in ("system/message", "developer/message", "assistant/message"):
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
    startSeq/endSeq 两个 seq 在当前 surface 上定位区间并整体替换。seq 不在
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
            start, end = op["startSeq"], op["endSeq"]
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


def derive_messages(events, projections=None) -> list[dict]:
    """纯投影：沿 surface 节点顺序派生模型消息（不修改日志，可重复调用）。

    replace 节点遮蔽被替换区间（上游 surface.ts：{op:'replace', startSeq,
    endSeq} 以当前 surface 上的 seq 定位区间并整体替换）。

    projections 非 None 时先应用 message 投影覆盖表（上游 foldSurface +
    SessionMessageProjection）：`image/offload` 等 durable 事实把对应节点
    投影为不可变副本（身份保留），未覆盖的节点原样派生。
    """
    nodes = _surface_nodes(events)
    try:
        event_list = [ev for ev in events]
    except TypeError:  # pragma: no cover - 迭代器兜底
        event_list = list(events)
        nodes = _surface_nodes(event_list)
    overrides = {}
    if projections is not None:
        from .projections import fold_projections

        overrides = fold_projections(event_list, nodes, projections)
    messages = []
    for node in nodes:
        if node["seq"] in overrides:
            messages.append(overrides[node["seq"]])
            continue
        msg = derive_event_message(node)
        if msg is not None:
            messages.append(msg)
    return messages