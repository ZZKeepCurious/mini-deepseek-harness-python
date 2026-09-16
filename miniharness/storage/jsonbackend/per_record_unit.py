"""`per-record` 布局的一个已开 JSON unit：unit 是一个目录，目录即状态。

上游对照：packages/storage/storage-json/src/per-record-unit.ts（PerRecordJsonUnit +
openPerRecordUnit + loadPerRecordState + bootstrapLegacyUnit）。

每个记录一个文档于 `<dir>/<table>/<key>.json` + `global.json`。本 unit 不持任何
自身内存态：load_all 重读树，每次写是一次持久文件操作——domain 层拥有活内存表
（由 open 时 load_all 播种）并以其写链串行化写，所以本 unit 从不改内存、无需
回滚——失败的写只让文件与 domain 内存都不动。

per-record 契约：记录文档坏格式或版本戳不在接受集（当前版本 + compatible_versions）
读作缺位——一条坏/过期文件绝不搞垮整个 unit，未接受版本戳弃该记录而非迁移之。
记录 key 成为路径段，须路径安全 `[a-zA-Z0-9_-]+`（写时不安全 key 拒绝）。

Legacy bootstrap：新树无任何文档路径时，旧整文档文件 `<root>/<name>.json`
（pre-per-record 布局）播种 per-record 文档——仅当其盖的 unit 版本在接受集；
任何其它版本戳的 legacy 文件原样留、读作空 unit。任一新文档路径（含不可读/过期）
压制整 unit 的 bootstrap。legacy 文件永不改删。
"""
from __future__ import annotations

import asyncio
import os
import re
from datetime import datetime
from typing import Any

from ..backend import KvUnitDescriptor
from ..error import StorageError
from .atomic import write_atomic
from .format import UnitState, _ABSENT, parse_record, serialize_record

__all__ = ["open_per_record_unit", "PerRecordJsonUnit"]

#: key 成为路径段；该集各 OS 路径安全。
SAFE_KEY_RE = re.compile(r"^[a-zA-Z0-9_-]+$")


def open_per_record_unit(descriptor: KvUnitDescriptor, root: str, on_close) -> "PerRecordJsonUnit":
    """打开一个 `per-record` 布局 unit：目录 `<root>/<name>/`。懒加载——首次
    load_all 才触 medium，打开本身不碰。"""
    return PerRecordJsonUnit(descriptor, os.path.join(root, descriptor.name), on_close)


class PerRecordJsonUnit:
    """`per-record` 布局 unit。无状态设计：目录即 medium，domain 层拥有活内存，
    每方法是一次持久文件操作。写排序属于调用方（domain 写链），同 single 布局。"""

    def __init__(self, descriptor: KvUnitDescriptor, dir: str, on_close) -> None:
        self._descriptor = descriptor
        self._dir = dir
        self._on_close = on_close
        self._closed = False
        self._in_flight: set[asyncio.Future] = set()

    async def load_all(self) -> dict[str, Any]:
        self.assert_open()
        state = await _load_per_record_state(self._descriptor, self._dir)
        return {
            "tables": {table: dict(records) for table, records in state.tables.items()},
            "global": state.global_,
        }

    async def put_record(self, table: str, key: str, value: Any) -> None:
        self.assert_open()
        assert_safe_key(self._descriptor.name, key)
        await self._tracked(_write_document(
            os.path.join(self._table_dir(table), f"{key}.json"),
            self._descriptor.version, value))

    async def delete_record(self, table: str, key: str) -> None:
        self.assert_open()
        assert_safe_key(self._descriptor.name, key)
        path = os.path.join(self._table_dir(table), f"{key}.json")
        await self._tracked(_remove_force(path))

    async def backup_record(self, table: str, key: str) -> str:
        """把一条记录的文档挪到一边为 `<key>.json.bak.<YYYYMMDDHHmm>`。

        挪后的文件不再以 .json 结尾，此后读侧忽略它；字节留盘可检。同一分钟内
        同名备份重写前备份（新字节值得保留）。
        """
        self.assert_open()
        assert_safe_key(self._descriptor.name, key)
        path = os.path.join(self._table_dir(table), f"{key}.json")
        moved = f"{path}.bak.{_backup_stamp(datetime.now())}"

        async def move() -> None:
            await asyncio.to_thread(os.replace, path, moved)

        await self._tracked(asyncio.ensure_future(move()))
        return moved

    async def set_global(self, value: Any) -> None:
        self.assert_open()
        if not self._descriptor.has_global:
            raise RuntimeError(f"unit {self._descriptor.name!r} does not declare a global slot")
        await self._tracked(_write_document(
            os.path.join(self._dir, "global.json"), self._descriptor.version, value))

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            await asyncio.gather(*list(self._in_flight), return_exceptions=True)
            self._on_close()
            return
        await asyncio.gather(*list(self._in_flight), return_exceptions=True)

    def assert_open(self) -> None:
        if self._closed:
            raise StorageError("closed", f"unit {self._descriptor.name!r} is closed")

    def _table_dir(self, table: str) -> str:
        if table not in self._descriptor.tables:
            raise RuntimeError(
                f"unit {self._descriptor.name!r} does not declare table {table!r}")
        return os.path.join(self._dir, table)

    def _tracked(self, awaited: Any) -> asyncio.Future:
        write = awaited if isinstance(awaited, asyncio.Future) else asyncio.ensure_future(awaited)
        self._in_flight.add(write)
        write.add_done_callback(self._in_flight.discard)
        return write


async def _load_per_record_state(descriptor: KvUnitDescriptor, dir: str) -> UnitState:
    versions = _accepted_stamps(descriptor)
    state = UnitState(
        version=descriptor.version,
        tables={table: {} for table in descriptor.tables},
    )
    try:
        entries = os.listdir(dir)
    except FileNotFoundError:
        entries = None  # 缺目录 = 空 unit；legacy bootstrap 照跑（新升级形正是缺新树）
    has_new = False
    if entries is not None:
        for name in entries:
            path = os.path.join(dir, name)
            if os.path.isdir(path):
                records = state.tables.get(name)
                if records is not None:
                    has_new = await _load_table_records(records, versions, path) or has_new
                continue
            if name == "global.json" and descriptor.has_global:
                record = await _read_record(path, versions)
                if record is not _ABSENT:
                    state.global_ = record
                has_new = True
    if not has_new:
        await _bootstrap_legacy_unit(descriptor, dir, state)
    return state


def _accepted_stamps(descriptor: KvUnitDescriptor) -> tuple[int, ...]:
    return (descriptor.version, *descriptor.compatible_versions)


async def _load_table_records(records: dict[str, Any], versions: tuple[int, ...],
                              dir: str) -> bool:
    has_documents = False
    for fname in os.listdir(dir):
        if not fname.endswith(".json"):
            continue
        has_documents = True
        key = fname[: -len(".json")]
        if SAFE_KEY_RE.match(key) is None:
            continue
        record = await _read_record(os.path.join(dir, fname), versions)
        # 坏/过期/去记录戳 = FOREIGN 读作缺位（None 或 _ABSENT 皆跳过）。
        if record is not None and record is not _ABSENT:
            records[key] = record
    return has_documents


async def _read_record(path: str, versions: tuple[int, ...]) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except OSError:
        return _ABSENT
    return parse_record(text, versions)


async def _bootstrap_legacy_unit(descriptor: KvUnitDescriptor, dir: str, state: UnitState) -> None:
    """把空 per-record 树从旧整文档文件灌入 per-record 文档。

    旧文件仅当其 unit.name 相符且盖的版本在接受集时迁移；任何其它（缺失/异主/
    坏格式/版本外来）原样留，空树保持空。迁移的记录值原样搬——domain 层 schema
    判它们。
    """
    legacy_path = os.path.join(os.path.dirname(dir), f"{descriptor.name}.json")
    try:
        with open(legacy_path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        return
    except OSError:
        raise
    import json as _json
    try:
        document = _json.loads(text)
    except _json.JSONDecodeError:
        return  # 坏 legacy 文件：不是我们的解释或删除对象
    if not isinstance(document, dict):
        return
    unit = document.get("unit")
    if not isinstance(unit, dict) or unit.get("name") != descriptor.name:
        return
    stamped = unit.get("version")
    if not isinstance(stamped, (int, float)) or isinstance(stamped, bool):
        return
    if int(stamped) not in _accepted_stamps(descriptor):
        return
    tables = document.get("tables")
    if not isinstance(tables, dict):
        return
    for table, records in tables.items():
        target = state.tables.get(table)
        if target is None or not isinstance(records, dict):
            continue
        for key, value in records.items():
            path = os.path.join(dir, table, f"{key}.json")
            await asyncio.to_thread(os.makedirs, os.path.dirname(path), exist_ok=True)
            await asyncio.to_thread(_write_file, path,
                                    serialize_record(descriptor.version, value))
            target[key] = value


def _write_file(path: str, content: str) -> None:
    write_atomic(path, content)


async def _write_document(path: str, version: int, value: Any) -> None:
    await asyncio.to_thread(os.makedirs, os.path.dirname(path), exist_ok=True)
    await asyncio.to_thread(_write_file, path, serialize_record(version, value))


async def _remove_force(path: str) -> None:
    await asyncio.to_thread(_remove, path)


def _remove(path: str) -> None:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _backup_stamp(now: datetime) -> str:
    """本地时 `YYYYMMDDHHmm` 后缀（备份文档用）。"""
    return now.strftime("%Y%m%d%H%M")


def assert_safe_key(unit: str, key: str) -> None:
    if SAFE_KEY_RE.match(key) is None:
        raise ValueError(
            f"unit {unit!r}: per-record key {key!r} is not path-safe "
            f"(must match {SAFE_KEY_RE.pattern})")