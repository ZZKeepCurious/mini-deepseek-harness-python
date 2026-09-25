"""released v4 校验（上游 session-format-v3-to-v4/src/{validation,developer,system-message,
retired-syntax,fork-result}.ts 的 mini 同步载体）。

V4 三层：
  * 信封（assert_v4_event）：五种 surface 类型必带 surfaceOp；replace 恰
    op/startSeq/endSeq 三键且端点早于自身；assistant/message 禁 sourceEventSeqs；
    retired tool-result 包裹块在所有解释过的 content 槽拒绝；request/header 禁
    header.system；system/developer/tool-result 消息形状本地校验。
  * payload（v4 词表闭集 + 语义）：`tool/result` 消息是 role `'tool'` 平铺
    content + 顶层 toolCallId/isError；`developer/message` 工具增删块；新增
    `workspace/changes`。
  * 关系投影（relationship projection）：`tool/result` 投影回 v3 包裹形
    （role user + tool-result 块；`forked` 合成 not-started 结果投影回
    `interrupted-` 身份与文案）、`system/message` 投影为 user/message、
    `developer/message` 保留类型并加入 stepEvents（复用 v0→v1 关系状态机
    relationships.py），端点名换回 v2 形。

消息措辞逐字对齐上游（field 无引号方言）。上游锚点：
validation.ts:19-130、developer.ts:6-113、system-message.ts:5-64、
retired-syntax.ts:5-75、fork-result.ts:10-40。
"""
from __future__ import annotations

from typing import Any, Iterable

from . import dispositions as _disp
from .helpers import (
    count,
    deep_equal,
    exact_keys,
    fail,
    lossless_json,
    released_v0_record,
    safe_integer,
    unsupported,
)
from .payload_validation import assert_released_payload_semantics
from .relationships import assert_released_artifact_relationships
from .validate_v2 import (
    RELEASED_V2_RELATIONSHIP_EXTENSIONS,
    assert_released_v2_header,
)

__all__ = [
    "RELEASED_V4_RELATIONSHIP_EXTENSIONS",
    "SURFACE_TYPES_V4",
    "assert_released_v4_artifact",
    "assert_released_v4_header",
    "assert_released_v4_physical_artifact",
    "assert_v4_event",
    "restore_released_v4_artifact",
]

_HEADER_REQUIRED = ("version", "id", "createdAt", "isSeeded", "delegationDepth")
_HEADER_OPTIONAL = ("cwd", "parentSession", "origin", "agentPreset")
_EVENT_REQUIRED = ("type", "seq", "time", "data")
_SURFACE_OPTIONAL = ("ignorable", "surfaceOp", "sourceEventSeqs")
_LOG_OPTIONAL = ("ignorable",)

#: V4 surface 类型（第 5 种：developer/message，工具增删的派生历史）。
SURFACE_TYPES_V4 = frozenset(
    {"system/message", "developer/message", "user/message", "assistant/message", "tool/result"})

#: v2 遗留 PTC tag（required 读面拒、ignorable 收留为 opaque）。
_OBSOLETE_PTC_TAGS = frozenset({"tool/code-dispatch-start", "tool/code-dispatch"})

#: v4 关系状态机扩展：v3 同款 + developer/message 计步（投影后仍以原名保留）。
RELEASED_V4_RELATIONSHIP_EXTENSIONS = {
    **RELEASED_V2_RELATIONSHIP_EXTENSIONS,
    "stepEvents": frozenset({"assistant/attempt", "developer/message"}),
}

#: interrupted 崩溃恢复 not-started 文案（relationships.py 稳定修复身份匹配）。
_INTERRUPTED_NOT_STARTED_TEXT = (
    "The tool call was interrupted before the Harness recorded it as started. "
    "Retry it if it is still needed."
)


def assert_released_v4_header(header: Any) -> None:
    """released v4 精确逻辑头（上游 assertReleasedV4Header：字段闭集与 v2/v3
    相同，version 门槛 4）。"""
    if not isinstance(header, dict):
        raise fail("format v4 header must be an object")
    exact_keys(header, _HEADER_REQUIRED, _HEADER_OPTIONAL, "format v4 header",
               member="field", quote=False, missing_first=True)
    if header.get("version") != 4:
        raise fail("expected format v4 header")
    assert_released_v2_header({**header, "version": 2})


def _is_obsolete(etype: Any) -> bool:
    return etype in _OBSOLETE_PTC_TAGS


def _assert_v4_event_admission(event: dict) -> None:
    """required 遗留 PTC tag 拒绝（上游 assertV4RetiredSyntax 首段）。"""
    if _is_obsolete(event.get("type")) and event.get("ignorable") is not True:
        raise unsupported(
            "format v4 rejects retired event type " + str(event.get("type")))


def _known_v4_type(etype: Any, known_event_types: Iterable[str] | None) -> bool:
    if _is_obsolete(etype):
        return False
    if etype in SURFACE_TYPES_V4:
        return True
    if etype in _disp.RELEASED_V4_EVENT_DISPOSITIONS:
        return True
    if known_event_types is not None and etype in frozenset(known_event_types):
        return True
    return False


def _is_event_seq(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def assert_v4_event(event: dict, known_event_types: Iterable[str] | None = None) -> None:
    """V4 信封校验（上游 payload.ts assertV3Event 的 v4 版）：未知类型按 opaque
    收留、envelope 键集分档、replace 形状/端点、assistant 禁 sources、structural
    行、canonical 载荷。"""
    etype = event.get("type")
    seq = event.get("seq")
    subject = f"format v4 {etype} at seq {seq}"
    opaque = not _known_v4_type(etype, known_event_types)
    surface = etype in SURFACE_TYPES_V4
    optional = _SURFACE_OPTIONAL if (surface or opaque) else _LOG_OPTIONAL
    exact_keys(event, _EVENT_REQUIRED, optional, subject,
               member="field", quote=False, missing_first=True)
    if not isinstance(etype, str):
        raise fail(f"{subject} type must be a string")
    count(seq, f"{subject} seq")
    safe_integer(event.get("time"), f"{subject} time")
    if "ignorable" in event and event.get("ignorable") is not True:
        raise fail(f"{subject} ignorable must be true when present")
    if surface:
        operation = event.get("surfaceOp")
        if operation is None:
            raise fail(f"{subject} requires a surfaceOp marker")
        if operation != "append":
            replace = released_v0_record(operation, f"{subject} surfaceOp")
            if (len(replace) != 3 or replace.get("op") != "replace"
                    or "startSeq" not in replace or "endSeq" not in replace):
                raise fail(f"{subject} requires exact replace fields op/startSeq/endSeq")
            for key in ("startSeq", "endSeq"):
                if count(replace.get(key), f"{subject} surfaceOp {key}") >= seq:
                    raise fail(f"{subject} replacement endpoints must reference earlier events")
        sources = event.get("sourceEventSeqs")
        if etype == "assistant/message" and sources is not None:
            raise fail(f"{subject} embeds its stream and cannot carry sourceEventSeqs")
        if sources is not None:
            if not isinstance(sources, list) or len(sources) == 0:
                raise fail(f"{subject} sourceEventSeqs must be a non-empty array")
            seen: set[int] = set()
            for source in sources:
                member = count(source, f"{subject} sourceEventSeqs member")
                if member >= seq or member in seen:
                    raise fail(f"{subject} sourceEventSeqs must be unique earlier seqs")
                seen.add(member)
    _assert_v4_structural_row(event)
    _assert_v4_canonical_payload(event)


def _assert_v4_structural_row(event: dict) -> None:
    """V4 结构行拒绝（上游 retired-syntax/system-message 的 local 部分）：
    retired header.system、retired tool-result 包裹块、system/developer/tool-result
    消息形状。"""
    etype = event.get("type")
    if etype == "request/header":
        data = released_v0_record(event.get("data"), "request/header data")
        header = released_v0_record(data.get("header"), "request header")
        if "system" in header:
            raise unsupported("format v4 request/header rejects retired header.system")
    elif etype == "system/message":
        data = released_v0_record(event.get("data"), "system/message data")
        _assert_system_shape(data, "system/message data")
    elif etype == "developer/message":
        data = released_v0_record(event.get("data"), "developer/message data")
        _assert_developer_shape(data, "developer/message data")
    elif etype == "tool/result":
        data = released_v0_record(event.get("data"), "tool/result data")
        _assert_tool_result_shape(data, "tool/result data")
    _assert_retired_content(event)


def _assert_system_shape(data: dict, label: str) -> None:
    """system/message 形状（上游 assertV4SystemMessageFields）：恰三键 + 正坐标 +
    消息四键 + role/source 门槛。"""
    exact_keys(data, ("turn", "step", "message"), (), label)
    for coordinate in ("turn", "step"):
        if count(data.get(coordinate), f"{label} {coordinate}") == 0:
            raise fail(f"{label} {coordinate} must be positive")
    message = released_v0_record(data.get("message"), f"{label} system message")
    for key in ("id", "role", "source", "content"):
        if key not in message:
            raise fail(f'{label} system message lacks required field {key}')
    if not isinstance(message.get("id"), str) or len(message["id"]) == 0 \
            or message.get("role") != "system":
        raise fail(f"{label} system message requires an id and system role")
    if not isinstance(message.get("content"), list):
        raise fail(f"{label} system message requires content array")
    source = released_v0_record(message.get("source"), f"{label} system source")
    kind = source.get("kind")
    if not isinstance(kind, str) or len(kind) == 0 or kind == "plugin":
        raise fail(f"{label} system source requires a producer-owned kind")


def _assert_developer_shape(data: dict, label: str) -> None:
    """developer/message 形状（上游 assertV4DeveloperData）：正坐标 + role
    developer + 生产者自有 source + 工具增删块规则 + headerSeq 绑定。"""
    exact_keys(data, ("turn", "step", "message"), ("headerSeq",), label)
    for coordinate in ("turn", "step"):
        if count(data.get(coordinate), f"{label} {coordinate}") == 0:
            raise fail(f"{label} {coordinate} must be positive")
    message = released_v0_record(data.get("message"), f"{label} developer message")
    if message.get("role") != "developer":
        raise fail(f"{label} requires a developer message")
    if not isinstance(message.get("id"), str) or len(message["id"]) == 0 \
            or not isinstance(message.get("content"), list):
        raise fail("format v4 developer message requires id, role, content, and a producer-owned source")
    source = released_v0_record(message.get("source"), f"{label} developer source")
    kind = source.get("kind")
    if not isinstance(kind, str) or len(kind) == 0 or kind == "plugin":
        raise fail("format v4 developer message requires id, role, content, and a producer-owned source")
    has_additions = False
    for block in message["content"]:
        if not isinstance(block, dict) or block.get("type") not in ("tool-addition", "tool-removal"):
            continue
        if not isinstance(block.get("toolName"), str) or len(block["toolName"]) == 0:
            raise fail(f"format v4 {block.get('type')} requires a nonempty toolName")
        if block.get("type") == "tool-addition":
            has_additions = True
            if "tool" in block:
                raise fail("format v4 tool-addition must omit inline tool definitions")
    if has_additions:
        count(data.get("headerSeq"), "developer/message headerSeq")
    elif "headerSeq" in data:
        raise fail("format v4 developer/message must omit headerSeq without tool additions")


def _assert_tool_result_shape(data: dict, label: str) -> None:
    """tool/result 形状（上游 assertV4ToolResultMessage）：role tool + 顶层
    toolCallId/source 一致 + 平铺 content 无包裹块 + isError 布尔。"""
    message = released_v0_record(data.get("message"), f"{label} message")
    if message.get("role") != "tool":
        raise fail(f"{label} requires a tool-role message")
    if not isinstance(message.get("id"), str) or len(message["id"]) == 0:
        raise fail(f"{label} requires a first-class message with a string id")
    tool_call_id = message.get("toolCallId")
    source = released_v0_record(message.get("source"), f"{label} source")
    if not isinstance(tool_call_id, str) or len(tool_call_id) == 0 \
            or source.get("callId") != tool_call_id or source.get("kind") != "tool":
        raise fail(f"{label} requires toolCallId matching its tool source")
    content = message.get("content")
    if not isinstance(content, list):
        raise fail(f"{label} requires array content")
    if any(isinstance(block, dict) and block.get("type") == "tool-result" for block in content):
        raise fail(f"{label} content must not contain a released tool-result wrapper")
    if message.get("isError") is not None and not isinstance(message.get("isError"), bool):
        raise fail(f"{label} isError must be boolean when present")


def _assert_retired_content(event: dict) -> None:
    """retired tool-result 包裹块在所有解释过的 content 槽拒绝
    （上游 assertV4RetiredSyntax：只查直接块标签）。"""
    etype = event.get("type")
    data = event.get("data")
    if not isinstance(data, dict):
        return
    subject = f"format v4 {etype} at seq {event.get('seq')} content"

    def content_of(value: Any) -> None:
        if isinstance(value, list):
            for block in value:
                if isinstance(block, dict) and block.get("type") == "tool-result":
                    raise fail(f"{subject} must not contain a released tool-result wrapper")

    def message_content(value: Any) -> None:
        if isinstance(value, dict):
            content_of(value.get("content"))

    if etype == "user/message":
        content_of(data.get("content"))
    elif etype in ("developer/message", "assistant/message", "team/message/queued"):
        message_content(data.get("message"))
    elif etype in ("agent/inbox/spliced", "session/title-llm-request"):
        messages = data.get("inserted" if etype == "agent/inbox/spliced" else "messages")
        if isinstance(messages, list):
            for message in messages:
                message_content(message)
    elif etype == "compaction/summary":
        content_of(data.get("summary"))
        content_of(data.get("rawOutput"))
    elif etype == "tool/ptc-dispatch":
        content_of(data.get("content"))
    if etype in ("assistant/message", "assistant/attempt") and isinstance(data.get("stream"), list):
        for entry in data["stream"]:
            if not isinstance(entry, dict) or entry.get("type") != "chunk":
                continue
            chunk = entry.get("chunk")
            if not isinstance(chunk, dict):
                continue
            if chunk.get("type") == "block-end" and isinstance(chunk.get("block"), dict) \
                    and chunk["block"].get("type") == "tool-result":
                raise fail(f"{subject} must not contain a released tool-result wrapper")
            if chunk.get("type") == "block-start" and chunk.get("blockType") == "tool-result":
                raise fail(f"{subject} must not contain a released tool-result wrapper")


def _assert_v4_canonical_payload(event: dict) -> None:
    """V4 canonical 载荷（上游 assertCanonicalPayload）：request/header 空可选
    省略 + tool/result error↔isError 一致性。"""
    subject = f"format v4 {event.get('type')} at seq {event.get('seq')}"
    if event.get("type") == "request/header":
        data = released_v0_record(event.get("data"), f"{subject} data")
        header = released_v0_record(data.get("header"), f"{subject} header")
        if (isinstance(header.get("tools"), list) and len(header["tools"]) == 0) \
                or (isinstance(header.get("adapterDefaults"), dict)
                    and len(header["adapterDefaults"]) == 0):
            raise fail(f"{subject} empty optional header fields must be omitted")
    if event.get("type") != "tool/result":
        return
    data = released_v0_record(event.get("data"), f"{subject} data")
    if data.get("error") is None:
        return
    message = released_v0_record(data.get("message"), f"{subject} message")
    if message.get("isError") is not True:
        raise fail(f"{subject} carries error metadata for a non-error tool result")


def _assert_v4_developer_header(event: dict, events: list) -> None:
    """developer/message 的 headerSeq 绑定（上游 v4 relationships.developer）：
    headerSeq 指向更早的 request/header；每个 tool-addition 的 toolName 在该
    header 的工具定义中恰好命中一个，且定义含 string description 与 object
    parameters。"""
    if event.get("type") != "developer/message":
        return
    data = event.get("data")
    if not isinstance(data, dict) or data.get("headerSeq") is None:
        return
    header_seq = data.get("headerSeq")
    if not _is_event_seq(header_seq) or header_seq >= event.get("seq") \
            or header_seq >= len(events):
        raise fail("developer/message headerSeq must reference an earlier request/header")
    header_event = events[header_seq]
    if not isinstance(header_event, dict) or header_event.get("type") != "request/header":
        raise fail("developer/message headerSeq must reference an earlier request/header")
    header_data = header_event.get("data")
    header = header_data.get("header") if isinstance(header_data, dict) else None
    tools = header.get("tools") if isinstance(header, dict) else None
    tools = tools if isinstance(tools, list) else []
    message = data.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    for block in content if isinstance(content, list) else []:
        if not isinstance(block, dict) or block.get("type") != "tool-addition":
            continue
        tool_name = block.get("toolName")
        definitions = [tool for tool in tools
                       if isinstance(tool, dict) and tool.get("name") == tool_name]
        if len(definitions) != 1:
            raise fail(
                f'developer/message tool-addition "{tool_name}" must name exactly one '
                f"tool in headerSeq {header_seq}")
        definition = definitions[0]
        if not isinstance(definition.get("description"), str) \
                or not isinstance(definition.get("parameters"), dict):
            raise fail(
                f'developer/message tool-addition "{tool_name}" requires a complete '
                f"tool definition in headerSeq {header_seq}")


def _assert_v4_message_sources(event: dict) -> None:
    """解释过的消息槽要求生产者自有 source kind（上游 assertV4MessageSources）。"""
    etype = event.get("type")
    data = event.get("data")
    if not isinstance(data, dict):
        return
    if etype == "user/message":
        messages = [data]
    elif etype in ("developer/message", "system/message", "assistant/message", "tool/result"):
        messages = [data.get("message")]
    elif etype == "agent/inbox/spliced":
        messages = data.get("inserted")
    elif etype == "session/title-llm-request":
        messages = data.get("messages")
    else:
        return
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, dict):
            continue
        source = message.get("source")
        if not isinstance(source, dict):
            continue
        kind = source.get("kind")
        if not isinstance(kind, str) or len(kind) == 0 or kind == "plugin":
            raise fail("format v4 message requires a producer-owned source kind")


def _assert_v4_fork_result(event: dict) -> None:
    """V4 fork 合成 not-started 结果身份校验（上游 assertV4ForkResult）。"""
    if event.get("type") != "tool/result":
        return
    data = event.get("data")
    if not isinstance(data, dict):
        return
    error = data.get("error")
    message = data.get("message")
    if not isinstance(error, dict) or error.get("code") != "TOOL_NOT_STARTED" \
            or not isinstance(message, dict) \
            or not isinstance(message.get("id"), str) \
            or not message["id"].startswith("forked-tool-result-"):
        return
    source = message.get("source")
    call_id = source.get("callId") if isinstance(source, dict) else None
    prefix = f"forked-tool-result-{call_id}-"
    if not isinstance(call_id, str) or not message["id"].startswith(prefix):
        raise fail("invalid V4 not-started fork result")
    suffix = message["id"][len(prefix):]
    operation = event.get("surfaceOp")
    replacement = isinstance(operation, dict) and operation.get("op") == "replace"
    sequence = int(suffix) if suffix.isdigit() else None
    content = message.get("content")
    text = content[0] if isinstance(content, list) and len(content) == 1 else None
    source_event_seqs = event.get("sourceEventSeqs")
    if sequence is None or not _is_event_seq(sequence) \
            or (sequence >= event.get("seq") if replacement else sequence != event.get("seq")) \
            or error.get("name") != "ToolNotStartedError" \
            or (not replacement and (source_event_seqs is not None or operation != "append")) \
            or (replacement and (not isinstance(source_event_seqs, list)
                                 or len(source_event_seqs) != 1
                                 or source_event_seqs[0] != sequence)) \
            or message.get("role") != "tool" or message.get("isError") is not True \
            or message.get("toolCallId") != call_id \
            or not isinstance(source, dict) or source.get("kind") != "tool" \
            or not isinstance(text, dict) or text.get("type") != "text" \
            or not isinstance(text.get("text"), str):
        raise fail("invalid V4 not-started fork result")


def _relationship_projection(event: dict) -> dict:
    """冻结关系视图投影：`system/message`→user/message、`tool/result` 平铺消息
    还原为 v3 包裹形（`forked` 合成 not-started 结果投影回 interrupted 身份与
    文案）、其余原样（developer/message 由 stepEvents 处理）。"""
    etype = event.get("type")
    if etype == "system/message":
        data = released_v0_record(event.get("data"), "system data")
        message = released_v0_record(data.get("message"), "system message")
        return {**event, "type": "user/message", "data": {**message, "role": "user"}}
    if etype != "tool/result":
        return event
    data = released_v0_record(event.get("data"), "tool result")
    message = released_v0_record(data.get("message"), "tool message")
    if message.get("role") != "tool":
        return event
    call_id = message.get("toolCallId")
    content = list(message.get("content") or [])
    wrapper: dict[str, Any] = {"type": "tool-result", "toolCallId": call_id,
                               "content": content}
    if message.get("isError") is not None:
        wrapper["isError"] = message.get("isError")
    projected_message: dict[str, Any] = {**message, "role": "user", "content": [wrapper]}
    error = data.get("error")
    if isinstance(error, dict) and error.get("code") == "TOOL_NOT_STARTED" \
            and isinstance(message.get("id"), str) \
            and message["id"].startswith("forked-tool-result-"):
        prefix = f"forked-tool-result-{call_id}-"
        suffix = message["id"][len(prefix):]
        projected_message["id"] = f"interrupted-tool-result-{call_id}-{suffix}"
        wrapper["content"] = [{"type": "text", "text": _INTERRUPTED_NOT_STARTED_TEXT}]
    return {**event, "data": {**data, "message": projected_message}}


def _rename_replace_endpoints(event: dict) -> dict:
    """冻结关系视图的端点名换回 v2 形（start/end）。"""
    etype = event.get("type")
    if etype not in SURFACE_TYPES_V4 or event.get("surfaceOp") == "append" \
            or event.get("surfaceOp") is None:
        return event
    replace = released_v0_record(event.get("surfaceOp"), "surface replacement")
    return {**event, "surfaceOp": {"op": "replace",
                                   "start": replace.get("startSeq"),
                                   "end": replace.get("endSeq")}}


def _assert_v4_payloads(events: list[dict]) -> None:
    """V4 逐事件 payload 语义（上游 v3→v4 target 校验）：v4 词表闭集 +
    opaque 快照 + 内嵌流三事实复核。未知/废弃 ignorable 事件按 opaque 跳过。"""
    for event in events:
        etype = event.get("type")
        if not _known_v4_type(etype, None):
            continue
        disposition = _disp.RELEASED_V4_EVENT_DISPOSITIONS.get(etype)
        if disposition is None:
            continue
        seq = event.get("seq")
        data = released_v0_record(event.get("data"), f"{etype} {seq} data")
        exact_keys(data, tuple(disposition["required"]), tuple(disposition["optional"]),
                   f"{etype} {seq} data", member="field", quote=False, missing_first=True)
        for key in disposition["opaque"]:
            if key in data:
                lossless_json(data[key], f"{etype} {seq} opaque {key}")
        if etype in ("assistant/attempt", "assistant/message"):
            from ....llm.assistant_stream import expand_assistant_stream  # noqa: PLC0415
            from ....llm.protocol import BlockAssembler  # noqa: PLC0415
            turn = count(data.get("turn"), f"{etype} {seq} turn")
            step = count(data.get("step"), f"{etype} {seq} step")
            assembler = BlockAssembler()
            try:
                timed = expand_assistant_stream(list(data.get("stream")))
                for member in timed:
                    assert_released_payload_semantics({
                        "type": "assistant/chunk",
                        "seq": seq,
                        "time": member.time,
                        "data": {"turn": turn, "step": step, "chunk": member.chunk},
                    }, 4)
                    assembler.push(member.chunk)
            except Exception as error:  # noqa: BLE001
                raise fail(f"{etype} {seq} has an invalid embedded stream") from error
            if etype == "assistant/attempt":
                continue
            assert_released_payload_semantics(event, 4)
            if timed:
                message = released_v0_record(data.get("message"),
                                             f"assistant/message {seq} message")
                content = assembler.interrupted_blocks() if data.get("interrupted") is True \
                    else assembler.blocks()
                if not deep_equal(message.get("content"), content):
                    raise fail(
                        f"assistant/message {seq} message content disagrees with its embedded stream")
                if not deep_equal(data.get("usage"), assembler.usage):
                    raise fail(
                        f"assistant/message {seq} usage disagrees with its embedded stream")
                source = released_v0_record(message.get("source"),
                                            f"assistant/message {seq} source")
                if not deep_equal(source.get("replayState"), assembler.replay_state):
                    raise fail(
                        f"assistant/message {seq} replay state disagrees with its embedded stream")
            continue
        if etype == "session/end-seed":
            if "inherited" in data and data.get("inherited") is not True:
                raise fail(f"session/end-seed {seq} inherited must be true when present")
            continue
        assert_released_payload_semantics(event, 4)


def _validate_released_v4_artifact(artifact: dict, mode: str,
                                   known_event_types: frozenset | None = None) -> None:
    """v4 三维校验核心（上游 restoreReleasedV4Artifact + 目录 target 校验）。

    physical：只校验物理 header + 事件信封 + 继承切点；current：+ 安装门（未知非
    ignorable 拒）+ 本地消息/退役语法校验 + 关系状态机；target：+ 逐事件 payload
    语义（v4 表）+ 内嵌流复核。
    """
    header = artifact["header"]
    assert_released_v4_header(header)
    events = artifact["events"]
    cut = count(artifact.get("inherited_event_count"), "format v4 inherited event count")
    if cut > len(events):
        raise fail("format v4 inherited event count exceeds its events")
    if not header.get("isSeeded") and cut != 0:
        raise fail("unseeded format v4 Session has inherited events")
    projected: list[dict] = []
    last_inherited_marker: int | None = None
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise fail(f"format v4 event {index} must be an object")
        etype = event.get("type")
        if mode != "physical":
            _assert_v4_event_admission(event)
            if not _known_v4_type(etype, known_event_types) and event.get("ignorable") is not True:
                raise unsupported(
                    f"format v4 contains unknown event type {etype!r} at seq {index}")
            assert_v4_event(event, known_event_types)
            _assert_v4_developer_header(event, events)
            _assert_v4_message_sources(event)
            _assert_v4_fork_result(event)
        seq = count(event.get("seq"), f"format v4 event {index} seq")
        if seq != index:
            raise fail(f"format v4 event {index} is not dense")
        safe_integer(event.get("time"), f"format v4 event {index} time")
        if etype == "session/end-seed":
            data = released_v0_record(event.get("data"), f"session/end-seed {index} data")
            if data.get("inherited") is True:
                last_inherited_marker = index
        projected.append(_relationship_projection(event))
    if header.get("isSeeded") and last_inherited_marker != cut:
        raise fail("format v4 seeded header disagrees with its last inherited end-seed marker")
    if not header.get("isSeeded") and last_inherited_marker is not None:
        raise fail("format v4 unseeded Session contains an inherited end-seed marker")
    if mode == "target":
        _assert_v4_payloads(events)
    if mode in ("target", "current"):
        projected_renamed = [_rename_replace_endpoints(ev) for ev in projected]
        assert_released_artifact_relationships(
            {"header": header, "inherited_event_count": cut, "events": projected_renamed},
            RELEASED_V4_RELATIONSHIP_EXTENSIONS)


def assert_released_v4_artifact(artifact: dict) -> None:
    """released v4 写出的精确逻辑镜像（target 全量）。"""
    _validate_released_v4_artifact(artifact, "target")


def assert_released_v4_physical_artifact(artifact: dict) -> None:
    """v4 词表中立物理解码校验（不解释事件词表/payload；v4 codec 读向用）。"""
    _validate_released_v4_artifact(artifact, "physical")


def restore_released_v4_artifact(artifact: dict,
                                 known_event_types: Iterable[str]) -> dict:
    """以当前安装词表恢复 v4（信封 + 安装门 + 本地消息/退役语法校验 + 关系）。"""
    _validate_released_v4_artifact(artifact, "current", frozenset(known_event_types))
    return artifact
