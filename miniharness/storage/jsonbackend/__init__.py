"""JSON storage backend：一个 unit 一个人可读文档于配置根下，原子重写发布。

上游对照：packages/storage/storage-json/src/index.ts（JsonStorageBackend +
validateDescriptor + apply）。

注册为 storage hub 的 backend `json`。`root` 没有缺省——回退到进程 cwd 会把
unit 文件撒得到处都是；装配方显式声明位置（上游配置 `root` required）。
"""
from __future__ import annotations

import asyncio
import os
from typing import Any

from ...core.scope import Context
from ..backend import KvUnitDescriptor, unit_name_ok
from ..error import StorageError
from ..hub import storage_backend_service_key
from .single_unit import open_single_unit
from .per_record_unit import open_per_record_unit

__all__ = ["JsonStorageBackend", "install_storage_json"]


class _JsonKvFacet:
    """JSON backend 的键值数据面。open 排除并发同名开。"""

    def __init__(self, owner: "JsonStorageBackend") -> None:
        self._owner = owner

    async def open(self, descriptor: KvUnitDescriptor):
        # await 前的正文同步执行，故保留槽仍排除并发同名 open。
        owner = self._owner
        if owner._closed:
            raise StorageError("closed", "json backend is closed")
        _validate_descriptor(descriptor)
        if descriptor.name in owner._open or descriptor.name in owner._opening:
            raise RuntimeError(
                f"unit {descriptor.name!r} is already open; a unit has exactly "
                "one live handle")
        task = asyncio.ensure_future(owner._open_unit(descriptor))
        owner._opening[descriptor.name] = task
        try:
            return await task
        finally:
            owner._opening.pop(descriptor.name, None)


class JsonStorageBackend:
    """JSON backend：拥有文件树根，服务 kv facet。"""

    def __init__(self, root: str) -> None:
        self._root = root
        self._open: dict[str, Any] = {}
        # 同步在 open() 入口保留，使并发开同名 unit 失败、close 能等还在飞的 open。
        self._opening: dict[str, asyncio.Future] = {}
        self._closed = False
        self.kv = _JsonKvFacet(self)

    async def _open_unit(self, descriptor: KvUnitDescriptor):
        await asyncio.to_thread(os.makedirs, self._root, exist_ok=True)
        # 两种布局仅 medium 形态不同；各 opener 拥有共享根下的自身路径约定。
        def on_close() -> None:
            self._open.pop(descriptor.name, None)

        if descriptor.layout == "per-record":
            unit = open_per_record_unit(descriptor, self._root, on_close)
        else:
            unit = open_single_unit(descriptor, self._root, on_close)
        if self._closed:
            await unit.close()
            raise StorageError("closed", "json backend is closed")
        self._open[descriptor.name] = unit
        return unit

    async def close(self) -> None:
        if not self._closed:
            self._closed = True
        await asyncio.gather(*list(self._opening.values()), return_exceptions=True)
        for unit in list(self._open.values()):
            await unit.close()

    @property
    def root(self) -> str:
        return self._root


def _validate_descriptor(descriptor: KvUnitDescriptor) -> None:
    if not unit_name_ok(descriptor.name):
        raise StorageError("malformed-medium", f"invalid unit name {descriptor.name!r}")
    for table in descriptor.tables:
        if not unit_name_ok(table):
            raise StorageError(
                "malformed-medium",
                f"invalid table name {table!r} in unit {descriptor.name!r}")


def install_storage_json(ctx: Context, root: str) -> JsonStorageBackend:
    """装配 `json` backend 到 ctx.storage（mini 装配点，对齐上游 apply）。

    经 storage_backend_service_key 提供生命周期键，保证依赖方可注入等待注册。
    """
    backend = JsonStorageBackend(root)
    storage = ctx.get("storage")
    storage.backend.register("json", backend)
    ctx.provide(storage_backend_service_key("json"), backend)
    return backend