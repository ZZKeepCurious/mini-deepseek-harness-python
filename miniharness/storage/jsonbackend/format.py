"""磁盘 JSON unit 格式：文件恒为当前净状态，人类可读（pretty-print，插入键序）。

上游对照：packages/storage/storage-json/src/format.ts（serialize / parse /
serializeRecord / parseRecord）。

'single' 布局 = 带 unit 头的单文档；'per-record' = 目录下一个记录一文档
（`<table>/<key>.json` + `global.json`），一次写只重写一条记录。

per-record 契约：格式坏或版本戳不在接受集（当前版本 + compatible_versions）的
记录文档是 FOREIGN，读作缺位——一条坏/过期记录文件绝不搞垮整个 unit，未接受
版本戳弃该记录而非迁移之（整文档格式相反：恰一个文档，所以拒绝）。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..backend import KvUnitDescriptor
from ..error import StorageError

__all__ = [
    "UnitState",
    "serialize",
    "parse",
    "serialize_record",
    "parse_record",
]


@dataclass
class UnitState:
    """一个 unit 的权威内存态；文件是它的投影。global 在首次写前为 None。"""

    version: int
    global_: Any = None
    tables: dict[str, dict[str, Any]] = field(default_factory=dict)


def serialize(name: str, state: UnitState) -> str:
    """把 unit 态序列化为文件内容（pretty JSON，尾随换行）。"""
    document = {
        "unit": {"name": name, "version": state.version},
        "global": state.global_,
        "tables": state.tables,
    }
    return json.dumps(document, indent=2, ensure_ascii=False) + "\n"


def parse(text: str, descriptor: KvUnitDescriptor) -> UnitState:
    """把文件内容解析为 unit 态，校验形态与版本。

    版本戳与期望不符 → 'version-mismatch'；任何形态/头损坏 → 'malformed-medium'。
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError as error:
        raise StorageError(
            "malformed-medium",
            f"unit {descriptor.name!r}: file is not valid JSON",
            cause=error,
        ) from error
    if not isinstance(document, dict):
        raise StorageError(
            "malformed-medium", f"unit {descriptor.name!r}: file is not a JSON object")
    unit = document.get("unit")
    if not isinstance(unit, dict):
        raise StorageError(
            "malformed-medium", f"unit {descriptor.name!r}: missing or foreign unit header")
    if unit.get("name") != descriptor.name:
        raise StorageError(
            "malformed-medium", f"unit {descriptor.name!r}: missing or foreign unit header")
    stamped = unit.get("version")
    if not isinstance(stamped, (int, float)) or isinstance(stamped, bool):
        raise StorageError(
            "malformed-medium", f"unit {descriptor.name!r}: missing or foreign unit header")
    if int(stamped) != descriptor.version:
        raise StorageError(
            "version-mismatch",
            f"unit {descriptor.name!r}: stored version {int(stamped)} != expected "
            f"{descriptor.version}")
    tables_raw = document.get("tables")
    if not isinstance(tables_raw, dict):
        raise StorageError(
            "malformed-medium", f"unit {descriptor.name!r}: tables is not an object")
    state = UnitState(version=descriptor.version)
    for table in descriptor.tables:
        records = tables_raw.get(table)
        if records is None:
            state.tables[table] = {}
            continue
        if not isinstance(records, dict):
            raise StorageError(
                "malformed-medium",
                f"unit {descriptor.name!r}: table {table!r} is not an object")
        state.tables[table] = dict(records)
    state.global_ = document.get("global")
    return state


def serialize_record(version: int, value: Any) -> str:
    """序列化一条 per-record 文档：unit 版本戳 + 记录值（pretty，尾随换行）。"""
    return json.dumps({"version": version, "record": value}, indent=2, ensure_ascii=False) + "\n"


def parse_record(text: str, versions: tuple[int, ...]) -> Any:
    """解析一条 per-record 文档并校验版本戳。

    坏格式或未接受版本 → FOREIGN，读作缺位（返回 None）。文档被移到一边或
    直接弃读，绝不迁移。
    """
    try:
        document = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(document, dict):
        return None
    stamped = document.get("version")
    if not isinstance(stamped, (int, float)) or isinstance(stamped, bool):
        return None
    if int(stamped) not in versions:
        return None
    if "record" not in document:
        return _ABSENT
    return document.get("record")


class _Absent:
    def __repr__(self) -> str:  # pragma: no cover
        return "<absent>"


_ABSENT = _Absent()