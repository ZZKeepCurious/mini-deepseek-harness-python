"""一个 open domain 的运行时：权威内存态 + 单写链 + 变更事件发射。

上游对照：packages/storage/storage-domain/src/domain.ts（DomainImpl / KvTableImpl /
DomainGlobal + TableHost 内部边界）。

契约：读同步走内存；每个写排上写链，先等 backend 持久，再改内存，再发
`domain/changed`——backend 写被拒则内存不动（读与 medium 零分歧），事件携带
的取值等于发射时刻的内存态，按写链顺序到达。close：立即拒绝新写、排空已排队的写
（其事件照发）、释放 unit、释放域名（经 facility 钩子）。幂等——重复调用共享
一次 teardown。
"""
from __future__ import annotations

import asyncio
from typing import Any, Awaitable, Callable, Iterable

from ..core.scope import Context
from .backend import KvUnit
from .error import DomainError
from .events import DomainChanged


class DomainGlobal:
    """domain global 单例句柄。首次 set 才是 medium 上的物化。"""

    def __init__(self, impl: "DomainImpl") -> None:
        self._impl = impl

    def get(self) -> Any:
        self._impl.assert_readable()
        return self._impl._global_value

    def set(self, value: Any) -> Awaitable[None]:
        """替换值持久化。经写链排队；首次 set 物化 global。"""

        async def job() -> None:
            await self._impl._unit.set_global(value)
            self._impl._global_value = value
            self._impl.emit_changed(DomainChanged(
                domain=self._impl.name, table="", key="", operation="put", value=value))

        return self._impl.enqueue(job)


class KvTable:
    """一张声明表的句柄。记录是不可变纯数据：返回即存储对象本身（无防御性
    拷贝），不得原地修改——用 put/update 替换。"""

    def __init__(self, host: "TableHost", table_name: str) -> None:
        self._host = host
        self._table = table_name

    def _records(self) -> dict[str, Any]:
        return self._host.records_of(self._table)

    def get(self, key: str) -> Any:
        self._host.assert_readable()
        return self._records().get(key)

    def entries(self) -> Iterable[tuple[str, Any]]:
        self._host.assert_readable()
        return list(self._records().items())

    def keys(self) -> Iterable[str]:
        self._host.assert_readable()
        return list(self._records().keys())

    @property
    def size(self) -> int:
        self._host.assert_readable()
        return len(self._records())

    def put(self, key: str, value: Any) -> Awaitable[None]:
        """持久插入/覆盖一条记录（整条新记录，无部分合并）。"""

        async def job() -> None:
            await self._host.unit().put_record(self._table, key, value)
            self._records()[key] = value
            self._host.emit_changed(DomainChanged(
                domain=self._host.domain_name(), table=self._table,
                key=key, operation="put", value=value))

        return self._host.enqueue(job)

    def delete(self, key: str) -> Awaitable[bool]:
        """持久删除一条记录。记录本就不在 → False（无写、无事件）。"""

        async def job() -> bool:
            # 存在性在本 job 的链槽决定，而非调用时：先入队的同 key put
            # 使本 delete 观察到它。
            records = self._records()
            if key not in records:
                return False
            await self._host.unit().delete_record(self._table, key)
            del records[key]
            self._host.emit_changed(DomainChanged(
                domain=self._host.domain_name(), table=self._table,
                key=key, operation="deleted"))
            return True

        return self._host.enqueue(job)

    def update(self, key: str, fn: Callable[[Any], Any]) -> Awaitable[Any]:
        """写链上的原子读改写：fn 看见其队列槽上的当前值，并发更新永不交错。

        缺 key → DomainError('missing-key')。
        """

        async def job() -> Any:
            records = self._records()
            if key not in records:
                raise DomainError(
                    "missing-key",
                    f"domain {self._host.domain_name()!r} table {self._table!r} has no "
                    f"record {key!r} to update")
            next_value = fn(records[key])
            await self._host.unit().put_record(self._table, key, next_value)
            records[key] = next_value
            self._host.emit_changed(DomainChanged(
                domain=self._host.domain_name(), table=self._table,
                key=key, operation="put", value=next_value))
            return next_value

        return self._host.enqueue(job)


class TableHost:
    """内部边界：表句柄拿到 domain 所有写的共用机制。"""

    def __init__(self, impl: "DomainImpl") -> None:
        self._impl = impl

    def domain_name(self) -> str:
        return self._impl.name

    def unit(self) -> KvUnit:
        return self._impl._unit

    def enqueue(self, job: Callable[[], Awaitable[Any]]) -> Awaitable[Any]:
        return self._impl.enqueue(job)

    def assert_readable(self) -> None:
        self._impl.assert_readable()

    def emit_changed(self, change: DomainChanged) -> None:
        self._impl.emit_changed(change)

    def records_of(self, table: str) -> dict[str, Any]:
        return self._impl._records[table]


class DomainImpl:
    """Domain 接口背后的单体实现。facility 从校验过的 load_all 快照构造它，
    并擦为类型化 Domain；本包之外无人构造。"""

    def __init__(
        self,
        ctx: Context,
        name: str,
        unit: KvUnit,
        records: dict[str, dict[str, Any]],
        global_value: Any,
        has_global: bool,
        on_closed: Callable[[], None],
    ) -> None:
        self._ctx = ctx
        self.name = name
        self._unit = unit
        self._records: dict[str, dict[str, Any]] = records
        self._global_value: Any = global_value
        self._has_global = has_global
        self._on_closed = on_closed
        self._tables: dict[str, KvTable] = {
            table: KvTable(TableHost(self), table) for table in records}
        self._global_handle: DomainGlobal | None = DomainGlobal(self) if has_global else None
        self._chain: asyncio.Future | None = None
        self._disposing = False
        self._closed = False
        self._disposal: asyncio.Future | None = None

    @property
    def global_(self) -> DomainGlobal:
        """global 单例句柄。spec 未声明 global 时访问是调用方 bug，抛错。"""
        if self._global_handle is None:
            raise RuntimeError(f"domain {self.name!r} declares no global")
        return self._global_handle

    def table(self, name: str) -> KvTable:
        """解析一个已声明表句柄；未声明名是调用方 bug，抛错。句柄稳定。"""
        table = self._tables.get(name)
        if table is None:
            raise RuntimeError(f"domain {self.name!r} declares no table {name!r}")
        return table

    def close(self) -> Awaitable[None]:
        if self._disposal is None:
            self._disposal = asyncio.ensure_future(self._run_close())
        return self._disposal

    async def _run_close(self) -> None:
        self._disposing = True
        chain = self._chain
        if chain is not None:
            try:
                await chain  # 链尾从不拒（swallow），纯排空屏障
            except Exception:  # noqa: BLE001 防御：closed 仍要释放 unit
                pass
        await self._unit.close()
        self._closed = True
        self._on_closed()

    def emit_changed(self, change: DomainChanged) -> None:
        """一次持久写后的变更通知（含 observer 失败）：写已提交（medium 与
        内存都持有新态），抛错的监听器不得反过头拒绝它。"""
        try:
            self._ctx.emit("domain/changed", change)
        except Exception as error:  # noqa: BLE001 只吞同步派发异常
            logger = self._ctx.get("logger")
            if logger is not None:
                logger(f"storage.{self.name}").warn(
                    f"domain {self.name!r}: domain/changed listener failed: {error}")

    def enqueue(self, job: Callable[[], Awaitable[Any]]) -> Awaitable[Any]:
        if self._disposing:
            raise DomainError("closed", f"domain {self.name!r} is closed")
        # 前置链在 enqueue 现场同步摄取，不能等调度期再读（否则并发排队时每个
        # 任务都串到最后一个链尾上，形成自等待死锁）。
        prev = self._chain

        async def run() -> Any:
            if prev is not None:
                try:
                    await prev
                except Exception:  # noqa: BLE001 前任任务的错由它自己的调用方观察
                    pass
            return await job()

        task = asyncio.ensure_future(run())
        self._chain = task
        return task

    def assert_readable(self) -> None:
        if self._closed:
            raise DomainError("closed", f"domain {self.name!r} is closed")