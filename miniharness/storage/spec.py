"""domain 声明词汇：一个 spec 对象是 domain 身份/布局/记录 schema 的唯一来源。

上游对照：packages/storage/storage-domain/src/spec.ts（DomainSpec / DomainGlobalSpec /
DomainTableSpec / defineDomain / domainTable / descriptorOf）。

上游用 zod 表达记录 schema；mini 载波采用 core/schema（schemastery 移植）的
schema 节点（`validate_schema_value`），语义等价：都是 durable 边界上的校验器。
defineDomain（mini: define_domain）在拥有方模块加载时即校验，任何中等前 fail
loud——domain 或表名不在 UNIT_NAME_RE、version 非负整数、global schema 接受
null 都抛错（null 拒绝防 round-trip：backend 以 null 作 "never written"
哨兵，可空 global 会与 absent 混淆，存库 null 静默回退 initial）。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..core.schema import ValidationError, validate_schema_value
from .backend import UNIT_NAME_RE, KvUnitDescriptor, unit_name_ok

__all__ = [
    "DomainGlobalSpec",
    "DomainTableSpec",
    "DomainSpec",
    "domain_table",
    "define_domain",
    "descriptor_of",
]

UNIT_NAME_RE_TEXT = UNIT_NAME_RE.pattern


@dataclass(frozen=True)
class DomainGlobalSpec:
    """global 单例声明：schema + 首次写前的取值。schema 不得接受 null。"""

    schema: Any
    initial: Any


@dataclass(frozen=True)
class DomainTableSpec:
    """一张表的声明：value_schema 校验每条持久记录。"""

    value_schema: Any


@dataclass(frozen=True)
class DomainSpec:
    """一个 domain 的静态声明：身份、版本与记录布局。

    version：当前 domain 格式版本；按所选 layout 在读侧强制。
    layout：'single'（缺省）整 unit 一文档；'per-record' 每条记录一文档。
    compatible_versions：老版本（当前 schema 仍可接受）的 'per-record' 文档仍可读。
    invalid_records：'backup-and-skip' 时坏记录移到一边继续 open（可弃派生数据）。
    global_：可选 global 单例槽。
    tables：表声明，keyed by 表名（每名须匹配 UNIT_NAME_RE）。
    """

    name: str
    version: int
    tables: dict[str, DomainTableSpec] = field(default_factory=dict)
    layout: str | None = None
    compatible_versions: tuple[int, ...] = ()
    invalid_records: str | None = None
    global_: DomainGlobalSpec | None = None


def domain_table(schema: Any) -> DomainTableSpec:
    """声明一张表（对齐 upstream domainTable）。"""
    return DomainTableSpec(value_schema=schema)


def define_domain(spec: DomainSpec) -> DomainSpec:
    """身份助手：钉住字面类型并校验字段，异常在拥有方模块加载时抛（任何 medium 被触前）。

    返回同一 spec 实例（对齐 upstream defineDomain 的原样返回）。
    """
    if not unit_name_ok(spec.name):
        raise ValueError(f"domain name {spec.name!r} must match {UNIT_NAME_RE_TEXT}")
    if not isinstance(spec.version, int) or isinstance(spec.version, bool) or spec.version < 0:
        raise ValueError(
            f"domain {spec.name!r} version must be a non-negative integer, got {spec.version}")
    for compat in spec.compatible_versions:
        if not isinstance(compat, int) or isinstance(compat, bool) or compat < 0 or compat >= spec.version:
            raise ValueError(
                f"domain {spec.name!r} compatible_versions entries must be non-negative "
                f"integers below version {spec.version}, got {compat}")
    if spec.layout is not None:
        if spec.layout not in ("single", "per-record"):
            raise ValueError(
                f"domain {spec.name!r} layout must be 'single' or 'per-record', got {spec.layout}")
    if spec.invalid_records is not None:
        if spec.invalid_records != "backup-and-skip":
            raise ValueError(
                f"domain {spec.name!r} invalid_records must be 'backup-and-skip' when present, "
                f"got {spec.invalid_records}")
    for table in spec.tables:
        if not unit_name_ok(table):
            raise ValueError(
                f"domain {spec.name!r} table name {table!r} must match {UNIT_NAME_RE_TEXT}")
    if spec.global_ is not None:
        _reject_null_global(spec)
    return spec


def _reject_null_global(spec: DomainSpec) -> None:
    """global schema 不得接受 null：null 是 medium 的 "never written" 哨兵。"""
    try:
        validate_schema_value(spec.global_.schema, None)
    except ValidationError:
        return  # null 被拒，符合预期
    raise ValueError(
        f"domain {spec.name!r} global schema must not accept null: "
        "null is the medium's 'never written' sentinel, so a stored null could not round-trip")


def descriptor_of(spec: DomainSpec) -> KvUnitDescriptor:
    """把 spec 投影到 backend 面对的 unit descriptor（对齐 upstream descriptorOf）。"""
    kwargs: dict[str, Any] = {
        "name": spec.name,
        "version": spec.version,
        "tables": tuple(spec.tables),
        "has_global": spec.global_ is not None,
    }
    if spec.layout is not None:
        kwargs["layout"] = spec.layout
    if spec.compatible_versions:
        kwargs["compatible_versions"] = tuple(spec.compatible_versions)
    return KvUnitDescriptor(**kwargs)