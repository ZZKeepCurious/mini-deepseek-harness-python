"""Storage backend 词汇：backend 拥有一个 medium，出现于操作组之上。

上游对照：packages/storage/storage/src/backend.ts（KvUnitDescriptor / KvFacet /
StorageBackend / KvUnit + UNIT_NAME_RE）。

契约：backend 拥有且只拥有一个 medium（文件树根 / 数据库文件），其生命周期
横跨全部 facet；facet 是可选成员——不能服务某类数据的 backend 直接省略它，
解析时 fail loud。unit 不做并发写串行化——写排序是调用方（domain 层的单写链）
的责任；unit 只保证每次单次调用在 medium 上原子、且在 resolve 后持久。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol, runtime_checkable

__all__ = [
    "UNIT_NAME_RE",
    "KvUnitDescriptor",
    "KvFacet",
    "StorageBackend",
    "KvUnit",
]


#: unit / table 名允许集：可安全用作文件名与 SQL 标识符段（对齐 upstream UNIT_NAME_RE）。
UNIT_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class KvUnitDescriptor:
    """一个 KV unit 的静态身份与形态，由其 owner 的 spec 投影而来。

    layout：'single'（缺省）整个 unit 一个文档；'per-record' 每条记录一个
    文档（大/稀疏/单体可弃记录适用，未接受版本戳只弃该记录而非拒整个 unit）。
    compatible_versions：'per-record' 读取接受文档携带的列出版本（老版本
    schema 仍可读），写恒盖 version。'single' 读取精确版本。
    """
    name: str
    version: int
    tables: tuple[str, ...] = ()
    has_global: bool = False
    layout: str | None = None
    compatible_versions: tuple[int, ...] = ()


@runtime_checkable
class KvUnit(Protocol):
    """一个打开的 unit。

    load_all 返回全量快照（每表 keyed by 表名 + global 单例，never-written 为 None）。
    put/delete/set_global 为持久写原语；per-record 布局下 key 成为路径段，
    必须匹配 `[a-zA-Z0-9_-]+`（不安全 key 拒绝）。close 后任何调用以 'closed' 拒绝。
    """

    async def load_all(self) -> dict[str, Any]: ...

    async def put_record(self, table: str, key: str, value: Any) -> None: ...

    async def delete_record(self, table: str, key: str) -> None: ...

    async def backup_record(self, table: str, key: str) -> str: ...

    async def set_global(self, value: Any) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class KvFacet(Protocol):
    """按键值数据形态：整 unit 快照 + 逐记录持久写。

    open 打开一个 unit（medium 无痕迹则创建；物化可拖延到首次写，但 load_all
    必须立刻给出空形态）。medium 上已盖的版本与 descriptor.version 不同则以
    'version-mismatch' 拒绝；无法按该 unit 解析的 medium 以 'malformed-medium'
    拒绝。同名 unit 未关闭再开是调用方 bug，拒绝。
    """

    async def open(self, descriptor: KvUnitDescriptor) -> KvUnit: ...


@runtime_checkable
class StorageBackend(Protocol):
    """一个已注册 backend。kv 可选；close 排空跨全部已开 unit 的写并释放 medium（幂等）。"""

    kv: KvFacet | None

    async def close(self) -> None: ...


def unit_name_ok(name: str) -> bool:
    """unit / table 名是否匹配 UNIT_NAME_RE（实施面共享校验）。"""
    return UNIT_NAME_RE.match(name) is not None