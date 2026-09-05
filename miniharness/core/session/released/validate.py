"""released 制品 scoped 校验（Phase A 替代上游深度校验层的最小不变量面）。

上游 `assertReleasedV1Artifact`/`assertReleasedV2Artifact` 的完整面 = 51 类型逐字段
payload 语义（1028 行）+ 跨事件关系状态机（475 行）——Phase B 候选。本模块实现迁移
与读路径**必需**的不变量：header 闭集、信封坐标、封闭词表策略、surface 元数据、
已知类型的 disposition 键集、chunk 形状、end-seed 形状。病态输入的最终防线是
「迁移产物过现行 v2 restore 全量校验」（Session seed 边界 + surface/provenance +
`_assert_current_assistant_stream`）。
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
    safe_integer,
)
from .codec import LEGACY_EVENT_TYPES

__all__ = [
    "assert_released_surface_metadata",
    "assert_scoped_v1_artifact",
    "scoped_v1_source_check",
]

_SURFACE_TYPES = frozenset({"user/message", "assistant/message", "tool/result"})

#: chunk 七变体的成员闭集（v0v1/payload-validation.ts:674-712 的形状半边）。
_CHUNK_SHAPES: dict[str, tuple[tuple[str, ...], tuple[str, ...]]] = {
    "block-start": (("index", "blockType"), ()),
    "text-delta": (("index", "text"), ()),
    "reasoning-delta": (("index", "text"), ()),
    "tool-call-delta": (("index", "id", "argumentsDelta"), ("name",)),
    "block-end": (("index", "block"), ()),
    "usage": (("usage",), ()),
    "finish": (("reason",), ("replayState",)),
}


def assert_released_surface_metadata(event: dict, index: int, version: int,
                                     mode: str = "allow-empty-assistant") -> None:
    """surface 元数据（上游 assertReleasedSurfaceMetadata 的规则半边）：
    sourceEventSeqs 唯一且更早、非空除非 assistant/message；forbid-assistant 模式下
    assistant/message 带 provenance 即拒；surfaceOp 为 append 或 replace 记录。"""
    etype = event["type"]
    seq = event["seq"]
    sources = event.get("sourceEventSeqs")
    if sources is not None:
        if mode == "forbid-assistant" and etype == "assistant/message":
            raise fail(f"assistant/message {index} retains obsolete chunk provenance")
        if not isinstance(sources, list):
            raise fail(f"{etype} {index} sourceEventSeqs must be an array")
        seen: set[int] = set()
        for member in sources:
            if isinstance(member, bool) or not isinstance(member, int) \
                    or not 0 <= member < seq:
                raise fail(f"{etype} {index} sourceEventSeqs must be unique earlier seqs")
            if member in seen:
                raise fail(f"{etype} {index} sourceEventSeqs must be unique earlier seqs")
            seen.add(member)
        if len(sources) == 0 and etype != "assistant/message":
            raise fail(f"{etype} {index} sourceEventSeqs must be non-empty")
    surface_op = event.get("surfaceOp")
    if surface_op is None:
        return
    if surface_op == "append":
        return
    if is_json_object(surface_op):
        exact_keys(surface_op, ("op", "start", "end"), (), f"{etype} {index} surfaceOp")
        if surface_op.get("op") != "replace":
            raise fail(f"{etype} {index} has an invalid surface replacement")
        for key in ("start", "end"):
            value = surface_op.get(key)
            safe_integer(value, f"{etype} {index} surfaceOp {key}")
            if not 0 <= value < seq:
                raise fail(f"{etype} {index} has an invalid surface replacement")
        return
    raise fail(f"{etype} {index} has an invalid surface replacement")


def _assert_data_disposition(event: dict, index: int, table: dict[str, dict],
                             version: int) -> None:
    """已知类型的 data 顶层成员闭集（member 措辞，v0v1 方言）。"""
    etype = event["type"]
    disposition = table.get(etype)
    if disposition is None:
        if event.get("ignorable") is not True:
            raise fail(
                f'format v{version} contains unknown event type "{etype}" at seq {index}')
        return
    label = f"{etype} {index} payload"
    exact_keys(event["data"], tuple(disposition["required"]),
               tuple(disposition["optional"]), label)
    for member in disposition["opaque"]:
        if member in event["data"]:
            from .helpers import lossless_json
            lossless_json(event["data"][member], f"{label} {member}")


def _assert_chunk_shape(chunk: Any, label: str) -> None:
    """chunk 七变体形状半边（stream 归并/attempt 语义的最低输入要求）。"""
    if not is_json_object(chunk):
        raise fail(f"{label} chunk must be a JSON object")
    ctype = chunk.get("type")
    shape = _CHUNK_SHAPES.get(ctype) if isinstance(ctype, str) else None
    if shape is None:
        raise fail(f"{label} has unknown stream chunk type")
    required, optional = shape
    exact_keys(chunk, ("type",) + tuple(required), tuple(optional),
               f"{label} {ctype} chunk", member="field")
    if "index" in chunk:
        count(chunk.get("index"), f"{label} chunk index")
    if ctype in ("text-delta", "reasoning-delta") and not isinstance(chunk.get("text"), str):
        raise fail(f"{label} {ctype} text must be a string")
    if ctype == "tool-call-delta":
        if not isinstance(chunk.get("id"), str):
            raise fail(f"{label} tool-call-delta id must be a string")
        if "name" in chunk and not isinstance(chunk.get("name"), str):
            raise fail(f"{label} tool-call-delta name must be a string")
        if not isinstance(chunk.get("argumentsDelta"), str):
            raise fail(f"{label} tool-call-delta argumentsDelta must be a string")


def _assert_assistant_stream(stream: Any, label: str) -> None:
    """内嵌 stream 形状（v2 四变体记录——复用现行 expand 校验）。"""
    from ....llm.assistant_stream import expand_assistant_stream
    if not isinstance(stream, (list, tuple)):
        raise fail(f"{label} stream must be an array")
    try:
        expand_assistant_stream(list(stream))
    except Exception as error:  # noqa: BLE001
        raise SessionFormatError(f"{label} has an invalid embedded stream: {error}") from error


def _assert_essential_payload(event: dict, index: int, version: int) -> None:
    """已知类型的关键形状半边（深度语义 Phase B）：assistant 族 + end-seed。"""
    etype = event["type"]
    data = event["data"]
    label = f"{etype} {index}"
    if etype == "assistant/chunk":
        if not isinstance(data.get("turn"), int) or isinstance(data.get("turn"), bool):
            raise fail(f"{label} turn must be a number")
        if not isinstance(data.get("step"), int) or isinstance(data.get("step"), bool):
            raise fail(f"{label} step must be a number")
        _assert_chunk_shape(data.get("chunk"), label)
    elif etype == "assistant/message":
        count(data.get("turn"), f"{label} turn")
        count(data.get("step"), f"{label} step")
        message = data.get("message")
        if not is_json_object(message):
            raise fail(f"{label} message must be a JSON object")
        if not isinstance(message.get("id"), str) or not message["id"]:
            raise fail(f"{label} message id must be a non-empty string")
        if message.get("role") != "assistant":
            raise fail(f"{label} message role must be assistant")
    elif etype == "assistant/attempt":
        count(data.get("turn"), f"{label} turn")
        count(data.get("step"), f"{label} step")
        _assert_assistant_stream(data.get("stream"), label)
    elif etype == "session/end-seed":
        inherited = data.get("inherited") if isinstance(data, dict) else None
        if inherited is not None and inherited is not True:
            raise fail(f"{label} inherited must be true when present")


def assert_scoped_v1_artifact(artifact: dict, *, allow_empty_assistant: bool = True,
                              forbid_assistant_provenance: bool = False,
                              require_known_types: bool = True) -> None:
    """released v1/v2 制品 scoped 校验：header + 坐标 + 词表策略 + surface 元数据 +
    disposition 键集 + 关键形状。"""
    header = artifact["header"]
    version = header["version"]
    from .codec import _assert_logical_header  # noqa: PLC0415 - 避免环
    _assert_logical_header(header, version, f"released v{version} Session header")
    inherited = count(artifact.get("inherited_event_count"), "Session inheritedEventCount")
    events = artifact["events"]
    if inherited > len(events):
        raise fail("Session inheritedEventCount exceeds its event count")
    if not header.get("isSeeded") and inherited != 0:
        raise fail("unseeded Session inheritedEventCount must be 0")
    table = _disp.RELEASED_V0_EVENT_DISPOSITIONS if version == 1 \
        else _disp.RELEASED_V2_EVENT_DISPOSITIONS
    for index, event in enumerate(events):
        label = f"{event.get('type')} {index}"
        exact_keys(event, ("type", "seq", "time", "data"),
                   ("ignorable", "sourceEventSeqs", "surfaceOp"), label)
        if event["seq"] != index:
            raise fail(f"{label} has non-dense seq")
        safe_integer(event.get("time"), f"{label} time")
        if event.get("ignorable") is not None and event.get("ignorable") is not True:
            raise fail(f"{label} ignorable must be true when present")
        etype = event["type"]
        if etype not in table:
            if require_known_types and event.get("ignorable") is not True:
                raise fail(
                    f'format v{version} contains unknown event type "{etype}" at seq {index}')
        mode = "forbid-assistant" if forbid_assistant_provenance else "allow-empty-assistant"
        assert_released_surface_metadata(event, index, version, mode)
        if etype in table:
            _assert_data_disposition(event, index, table, version)
        _assert_essential_payload(event, index, version)
    if version == 2:
        # marker/cut 双向一致性（上游 codec deriveInheritedEventCount + validation）
        last_marker = None
        for event in events:
            if event["type"] == "session/end-seed" \
                    and event["data"].get("inherited") is True:
                last_marker = event["seq"]
        if header.get("isSeeded") and last_marker != inherited:
            raise fail(
                "released v2 seeded header disagrees with its last inherited end-seed marker")
        if not header.get("isSeeded") and last_marker is not None:
            raise fail("released v2 unseeded Session carries an inherited end-seed marker")


def scoped_v1_source_check(artifact: dict) -> None:
    """v1→v2 迁移的源侧 scoped 校验入口（allow-empty-assistant；词表策略由
    migrate 的封闭清单检查承担）。"""
    assert_scoped_v1_artifact(artifact)
