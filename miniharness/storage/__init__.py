"""storage：非会话 KV 存储中心（`ctx.storage`）——hub + domain 数据形态 + JSON backend。

上游对照：packages/storage/（storage hub + storage-domain + storage-json；
storage-sqlite 不承载，载体差异见 verified-diffs）。

三层组织：
  * hub（hub.py）—— 具名 backend 表 + 可挂载数据形态，不执行任何 IO。
  * domain 数据形态（facility/domain/spec/events）—— schema 校验、单写链、
    持久后 `domain/changed` 事件发射。消费方只依赖本层，绝不直接碰 backend。
  * JSON backend（jsonbackend/）—— human-readable 持久 medium：'single' 整
    unit 一文档，'per-record' 一记录一文档；原子重写发布。

mini 装配点：install_storage(ctx, root) 一次搭好 hub + json backend + domain
设施（对齐上游 storage 插件 + storage-json + storage-domain 的组合加载）。
"""
from __future__ import annotations

from ..core.scope import Context
from .error import DomainError, StorageError
from .events import DomainChanged
from .hub import Storage, storage_backend_service_key
from .registry import BackendRegistry
from .facility import DomainFacility
from .jsonbackend import JsonStorageBackend, install_storage_json
from .domain import DomainGlobal, DomainImpl, KvTable
from .spec import (
    DomainGlobalSpec,
    DomainSpec,
    DomainTableSpec,
    define_domain,
    descriptor_of,
    domain_table,
)

__all__ = [
    "Storage",
    "storage_backend_service_key",
    "BackendRegistry",
    "StorageError",
    "DomainError",
    "DomainChanged",
    "DomainFacility",
    "DomainGlobal",
    "DomainImpl",
    "KvTable",
    "DomainSpec",
    "DomainGlobalSpec",
    "DomainTableSpec",
    "define_domain",
    "descriptor_of",
    "domain_table",
    "JsonStorageBackend",
    "install_storage",
]


def install_storage(ctx: Context, root: str) -> Storage:
    """装配 storage hub + `json` backend + domain 数据形态。

    上游 storage-json 的 root 无缺省（不回退 cwd——unit 文件会撒得到处都是）；
    调用方必须显式给出根目录。返回 hub 服务（ctx.storage）。
    """
    if not root:
        raise ValueError("storage root must be an explicit non-empty path")
    storage = Storage(ctx)
    install_storage_json(ctx, root)
    facility = DomainFacility(ctx, {"backend": "json"})
    storage.mount("domain", facility)
    return storage