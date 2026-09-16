"""domain 数据形态（`ctx.storage.domain`）：具 schema 校验、发射变更事件的 KV
domain，跑在 storage backend 之上。

上游对照：packages/storage/storage-domain/src/index.ts（DomainFacility + parseRecord）。

契约：open 的每一步各自使整次调用失败——reject 已在开的名字
（already-open）；经 route 表解析 backend（backend-not-found 由 hub 原样穿过）；
要求其 kv facet（facet-unsupported）；按 spec 投影打开 unit（backend 的
version-mismatch / malformed-medium 穿过）；用 spec 的 schema 校验每条持久记录
（invalid-record 带违规表与 key——除非 spec 声明 invalid_records='backup-and-skip'
且 unit 可把文档移开，此时坏记录备份、记日志、跳过）；构造 domain。

生命周期：调用方拥有返回的句柄，经 Domain.close() 关闭（典型作为自身
ctx.effect 的 disposer）——facility 不把 domain 绑到任何消费者 fiber；facility
卸载时仍开着的 domain 由配置的 close 钩子收走。
"""
from __future__ import annotations

from typing import Any

from ..core.schema import ValidationError, validate_schema_value
from ..core.scope import Context
from .domain import DomainImpl
from .error import DomainError
from .spec import DomainSpec, descriptor_of

__all__ = ["DomainFacility"]


class DomainFacility:
    """已挂载的 domain 设施：经路由 backend 打开声明域；一个设施实例拥有
    open-domain 表并强制每域单开。"""

    def __init__(self, ctx: Context, config: dict) -> None:
        self._ctx = ctx
        self._config = config
        self._domains: dict[str, DomainImpl] = {}
        self._reserved: set[str] = set()

    async def open(self, spec: DomainSpec) -> DomainImpl:
        """打开一个声明 domain（步骤与失败路径见模块 docstring）。"""
        if spec.name in self._reserved:
            raise DomainError("already-open", f"domain {spec.name!r} is already open")
        self._reserved.add(spec.name)
        try:
            backend_name = self._config.get("routes", {}).get(spec.name, self._config["backend"])
            backend = self._ctx.get("storage").backend.get(backend_name)
            if backend.kv is None:
                raise DomainError(
                    "facet-unsupported",
                    f"backend {backend_name!r} routed for domain {spec.name!r} has no kv facet")
            unit = await backend.kv.open(descriptor_of(spec))
            try:
                snapshot = await unit.load_all()
                tables: dict[str, dict[str, Any]] = {}
                for table, table_spec in spec.tables.items():
                    records: dict[str, Any] = {}
                    for key, raw in (snapshot.get("tables", {}).get(table, {})).items():
                        try:
                            parsed = validate_schema_value(table_spec.value_schema, raw)
                        except ValidationError as error:
                            # backup-and-skip 政策（可弃派生数据）：把记录文档移到
                            # 一边，记具体失败，开域但不带此记录。unit 移不开则保留响亮路径。
                            if spec.invalid_records != "backup-and-skip" or (
                                    not hasattr(unit, "backup_record")):
                                raise _invalid_record(spec.name, table, key, error)
                            moved = await unit.backup_record(table, key)
                            logger = self._ctx.get("logger")
                            logger(f"storage.{spec.name}").error(
                                f"domain {spec.name!r}: stored record {key!r} in table "
                                f"{table!r} failed schema validation; moved to {moved!r} "
                                f"and treated as absent. Cause: {error}")
                            continue
                        records[key] = parsed
                    tables[table] = records
                # null 存储 global = "never written"：先提供 initial，不物化——
                # 首次 set 才写。
                global_spec = spec.global_
                if global_spec is None:
                    global_value: Any = None
                elif snapshot.get("global") is None:
                    global_value = global_spec.initial
                else:
                    try:
                        global_value = validate_schema_value(global_spec.schema, snapshot["global"])
                    except ValidationError as error:
                        raise _invalid_record(spec.name, "", "", error)
                # on_closed 严格在 teardown 完成后跑：排空中落地的写仍发
                # domain/changed，domain 到彻底关闭前始终可解析，随后域名才腾出。
                domain = DomainImpl(
                    self._ctx, spec.name, unit, tables, global_value,
                    has_global=global_spec is not None,
                    on_closed=lambda: self._free(spec.name))
                self._domains[spec.name] = domain
                return domain
            except Exception:
                await unit.close()
                raise
        except Exception:
            # 任何失败 = domain 从不注册（其后不可能再抛），无条件释放名保留。
            self._reserved.discard(spec.name)
            raise

    def get(self, name: str) -> DomainImpl | None:
        """按名查 open domain（无类型，诊断面）。类型化消费者持有 open 的句柄。"""
        return self._domains.get(name)

    async def close_all(self) -> None:
        """关闭本设施仍开着的每个 domain。未自调 Domain.close() 的消费者的
        卸载路径；close 幂等，双关无害。"""
        for domain in list(self._domains.values()):
            await domain.close()

    def _free(self, name: str) -> None:
        self._domains.pop(name, None)
        self._reserved.discard(name)


def _invalid_record(domain: str, table: str, key: str, cause: ValidationError) -> DomainError:
    slot = "global" if table == "" else f"record {key!r} in table {table!r}"
    return DomainError(
        "invalid-record",
        f"domain {domain!r}: stored {slot} does not match its schema",
        detail={"table": table, "key": key},
        cause=cause)