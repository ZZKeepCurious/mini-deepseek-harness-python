"""released v2 制品校验（上游 session-format-v1-to-v2/src/validation.ts 逐字移植）。

v2 校验面在 v1 基础上进两档：内嵌流复核（BlockAssembler：assistant/attempt 组验证 +
assistant/message 三事实比对 content/usage/replayState）、marker/cut 双向一致性、
v2 关系扩展（assistant/attempt 参与 step 生命周期；framed title 请求文本保留不重释）。
消息措辞逐字对齐（field 无引号方言；`jsonRecord` 用 ``must be an object``）。

surface 元数据与 payload 语义复用 v0→v1 共享模块（上游 validateReleasedV2Artifact
imports v0→v1 的 assertReleasedSurfaceMetadata / assertReleasedPayloadSemantics）。
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
    safe_integer,
    unsupported,
)
from .payload_validation import assert_released_payload_semantics
from .relationships import assert_released_artifact_relationships
from .validate import assert_released_surface_metadata

__all__ = [
    "RELEASED_V2_RELATIONSHIP_EXTENSIONS",
    "assert_released_v2_artifact",
    "assert_released_v2_header",
    "assert_released_v2_physical_artifact",
    "restore_released_v2_artifact",
]

_HEADER_REQUIRED = ("version", "id", "createdAt", "isSeeded", "delegationDepth")
_HEADER_OPTIONAL = ("cwd", "parentSession", "origin", "agentPreset")
_EVENT_REQUIRED = ("type", "seq", "time", "data")
_SURFACE_TYPES = frozenset({"user/message", "assistant/message", "tool/result"})
_SURFACE_OPTIONAL = ("ignorable", "sourceEventSeqs", "surfaceOp")
_LOG_OPTIONAL = ("ignorable",)
_RELEASED_V2_EVENT_TYPE_SET = frozenset(_disp.RELEASED_V2_EVENT_DISPOSITIONS)

#: v2 关系状态机扩展（上游 validation.ts:32-35）。
RELEASED_V2_RELATIONSHIP_EXTENSIONS = {
    "stepEvents": frozenset({"assistant/attempt"}),
    "preservedSourceTitleRequestText": True,
}


def _is_absolute(path: str) -> bool:
    from pathlib import PurePosixPath, PureWindowsPath
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def _json_record(value: Any, label: str) -> dict:
    """非 null 非数组对象（上游 jsonRecord：``must be an object``）。"""
    if not isinstance(value, dict):
        raise fail(f"{label} must be an object")
    return value


def assert_released_v2_header(header: Any) -> None:
    """released v2 精确逻辑头（上游 assertReleasedV2Header）。

    cwd 单个消息覆盖非字符串与非绝对路径两种失败；parentSession/agentPreset 与
    origin 对 null 同样拒绝（``!== undefined`` 语义，null 是值不是缺省）。
    """
    record = _json_record(header, "format v2 header")
    exact_keys(record, _HEADER_REQUIRED, _HEADER_OPTIONAL, "format v2 header",
               member="field", quote=False, missing_first=True)
    if record.get("version") != 2:
        raise fail("expected format v2 header")
    if not isinstance(record.get("id"), str):
        raise fail("format v2 header id must be a string")
    count(record.get("createdAt"), "format v2 header createdAt")
    count(record.get("delegationDepth"), "format v2 header delegationDepth")
    if not isinstance(record.get("isSeeded"), bool):
        raise fail("format v2 header isSeeded must be boolean")
    if "cwd" in record \
            and not (isinstance(record.get("cwd"), str) and _is_absolute(record["cwd"])):
        raise fail("format v2 header cwd must be absolute")
    for key in ("parentSession", "agentPreset"):
        if key in record and not isinstance(record.get(key), str):
            raise fail(f"format v2 header {key} must be a string")
    if "origin" in record and record.get("origin") != "subagent":
        raise fail('format v2 header origin must be "subagent"')


def assert_released_v2_artifact(artifact: dict) -> None:
    """released v2 写出的精确逻辑镜像（env+payload+关系+cut）。"""
    _validate_released_v2_artifact(artifact, "target", _RELEASED_V2_EVENT_TYPE_SET)


def assert_released_v2_physical_artifact(artifact: dict) -> None:
    """v2 词表中立物理解码校验（不解释事件词表/payload）。"""
    _validate_released_v2_artifact(artifact, "physical")


def restore_released_v2_artifact(artifact: dict,
                                 known_event_types: Iterable[str]) -> dict:
    """以当前安装词表恢复 v2（env+cut+安装门；不跑 payload/关系/流）。"""
    _validate_released_v2_artifact(artifact, "current", frozenset(known_event_types))
    return artifact


def _validate_released_v2_artifact(artifact: dict, mode: str,
                                   known_event_types: frozenset | None = None) -> None:
    """v2 三维校验核心（上游 validateReleasedV2Artifact）。

    physical：只校验物理 header + 事件信封 + 继承切点；target：全量（payload +
    流复核 + surface 元数据 + 关系 + marker/cut 双向）；current：安装门（未知类型
    必须 ignorable，恢复只做信封）。
    """
    assert_released_v2_header(artifact["header"])
    cut = count(artifact.get("inherited_event_count"), "format v2 inherited event count")
    if cut > len(artifact["events"]):
        raise fail("format v2 inherited event count exceeds its events")
    if not artifact["header"].get("isSeeded") and cut != 0:
        raise fail("unseeded format v2 Session has inherited events")
    last_inherited_marker: int | None = None
    for index, event in enumerate(artifact["events"]):
        record = _json_record(event, f"format v2 event {index}")
        etype = record.get("type")
        if not isinstance(etype, str):
            raise fail(f"format v2 event {index} type must be a string")
        disposition = _disp.RELEASED_V2_EVENT_DISPOSITIONS.get(etype)
        installed = known_event_types is not None and etype in known_event_types
        ignorable_unknown = disposition is None and mode == "current" \
            and record.get("ignorable") is True
        if mode != "physical" and disposition is None and not installed \
                and not ignorable_unknown:
            raise unsupported(
                f"format v2 contains unknown event type {js_stringify(etype)} at seq {index}")
        if disposition is not None:
            surface = etype in _SURFACE_TYPES
        else:
            surface = False
        if mode == "physical" or disposition is None:
            optional = _SURFACE_OPTIONAL
        elif surface:
            optional = _SURFACE_OPTIONAL
        else:
            optional = _LOG_OPTIONAL
        exact_keys(record, _EVENT_REQUIRED, optional, f"format v2 event {index}",
                   member="field", quote=False, missing_first=True)
        seq_value = record.get("seq")
        if isinstance(seq_value, bool) or seq_value != index:
            raise fail(f"format v2 event {index} is not dense")
        safe_integer(record.get("time"), f"format v2 event {index} time")
        if "ignorable" in record and record.get("ignorable") is not True:
            raise fail(f"format v2 event {index} ignorable must be true when present")
        if mode == "target" and surface:
            assert_released_surface_metadata(record, index, etype, "forbid-assistant")
        if mode == "target" and disposition is not None:
            _assert_v2_payload(event, disposition)
        if etype == "session/end-seed":
            data = _json_record(record.get("data"), f"session/end-seed {index} data")
            if data.get("inherited") is True:
                last_inherited_marker = index
    if artifact["header"].get("isSeeded") and last_inherited_marker != cut:
        raise fail("format v2 seeded header disagrees with its last inherited end-seed marker")
    if not artifact["header"].get("isSeeded") and last_inherited_marker is not None:
        raise fail("format v2 unseeded Session contains an inherited end-seed marker")
    if mode == "target":
        assert_released_artifact_relationships(artifact, RELEASED_V2_RELATIONSHIP_EXTENSIONS)


def _assert_v2_payload(event: dict, disposition: dict) -> None:
    """v2 事件 payload：闭集 + opaque 快照 + assistant 内嵌流复核（上游 assertPayload）。

    内嵌流复核与上游一致：先逐 member 做 assistant/chunk payload 语义与 assembler 喂入
    （在 try 内，任何失败 → ``has an invalid embedded stream``），再对 assistant/message
    比对 content/usage/replayState 三事实。attempt 或空流只验证可还原性。
    """
    etype = event["type"]
    seq = event["seq"]
    data = _json_record(event.get("data"), f"{etype} {seq} data")
    exact_keys(data, tuple(disposition["required"]), tuple(disposition["optional"]),
               f"{etype} {seq} data", member="field", quote=False, missing_first=True)
    for key in disposition["opaque"]:
        if key in data:
            lossless_json(data[key], f"{etype} {seq} opaque {key}")
    if etype in ("assistant/attempt", "assistant/message"):
        turn = count(data.get("turn"), f"{etype} {seq} turn")
        step = count(data.get("step"), f"{etype} {seq} step")
        from ....llm.assistant_stream import expand_assistant_stream  # noqa: PLC0415
        from ....llm.protocol import BlockAssembler  # noqa: PLC0415
        assembler = BlockAssembler()
        try:
            timed = expand_assistant_stream(list(data.get("stream")))
            for member in timed:
                assert_released_payload_semantics({
                    "type": "assistant/chunk",
                    "seq": seq,
                    "time": member.time,
                    "data": {"turn": turn, "step": step, "chunk": member.chunk},
                }, 2)
                assembler.push(member.chunk)
        except Exception as error:  # noqa: BLE001
            raise fail(f"{etype} {seq} has an invalid embedded stream") from error
        if etype == "assistant/attempt":
            return
        assert_released_payload_semantics(event, 2)
        if timed:
            message = _json_record(data.get("message"),
                                   f"assistant/message {seq} message")
            content = assembler.interrupted_blocks() if data.get("interrupted") is True \
                else assembler.blocks()
            if not deep_equal(message.get("content"), content):
                raise fail(
                    f"assistant/message {seq} message content disagrees with its embedded stream")
            if not deep_equal(data.get("usage"), assembler.usage):
                raise fail(
                    f"assistant/message {seq} usage disagrees with its embedded stream")
            source = _json_record(message.get("source"),
                                  f"assistant/message {seq} source")
            if not deep_equal(source.get("replayState"), assembler.replay_state):
                raise fail(
                    f"assistant/message {seq} replay state disagrees with its embedded stream")
        return
    if etype == "session/end-seed":
        if "inherited" in data and data.get("inherited") is not True:
            raise fail(f"session/end-seed {seq} inherited must be true when present")
        return
    assert_released_payload_semantics(event, 2)