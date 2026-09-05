"""released v0/v1 制品校验（上游 session-format-v0-to-v1/src/validation.ts 逐字移植）。

v0 源冻结 → legacy 归一化 → v1 精确镜像的完整深度校验面：header 闭集、artifact 坐标
（封闭词表 + legacy 白名单 + surface 元数据）、51 类型逐字段 payload 语义
（payload_validation.py）与跨事件关系状态机（relationships.py）。消息措辞逐字对齐。
"""
from __future__ import annotations

from typing import Any

from . import dispositions as _disp
from .helpers import (
    SessionFormatError,
    count,
    exact_keys,
    fail,
    is_json_object,
    js_stringify,
    lossless_json,
    released_v0_record,
    safe_integer,
    unsupported,
)
from .payload_validation import assert_released_payload_semantics
from .relationships import assert_released_artifact_relationships

__all__ = [
    "assert_released_event_payload",
    "assert_released_session_format_header",
    "assert_released_surface_metadata",
    "assert_released_v1_header",
    "assert_released_v1_artifact",
    "assert_released_v1_physical_artifact",
    "assert_released_v0_source_artifact",
    "assert_normalized_released_v0_artifact",
    "restore_released_v1_artifact",
]

_HEADER_REQUIRED = ("version", "id", "createdAt", "isSeeded", "delegationDepth")
_HEADER_OPTIONAL = ("cwd", "parentSession", "origin", "agentPreset")
_EVENT_REQUIRED = ("type", "seq", "time", "data")
_SURFACE_EVENT_TYPES = frozenset({"user/message", "assistant/message", "tool/result"})
_SURFACE_OPTIONAL = ("ignorable", "sourceEventSeqs", "surfaceOp")
_LOG_OPTIONAL = ("ignorable",)
_LEGACY_SOURCE_TYPES = frozenset({"steering/message", "request/header-delta", "mode/set"})
_RELEASED_V0_EVENT_TYPE_SET = frozenset(_disp.RELEASED_V0_EVENT_DISPOSITIONS)


def _is_absolute(path: str) -> bool:
    from pathlib import PurePosixPath, PureWindowsPath
    return PurePosixPath(path).is_absolute() or PureWindowsPath(path).is_absolute()


def assert_released_session_format_header(header: Any, version: int) -> None:
    """v0/v1 共享逻辑头（上游 assertReleasedSessionFormatHeader）。"""
    record = released_v0_record(header, f"format v{version} header")
    exact_keys(record, _HEADER_REQUIRED, _HEADER_OPTIONAL, f"format v{version} header")
    if isinstance(record.get("version"), bool) or record.get("version") != version:
        raise fail(f"expected format v{version} header")
    if not isinstance(record.get("id"), str):
        raise fail(f"format v{version} header id must be a string")
    count(record.get("createdAt"), f"format v{version} header createdAt")
    if not isinstance(record.get("isSeeded"), bool):
        raise fail(f"format v{version} header isSeeded must be a boolean")
    count(record.get("delegationDepth"), f"format v{version} header delegationDepth")
    for key in ("cwd", "parentSession", "agentPreset"):
        if record.get(key) is not None and not isinstance(record.get(key), str):
            raise fail(f"format v{version} header {key} must be a string")
    if isinstance(record.get("cwd"), str) and not _is_absolute(record.get("cwd")):
        raise fail(f"format v{version} header cwd must be absolute")
    if record.get("origin") is not None and record.get("origin") != "subagent":
        raise fail(f'format v{version} header origin must be "subagent"')


def assert_released_v1_header(header: Any) -> None:
    """released v1 精确逻辑头。"""
    assert_released_session_format_header(header, 1)


def assert_released_v0_source_artifact(artifact: dict) -> None:
    """v0 归一化前源校验（header + 坐标 + legacy 白名单，不跑 payload/关系）。"""
    assert_released_session_format_header(artifact["header"], 0)
    _assert_artifact_coordinates(artifact, True, _RELEASED_V0_EVENT_TYPE_SET)


def assert_normalized_released_v0_artifact(artifact: dict) -> None:
    """归一化 v0 制品（payload 全量 + 关系，header 仍为 v0）。"""
    assert_released_session_format_header(artifact["header"], 0)
    _assert_artifact_coordinates(artifact, False, _RELEASED_V0_EVENT_TYPE_SET)
    for event in artifact["events"]:
        assert_released_event_payload(event, 0)
    assert_released_artifact_relationships(artifact)


def assert_released_v1_artifact(artifact: dict) -> None:
    """released v1 写出的精确逻辑镜像。"""
    assert_released_v1_header(artifact["header"])
    _assert_artifact_coordinates(artifact, False, _RELEASED_V0_EVENT_TYPE_SET)
    for event in artifact["events"]:
        if _disp.RELEASED_V0_EVENT_DISPOSITIONS.get(event.get("type")) is not None:
            assert_released_event_payload(event, 1)
    assert_released_artifact_relationships(artifact)


def restore_released_v1_artifact(artifact: dict, known_event_types: frozenset) -> dict:
    """以当前安装词表恢复 v1（不冻结 payload 增扩；不跑 payload/关系）。"""
    assert_released_v1_header(artifact["header"])
    _assert_artifact_coordinates(artifact, False, known_event_types)
    return artifact


def assert_released_v1_physical_artifact(artifact: dict) -> None:
    """v1 词表中立物理解码校验（不解释事件词表）。"""
    assert_released_v1_header(artifact["header"])
    _assert_artifact_coordinates(artifact, False, None, True)


def _assert_artifact_coordinates(artifact: dict, allow_legacy_steering: bool,
                                 known_event_types: frozenset | None = None,
                                 vocabulary_neutral: bool = False) -> None:
    """artifact 坐标（上游 assertArtifactCoordinates）。"""
    inherited_event_count = count(artifact.get("inherited_event_count"),
                                  "Session inheritedEventCount")
    if inherited_event_count > len(artifact["events"]):
        raise fail("Session inheritedEventCount exceeds its event count")
    if not artifact["header"].get("isSeeded") and inherited_event_count != 0:
        raise fail("unseeded Session inheritedEventCount must be 0")
    for index, event in enumerate(artifact["events"]):
        record = released_v0_record(event, f"Session event {index}")
        etype = record.get("type")
        if not isinstance(etype, str):
            raise fail(f"Session event {index} type must be a string")
        disposition = _disp.RELEASED_V0_EVENT_DISPOSITIONS.get(etype)
        legacy = allow_legacy_steering and etype in _LEGACY_SOURCE_TYPES
        current_known = known_event_types is not None and etype in known_event_types
        ignorable_current = not allow_legacy_steering and not current_known \
            and record.get("ignorable") is True
        if not current_known and not legacy and not ignorable_current and not vocabulary_neutral:
            if allow_legacy_steering:
                raise unsupported(
                    f"format v0 contains unknown historical event type {js_stringify(etype)} "
                    f"at seq {index}; migration refuses unknown historical events even when ignorable")
            raise unsupported(
                f"format v1 contains unknown required event type {js_stringify(etype)} at seq {index}")
        frozen_envelope = not vocabulary_neutral and known_event_types is _RELEASED_V0_EVENT_TYPE_SET
        if disposition is not None:
            surface = etype in _SURFACE_EVENT_TYPES
        else:
            surface = etype == "steering/message"
        if frozen_envelope:
            optional = _SURFACE_OPTIONAL if surface else _LOG_OPTIONAL
        else:
            optional = _SURFACE_OPTIONAL
        exact_keys(record, _EVENT_REQUIRED, optional, f"Session event {index}")
        seq_value = record.get("seq")
        if isinstance(seq_value, bool) or seq_value != index:
            raise fail(f"Session event {index} has non-dense seq {js_stringify(seq_value)}")
        safe_integer(record.get("time"), f"Session event {index} time")
        if record.get("ignorable") is not None and record.get("ignorable") is not True:
            raise fail(f"Session event {index} ignorable must be true when present")
        if frozen_envelope and surface:
            assert_released_surface_metadata(record, index, etype, "allow-empty-assistant")


def assert_released_surface_metadata(record: dict, seq: int, type_: str,
                                     assistant_sources: str) -> None:
    """surface 引用元数据（上游 assertReleasedSurfaceMetadata）。"""
    sources = record.get("sourceEventSeqs")
    if type_ == "assistant/message" and sources is not None \
            and assistant_sources == "forbid-assistant":
        raise fail(f"assistant/message {seq} retains obsolete chunk provenance")
    if sources is not None:
        if not isinstance(sources, list):
            raise fail(f"{type_} {seq} sourceEventSeqs must be an array")
        seen: set[int] = set()
        for source in sources:
            current = count(source, f"{type_} {seq} sourceEventSeqs member")
            if current >= seq or current in seen:
                raise fail(f"{type_} {seq} sourceEventSeqs must be unique earlier seqs")
            seen.add(current)
        if len(sources) == 0 \
                and (type_ != "assistant/message" or assistant_sources == "forbid-assistant"):
            raise fail(f"{type_} {seq} sourceEventSeqs must be non-empty")
    operation = record.get("surfaceOp")
    if operation is None or operation == "append":
        return
    replacement = released_v0_record(operation, f"{type_} {seq} surfaceOp")
    exact_keys(replacement, ("op", "start", "end"), (), f"{type_} {seq} surfaceOp")
    if replacement.get("op") != "replace":
        raise fail(f"{type_} {seq} surfaceOp must replace")
    start = count(replacement.get("start"), f"{type_} {seq} surface start")
    end = count(replacement.get("end"), f"{type_} {seq} surface end")
    if start >= seq or end >= seq:
        raise fail(f"{type_} {seq} has an invalid surface replacement")


def assert_released_event_payload(event: dict, version: int) -> None:
    """一个已知事件的 payload 全量语义（上游 assertReleasedEventPayload）。"""
    disposition = _disp.RELEASED_V0_EVENT_DISPOSITIONS.get(event.get("type"))
    if disposition is None:
        raise unsupported(
            f"format v0 contains unknown event type {js_stringify(event.get('type'))} "
            f"at seq {event.get('seq')}")
    data = released_v0_record(event.get("data"),
                              f"{event.get('type')} {event.get('seq')} data")
    if event.get("type") == "subagent/descriptor" and data.get("version") != 3:
        descriptor_version = count(data.get("version"),
                                   f"{event.get('type')} {event.get('seq')} version")
        if version == 0:
            raise unsupported(
                f"{event.get('type')} {event.get('seq')} uses unsupported descriptor version "
                f"{descriptor_version}")
        return
    if version == 1 and event.get("type") == "session-log-deepseek/delivery-accepted":
        version_optional = [*disposition["optional"], "sessionFormatVersion"]
    else:
        version_optional = disposition["optional"]
    exact_keys(data, tuple(disposition["required"]), tuple(version_optional),
               f"{event.get('type')} {event.get('seq')} data")
    for key in disposition["opaque"]:
        if key in data:
            lossless_json(data[key],
                          f"{event.get('type')} {event.get('seq')} opaque {key}")
    assert_released_payload_semantics(event, version)