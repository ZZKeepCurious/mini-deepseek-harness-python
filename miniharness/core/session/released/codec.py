"""released v0/v1 物理 codec（上游 session-format-v0-to-v1/src/codec.ts 共享工厂）。

两代出自同一工厂 `createReleasedCodec(version)`，唯一值差异是 `version`；源侧校验器
不同（v0 = 冻结 51 词表 + legacy 白名单；v1 = 词表中立）。物理行三种：普通事件行 /
打包行（三 tag）/ 带 provenance 的行（区间编码混合数组）。`MIN_RUN=3` 是写侧规则，
读侧对任何合法打包行都展开。

可恢复扫描（`recoverable=True`）：首 issue 保留（`issue ??=`）、坏行跳过、issue 后
只放行可解码且非 `turn/end` 的行（见 turn/end 即抛 issue——「终结帧证明故障区已
提交」）、seq gap 回滚该行已展开事件。
"""
from __future__ import annotations

from typing import Any, Callable

from .helpers import (
    SessionFormatError,
    count,
    exact_keys,
    fail,
    is_json_object,
    safe_integer,
)
from .validate import (
    assert_released_session_format_header,
    assert_released_v0_source_artifact,
    assert_released_v1_physical_artifact,
)
from ..seq_ranges import decode_seq_ranges

__all__ = [
    "RELEASED_V0_CODEC",
    "RELEASED_V1_CODEC",
    "PackedRowError",
    "create_released_codec",
    "decode_released_header",
]

_PACKED_TAGS = frozenset({"text-chunks", "reasoning-chunks", "tool-call-chunks"})
_EVENT_REQUIRED = ("type", "seq", "time", "data")
_EVENT_OPTIONAL = ("ignorable", "sourceEventSeqs", "surfaceOp")
_PHYSICAL_HEADER_REQUIRED = ("type", "version", "id", "createdAt", "delegationDepth")
_PHYSICAL_HEADER_OPTIONAL = ("cwd", "parentSession", "seedLength", "origin", "agentPreset")


class PackedRowError(SessionFormatError):
    """打包行/provenance 行形状非法（解码期 fail-closed）。"""


def decode_released_header(header: Any) -> int:
    """只读版本判别（generation 快速检查用）：非对象/版本非法即拒。"""
    if not is_json_object(header):
        raise fail("corrupt session log: header is not a JSON object")
    return count(header.get("version"), "session log header version")


def _decode_physical_header(value: Any, version: int) -> dict:
    """物理头 → 逻辑头（`seedLength → isSeeded`，`seedLength:0` = seeded 零切割）。"""
    label = f"released v{version} physical Session header"
    exact_keys(value, _PHYSICAL_HEADER_REQUIRED, _PHYSICAL_HEADER_OPTIONAL, label)
    if value.get("type") != "session":
        raise fail(f"expected released v{version} physical Session header")
    if value.get("version") != version or isinstance(value.get("version"), bool):
        raise fail(f"expected released v{version} physical Session header")
    if not isinstance(value.get("id"), str):
        raise fail(f"{label} id must be a string")
    count(value.get("createdAt"), f"{label} createdAt")
    count(value.get("delegationDepth"), f"{label} delegationDepth")
    seed_length = value.get("seedLength")
    inherited = 0
    is_seeded = False
    if seed_length is not None:
        inherited = count(seed_length, f"{label} seedLength")
        is_seeded = True
    logical: dict[str, Any] = {
        "version": version,
        "id": value["id"],
        "createdAt": value["createdAt"],
        "isSeeded": is_seeded,
        "delegationDepth": value["delegationDepth"],
    }
    for key in ("cwd", "parentSession", "origin", "agentPreset"):
        if key in value:
            logical[key] = value[key]
    for key in ("cwd", "parentSession", "agentPreset"):
        if key in logical and not isinstance(logical[key], str):
            raise fail(f"{label} {key} must be a string")
    if "origin" in logical and logical["origin"] != "subagent":
        raise fail(f'{label} origin must be "subagent"')
    assert_released_session_format_header(logical, version)
    return {"header": logical, "inherited_event_count": inherited}


def _expand_packed_row(row: dict, row_index: int) -> list[dict]:
    """打包行 → assistant/chunk 事件序列（上游 expandPackedRow）。"""
    tag = row.get("type")
    label = f"released packed row {row_index + 1} ({tag})"
    exact_keys(row, ("type", "seq0", "time0", "data"), (), label)
    seq0 = count(row.get("seq0"), f"{label} seq0")
    time0 = safe_integer(row.get("time0"), f"{label} time0")
    data = row.get("data")
    if tag == "tool-call-chunks":
        exact_keys(data, ("turn", "step", "index", "id", "dt", "args"), ("name",), f"{label} data")
    else:
        exact_keys(data, ("turn", "step", "index", "dt", "texts"), (), f"{label} data")
    turn = data.get("turn")
    step = data.get("step")
    index = data.get("index")
    for name, coordinate in (("turn", turn), ("step", step), ("index", index)):
        # 上游只查 number；mini 读端更严（safe int，登记沿用既有口径）
        if not isinstance(coordinate, int) or isinstance(coordinate, bool):
            raise fail(f"{label} {name} must be a number")
    payload_key = "args" if tag == "tool-call-chunks" else "texts"
    payload = data.get(payload_key)
    if not isinstance(payload, list) or len(payload) == 0 or \
            any(not isinstance(member, str) for member in payload):
        raise fail(f"{label} {payload_key} must be a non-empty array of strings")
    dt = data.get("dt")
    if not isinstance(dt, list) or len(dt) != len(payload) - 1:
        raise fail(f"{label} dt must have one gap per payload gap")
    gaps = [safe_integer(gap, f"{label} dt[{i}]") for i, gap in enumerate(dt)]
    if tag == "tool-call-chunks":
        if not isinstance(data.get("id"), str):
            raise fail(f"{label} tool id must be a string")
        if "name" in data and not isinstance(data.get("name"), str):
            raise fail(f"{label} tool name must be a string")
    events: list[dict] = []
    time = time0
    for offset, text in enumerate(payload):
        if offset > 0:
            time = safe_integer(time + gaps[offset - 1], f"{label} time")
        if tag == "tool-call-chunks":
            chunk: dict[str, Any] = {
                "type": "tool-call-delta", "index": index, "id": data["id"],
                "argumentsDelta": text,
            }
            if "name" in data:
                chunk["name"] = data["name"]
        else:
            chunk_type = "text-delta" if tag == "text-chunks" else "reasoning-delta"
            chunk = {"type": chunk_type, "index": index, "text": text}
        events.append({
            "type": "assistant/chunk",
            "seq": count(seq0 + offset, f"{label} seq"),
            "time": time,
            "data": {"turn": turn, "step": step, "chunk": chunk},
        })
    return events


def _decode_provenance(value: Any, event_seq: int, label: str) -> list[int]:
    """存储态区间编码 → 平铺展开（上游 decodeSeqRanges：总数 ≤ 事件 seq、
    有区间时整体严格递增）。"""
    if value is None:
        return []
    if not isinstance(value, list):
        raise fail(f"{label} sourceEventSeqs must be an array")
    try:
        return decode_seq_ranges(value, max_entries=event_seq if event_seq > 0 else 0)
    except ValueError as error:
        raise PackedRowError(str(error)) from error


def _decode_event_row(row: Any, row_index: int, version: int) -> dict:
    label = f"released v{version} row {row_index + 1}"
    exact_keys(row, _EVENT_REQUIRED, _EVENT_OPTIONAL, label)
    seq = count(row.get("seq"), f"{label} seq")
    safe_integer(row.get("time"), f"{label} time")
    ignorable = row.get("ignorable")
    if ignorable is not None and ignorable is not True:
        raise fail(f"{label} ignorable must be true when present")
    event: dict[str, Any] = {
        "type": row["type"],
        "seq": seq,
        "time": row["time"],
        "data": row["data"],
    }
    if ignorable is True:
        event["ignorable"] = True
    if "sourceEventSeqs" in row:
        event["sourceEventSeqs"] = _decode_provenance(
            row["sourceEventSeqs"], seq, label)
    if "surfaceOp" in row:
        event["surfaceOp"] = row["surfaceOp"]
    if not isinstance(event["type"], str) or not event["type"]:
        raise fail(f"{label} type must be a non-empty string")
    return event


def _decode_row(row: Any, row_index: int, version: int) -> list[dict]:
    if is_json_object(row) and row.get("type") in _PACKED_TAGS:
        return _expand_packed_row(row, row_index)
    return [_decode_event_row(row, row_index, version)]


def _scan_rows(rows: list[Any], version: int, recoverable: bool) -> list[dict]:
    """逐行解码（上游 scanRows）：严格模式首错即抛；可恢复模式保留好前缀。"""
    events: list[dict] = []
    issue: Exception | None = None
    for row_index, row in enumerate(rows):
        try:
            decoded = _decode_row(row, row_index, version)
        except Exception as error:  # noqa: BLE001 - 行级错误统一进入 issue 语义
            if not recoverable:
                if isinstance(error, SessionFormatError):
                    raise
                raise PackedRowError(
                    f"released v{version} row {row_index + 1} is malformed: {error}"
                ) from error
            issue = issue if issue is not None else error
            continue
        expected_seq = len(events)
        first_seq = decoded[0]["seq"] if decoded else expected_seq
        if first_seq != expected_seq:
            gap_error = fail(
                f"released v{version} row {row_index + 1} has seq gap "
                f"(expected {expected_seq}, got {first_seq})")
            if not recoverable:
                raise gap_error
            # 回滚该行已展开事件并记 issue
            del events[expected_seq:]
            issue = issue if issue is not None else gap_error
            continue
        if issue is not None:
            # 故障区已提交的证明：终结帧必须不可达
            if any(event["type"] == "turn/end" for event in decoded):
                raise issue
            continue
        events.extend(decoded)
    return events


def create_released_codec(version: int,
                          source_validator: Callable[[dict], None] | None = None) -> dict:
    """v0/v1 共享 codec 工厂：decode_header / decode_artifact / decode_recoverable_artifact。

    制品级校验委托真实校验器：v0 源冻结（51 词表 + legacy 白名单）、v1 词表中立
    （上游 assertReleasedV0SourceArtifact / assertReleasedV1PhysicalArtifact 逐字语义）。
    """
    validator = source_validator or (
        assert_released_v0_source_artifact if version == 0 else assert_released_v1_physical_artifact
    )

    def decode_header(header_value: Any) -> dict:
        return _decode_physical_header(header_value, version)["header"]

    def decode_artifact(header_value: Any, row_values: list[Any]) -> dict:
        physical = _decode_physical_header(header_value, version)
        events = _scan_rows(row_values, version, recoverable=False)
        artifact = {"header": physical["header"],
                    "inherited_event_count": physical["inherited_event_count"],
                    "events": events}
        validator(artifact)
        return artifact

    def decode_recoverable_artifact(header_value: Any, row_values: list[Any]) -> dict:
        physical = _decode_physical_header(header_value, version)
        events = _scan_rows(row_values, version, recoverable=True)
        artifact = {"header": physical["header"],
                    "inherited_event_count": physical["inherited_event_count"],
                    "events": events}
        validator(artifact)
        return artifact

    return {
        "version": version,
        "decode_header": decode_header,
        "decode_artifact": decode_artifact,
        "decode_recoverable_artifact": decode_recoverable_artifact,
    }


RELEASED_V0_CODEC = create_released_codec(0, assert_released_v0_source_artifact)
RELEASED_V1_CODEC = create_released_codec(1, assert_released_v1_physical_artifact)
