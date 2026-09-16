"""`single` 布局的一个已开 JSON unit：整 unit 一个文档于 `<root>/<name>.json`。

上游对照：packages/storage/storage-json/src/single-unit.ts（SingleJsonUnit +
openSingleUnit）。

内存态权威；每次写原语改内存并原子重发整个文件。写不在此排队——按 backend
契约，写排序属于调用方（domain 层写链）；本 unit 只保证每次单次调用发布一个
完整持久文件。'per-record' 布局是 per-record-unit.py 的独立 unit 类。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from ..backend import KvUnitDescriptor
from ..error import StorageError
from .atomic import write_atomic
from .format import UnitState, parse, serialize

__all__ = ["open_single_unit", "SingleJsonUnit"]


def open_single_unit(descriptor: KvUnitDescriptor, root: str, on_close) -> "SingleJsonUnit":
    """打开（读取或懒建）一个 `single` 布局 unit：文件 `<root>/<name>.json`。

    缺文件 = 空 unit（物化拖延到首次写）；有文件则解析并校验版本。
    """
    path = os.path.join(root, f"{descriptor.name}.json")
    try:
        with open(path, "r", encoding="utf-8") as handle:
            text = handle.read()
    except FileNotFoundError:
        state = UnitState(
            version=descriptor.version,
            tables={table: {} for table in descriptor.tables},
        )
    else:
        state = parse(text, descriptor)
    return SingleJsonUnit(descriptor, path, state, on_close)


class SingleJsonUnit:
    """`single` 布局 unit：整 unit 一文档，原子重写发布。"""

    def __init__(self, descriptor: KvUnitDescriptor, path: str, state: UnitState, on_close) -> None:
        self._descriptor = descriptor
        self._path = path
        self._state = state
        self._on_close = on_close
        self._closed = False
        self._in_flight: set[asyncio.Future] = set()

    async def load_all(self) -> dict[str, Any]:
        self.assert_open()
        return {
            "tables": {table: dict(records) for table, records in self._state.tables.items()},
            "global": self._state.global_,
        }

    async def put_record(self, table: str, key: str, value: Any) -> None:
        self.assert_open()
        records = self._records(table)
        had_key = key in records
        previous = records.get(key)
        records[key] = value
        try:
            await self._publish()
        except Exception:
            if had_key:
                records[key] = previous
            else:
                records.pop(key, None)
            raise

    async def delete_record(self, table: str, key: str) -> None:
        self.assert_open()
        records = self._records(table)
        if key not in records:
            return
        previous = records.get(key)
        del records[key]
        try:
            await self._publish()
        except Exception:
            records[key] = previous
            raise

    async def set_global(self, value: Any) -> None:
        self.assert_open()
        if not self._descriptor.has_global:
            raise RuntimeError(f"unit {self._descriptor.name!r} does not declare a global slot")
        previous = self._state.global_
        self._state.global_ = value
        try:
            await self._publish()
        except Exception:
            self._state.global_ = previous
            raise

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
            # 幂等：重复 close 只排空，on_close 只跑一次。
            await asyncio.gather(*list(self._in_flight), return_exceptions=True)
            self._on_close()
            return
        await asyncio.gather(*list(self._in_flight), return_exceptions=True)

    def assert_open(self) -> None:
        if self._closed:
            raise StorageError("closed", f"unit {self._descriptor.name!r} is closed")

    def _records(self, table: str) -> dict[str, Any]:
        records = self._state.tables.get(table)
        if records is None:
            raise RuntimeError(
                f"unit {self._descriptor.name!r} does not declare table {table!r}")
        return records

    def _publish(self) -> asyncio.Future:
        write = asyncio.ensure_future(
            _run_atomic(self._path, serialize(self._descriptor.name, self._state)))
        self._in_flight.add(write)
        write.add_done_callback(self._in_flight.discard)
        return write


async def _run_atomic(path: str, content: str) -> None:
    await asyncio.to_thread(write_atomic, path, content)