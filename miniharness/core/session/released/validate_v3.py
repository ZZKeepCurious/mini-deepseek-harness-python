"""released v3 校验（上游 session-format-v2-to-v3/src/{validation,payload}.ts 同步载体）。

V3 三层：
  * 信封（assertV3Event）：四 surface 类型必带 surfaceOp；replace 恰
    op/startSeq/endSeq 三键且端点早于自身；assistant/message 禁
    sourceEventSeqs；已知 log-only 仅 ignorable 可选；未知/废弃类型按 opaque
    收留（ignorable）——「未知 required 事件不吞」由安装门（restore 模式的
    knownEventTypes 检查）兜底。
  * 系统头（restoreReleasedV3Artifact）：system/message 必须匹配开步；
    保护头跟踪（首个 surface 事件才能成为 head；replace 必须恰指 head）；
    非 system replace 不得遮蔽 head；compaction shadowedSeqs 不得含 head。
  * 关系投影（relationshipEvent）：ptc→code-dispatch 改名、废弃 ignorable
    事件转 opaque 占位、system→user/message 投影、TOOL_NOT_STARTED 修复 id
    后缀换目标 seq——复用 v2 关系状态机（relationships.py）与 payload
    语义（payload_validation.py，version=3）。

消息措辞逐字对齐上游（field 无引号方言）。上游锚点：
validation.ts:12-120、payload.ts:212-337。
"""
from __future__ import annotations

from typing import Any, Iterable

from . import dispositions as _disp
from .helpers import (
    count,
    deep_equal,
    exact_keys,
    fail,
    js_stringify,
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
    "RELEASED_V3_RELATIONSHIP_EXTENSIONS",
    "SURFACE_TYPES_V3",
    "assert_released_v3_artifact",
    "assert_released_v3_header",
    "restore_released_v3_artifact",
]

_HEADER_REQUIRED = ("version", "id", "createdAt", "isSeeded", "delegationDepth")
_HEADER_OPTIONAL = ("cwd", "parentSession", "origin", "agentPreset")
_EVENT_REQUIRED = ("type", "seq", "time", "data")
_SURFACE_OPTIONAL = ("ignorable", "surfaceOp", "sourceEventSeqs")
_LOG_OPTIONAL = ("ignorable",)

#: V3 surface 类型（第 4 种：system/message，surface node 0）。
SURFACE_TYPES_V3 = frozenset(
    {"system/message", "user/message", "assistant/message", "tool/result"})

#: v2 遗留 PTC tag（required 读面拒、ignorable 收留为 opaque）。
_OBSOLETE_PTC_TAGS = frozenset({"tool/code-dispatch-start", "tool/code-dispatch"})

#: v3 关系状态机扩展（v2 同款：assistant/attempt 计步 + framed title 保留）。
RELEASED_V3_RELATIONSHIP_EXTENSIONS = dict(RELEASED_V2_RELATIONSHIP_EXTENSIONS)


def _is_repair_identity(id_: Any, call_id: Any) -> bool:
    """稳定修复 id 识别（上游 payload.ts isRepairIdentity）：历史后缀不是
    目标 seq 坐标。"""
    if not isinstance(call_id, str):
        return False
    prefix = "interrupted-tool-result-" + call_id + "-"
    if not isinstance(id_, str) or not id_.startswith(prefix):
        return False
    suffix = id_[len(prefix):]
    return suffix == "0" or (suffix.isdigit() and not suffix.startswith("0"))


def assert_released_v3_header(header: Any) -> None:
    """released v3 精确逻辑头（上游 assertReleasedV3Header：v2 字段复用 +
    version 3 门槛；v2 字段错误文案随复用沿用）。"""
    if not isinstance(header, dict):
        raise fail("format v3 header must be an object")
    exact_keys(header, _HEADER_REQUIRED, _HEADER_OPTIONAL, "format v3 header",
               member="field", quote=False, missing_first=True)
    if header.get("version") != 3:
        raise fail("expected format v3 header")
    assert_released_v2_header({**header, "version": 2})


def _is_obsolete(etype: Any) -> bool:
    return etype in _OBSOLETE_PTC_TAGS


def _assert_v3_event_admission(event: dict) -> None:
    """required 遗留 PTC tag 拒绝（上游 assertV3EventAdmission）：native V3
    不解释其 payload；ignorable 收留为 opaque。"""
    if _is_obsolete(event.get("type")) and event.get("ignorable") is not True:
        raise unsupported(
            "format v3 contains unknown event type "
            + js_stringify(event.get("type")) + " at seq " + js_stringify(event.get("seq")))


def _known_v3_type(etype: Any, known_event_types: Iterable[str] | None) -> bool:
    if _is_obsolete(etype):
        return False
    if etype in SURFACE_TYPES_V3:
        return True
    if etype in _disp.RELEASED_V3_EVENT_DISPOSITIONS:
        return True
    if etype in ("tool/ptc-dispatch", "tool/ptc-dispatch-start"):
        return True
    if known_event_types is not None and etype in frozenset(known_event_types):
        return True
    return False


def _is_event_seq(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def assert_v3_event(event: dict, known_event_types: Iterable[str] | None = None) -> None:
    """V3 信封校验（上游 payload.ts assertV3Event）：未知类型按 opaque 收留、
    envelope 键集分档、replace 形状/端点、assistant 禁 sources、structural 行、
    canonical 载荷。"""
    etype = event.get("type")
    seq = event.get("seq")
    subject = f"format v3 {etype} at seq {seq}"
    opaque = not _known_v3_type(etype, known_event_types)
    surface = etype in SURFACE_TYPES_V3
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
    _assert_v3_structural_row(event)
    _assert_canonical_payload(event)


def _assert_v3_structural_row(event: dict) -> None:
    """V3 结构行拒绝（上游 assertV3StructuralRow）：retired header.system、
    system/message 形状。"""
    etype = event.get("type")
    if etype == "request/header":
        data = released_v0_record(event.get("data"), "request/header data")
        header = released_v0_record(data.get("header"), "request header")
        if "system" in header:
            raise unsupported("format v3 request/header rejects retired header.system")
    elif etype == "system/message":
        data = released_v0_record(event.get("data"), "system/message data")
        _assert_system_shape(data, "system/message data")


def _assert_system_shape(data: dict, label: str) -> None:
    """system/message 形状（上游 assertSystem）：恰三键 + 正坐标 + 消息四键
    + role/source 门槛。"""
    exact_keys(data, ("turn", "step", "message"), (), label)
    for coordinate in ("turn", "step"):
        if count(data.get(coordinate), f"{label} {coordinate}") == 0:
            raise fail(f"{label} {coordinate} must be positive")
    message = released_v0_record(data.get("message"), f"{label} system message")
    exact_keys(message, ("id", "role", "source", "content"), (), f"{label} system message")
    if not isinstance(message.get("id"), str) or len(message["id"]) == 0 \
            or message.get("role") != "system":
        raise fail(f"{label} system message requires an id and system role")
    source = released_v0_record(message.get("source"), f"{label} system source")
    if source.get("kind") != "plugin" or not isinstance(source.get("plugin"), str) \
            or len(source["plugin"]) == 0:
        raise fail(f"{label} system message requires plugin source")


def _assert_canonical_payload(event: dict) -> None:
    """V3 canonical 载荷（上游 assertCanonicalPayload）：request/header 空可选
    省略 + tool/result error↔isError 一致性。"""
    subject = f"format v3 {event.get('type')} at seq {event.get('seq')}"
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
    content = message.get("content")
    if (not isinstance(content, list) or len(content) != 1
            or not isinstance(content[0], dict)
            or content[0].get("type") != "tool-result"
            or content[0].get("isError") is not True):
        raise fail(f"{subject} carries error metadata for a non-error tool result")


def _relationship_projection(event: dict, seq: int) -> dict:
    """冻结关系视图投影（上游 validation.ts relationshipEvent）：ptc 改名回
    code-dispatch、废弃 ignorable 转 opaque 占位、system→user/message、
    TOOL_NOT_STARTED 修复 id 后缀换目标 seq。"""
    etype = event.get("type")
    if etype == "tool/ptc-dispatch-start":
        return {**event, "type": "tool/code-dispatch-start"}
    if etype == "tool/ptc-dispatch":
        return {**event, "type": "tool/code-dispatch"}
    if _is_obsolete(etype):
        _assert_v3_event_admission(event)
        return {**event, "type": "v3/opaque-released-event"}
    if etype == "system/message":
        message = released_v0_record(
            released_v0_record(event.get("data"), "system data")["message"],
            "system message")
        return {**event, "type": "user/message", "data": {**message, "role": "user"}}
    if etype != "tool/result":
        return event
    data = released_v0_record(event.get("data"), "tool result")
    if data.get("error") is None:
        return event
    error = released_v0_record(data.get("error"), "tool error")
    if error.get("code") != "TOOL_NOT_STARTED":
        return event
    message = released_v0_record(data.get("message"), "tool message")
    source = released_v0_record(message.get("source"), "tool source")
    call_id = source.get("callId")
    if not _is_repair_identity(message.get("id"), call_id):
        return event
    prefix = "interrupted-tool-result-" + call_id + "-"
    return {**event, "data": {**data,
            "message": {**message, "id": f"{prefix}{seq}"}}}


def _validate_released_v3_artifact(artifact: dict, mode: str,
                                   known_event_types: frozenset | None = None) -> None:
    """v3 三维校验核心（上游 restoreReleasedV3Artifact + 目录 target 校验）。

    physical：信封 + 继承切点（不解释词表）；current：+ 安装门（未知非
    ignorable 拒）+ 系统头校验；target：+ 逐事件 payload 语义（v3 表）+
    投影后关系状态机 + 内嵌流复核。
    """
    header = artifact["header"]
    assert_released_v3_header(header)
    events = artifact["events"]
    cut = count(artifact.get("inherited_event_count"), "format v3 inherited event count")
    if cut > len(events):
        raise fail("format v3 inherited event count exceeds its events")
    if not header.get("isSeeded") and cut != 0:
        raise fail("unseeded format v3 Session has inherited events")
    projected: list[dict] = []
    last_inherited_marker: int | None = None
    open_step: dict | None = None
    head: int | None = None
    has_surface = False
    for index, event in enumerate(events):
        if not isinstance(event, dict):
            raise fail(f"format v3 event {index} must be an object")
        etype = event.get("type")
        if mode != "physical":
            _assert_v3_event_admission(event)
            assert_v3_event(event, known_event_types)
        seq = count(event.get("seq"), f"format v3 event {index} seq")
        if seq != index:
            raise fail(f"format v3 event {index} is not dense")
        safe_integer(event.get("time"), f"format v3 event {index} time")
        if etype == "step/start":
            data = released_v0_record(event.get("data"), f"{etype} {seq} data")
            open_step = {"turn": data.get("turn"), "step": data.get("step")}
        elif etype in ("step/end", "turn/end"):
            open_step = None
        if etype == "system/message":
            data = released_v0_record(event.get("data"), "system/message")
            if open_step is None or open_step["turn"] != data.get("turn") \
                    or open_step["step"] != data.get("step"):
                raise fail("system/message does not match an open step")
            operation = event.get("surfaceOp")
            if has_surface and head is None:
                raise fail("system/message requires a protected first surface head")
            if operation == "append":
                if not has_surface:
                    head = seq
            else:
                replace = released_v0_record(operation, "system replacement")
                if replace.get("startSeq") == head or replace.get("endSeq") == head:
                    if replace.get("startSeq") != head or replace.get("endSeq") != head:
                        raise fail(
                            "system/message must replace exactly the current system head")
                    head = seq
        elif etype in SURFACE_TYPES_V3 and event.get("surfaceOp") != "append":
            replace = released_v0_record(event.get("surfaceOp"), "surface replacement")
            if head is not None and (replace.get("startSeq") == head
                                     or replace.get("endSeq") == head):
                raise fail("surface replacement cannot shadow the protected system head")
        if etype in ("compaction/prune", "compaction/summary"):
            data = released_v0_record(event.get("data"), f"{etype} {seq} data")
            seqs = data.get("shadowedSeqs")
            if head is not None and isinstance(seqs, list) \
                    and any(member == head for member in seqs):
                raise fail("compaction cannot shadow the protected system head")
        if etype in SURFACE_TYPES_V3:
            has_surface = True
        if etype == "session/end-seed":
            data = released_v0_record(event.get("data"), f"session/end-seed {seq} data")
            if data.get("inherited") is True:
                last_inherited_marker = index
        projected.append(_relationship_projection(event, seq))
    if header.get("isSeeded") and last_inherited_marker != cut:
        raise fail("format v3 seeded header disagrees with its last inherited end-seed marker")
    if not header.get("isSeeded") and last_inherited_marker is not None:
        raise fail("format v3 unseeded Session contains an inherited end-seed marker")
    if mode == "current":
        # 安装门（上游 restoreReleasedV2Artifact(…, knownEventTypes, 3) 的
        # current 模式）：投影后未知类型必须 ignorable。投影后的 surface
        # replace 端点名换回 v2 形（start/end），复用 v2 信封/切点闭集
        # （unknown 文案随复用沿用 v2 措辞，与上游一致）。
        from .validate_v2 import _validate_released_v2_artifact  # noqa: PLC0415
        v2_header = {**header, "version": 2}
        projected_renamed = [_rename_replace_endpoints(ev) for ev in projected]
        _validate_released_v2_artifact(
            {"header": v2_header, "inherited_event_count": cut, "events": projected_renamed},
            "current", frozenset(known_event_types or ()))
        return
    if mode == "target":
        _assert_v3_payloads(events)
        from .validate_v2 import _validate_released_v2_artifact  # noqa: PLC0415
        v2_header = {**header, "version": 2}
        projected_renamed = [_rename_replace_endpoints(ev) for ev in projected]
        # 信封/切点复用 v2 物理档（词表门已由 v3 语义承担）；关系状态机
        # 在投影件上跑（ptc→code-dispatch 已改名）。
        _validate_released_v2_artifact(
            {"header": v2_header, "inherited_event_count": cut, "events": projected_renamed},
            "physical")
        assert_released_artifact_relationships(
            {"header": v2_header, "inherited_event_count": cut,
             "events": projected_renamed},
            RELEASED_V3_RELATIONSHIP_EXTENSIONS)


def _rename_replace_endpoints(event: dict) -> dict:
    """冻结关系视图的端点名换回 v2 形（start/end；上游 validation.ts:68-71）。"""
    etype = event.get("type")
    if etype not in SURFACE_TYPES_V3 or event.get("surfaceOp") == "append" \
            or event.get("surfaceOp") is None:
        return event
    replace = released_v0_record(event.get("surfaceOp"), "surface replacement")
    return {**event, "surfaceOp": {"op": "replace",
                                   "start": replace.get("startSeq"),
                                   "end": replace.get("endSeq")}}


def _assert_v3_payloads(events: list[dict]) -> None:
    """V3 逐事件 payload 语义（上游 v2→v3 目录 target 校验）：v3 词表闭集 +
    opaque 快照 + 内嵌流三事实复核。未知/废弃 ignorable 事件按 opaque 跳过
    （上游同款，不解释 payload）。"""
    for event in events:
        etype = event.get("type")
        if not _known_v3_type(etype, None):
            continue
        disposition = _disp.RELEASED_V3_EVENT_DISPOSITIONS.get(etype)
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
                    }, 3)
                    assembler.push(member.chunk)
            except Exception as error:  # noqa: BLE001
                raise fail(f"{etype} {seq} has an invalid embedded stream") from error
            if etype == "assistant/attempt":
                continue
            assert_released_payload_semantics(event, 3)
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
        assert_released_payload_semantics(event, 3)


def assert_released_v3_artifact(artifact: dict) -> None:
    """released v3 写出的精确逻辑镜像（target 全量）。"""
    _validate_released_v3_artifact(artifact, "target")


def restore_released_v3_artifact(artifact: dict,
                                 known_event_types: Iterable[str]) -> dict:
    """以当前安装词表恢复 v3（信封 + 安装门 + 系统头/开步/压缩头校验）。"""
    _validate_released_v3_artifact(artifact, "current", frozenset(known_event_types))
    return artifact
