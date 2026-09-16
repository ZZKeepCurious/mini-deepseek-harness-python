"""domain 数据形态的变更事件词汇。

上游对照：packages/storage/storage-domain/src/events.ts（DomainChanged）。

契约：每次持久写恰发一个事件，严格在 backend 确认持久之后、携带新快照与操作
判别符——绝不携带旧值（diff 消费者自留上一个快照）。单 domain 的事件按写链
顺序到达。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = ["DomainChanged"]

_NONE = object()


@dataclass(frozen=True)
class DomainChanged:
    """一次持久的 domain 变更。operation = 'put' | 'deleted'。

    put：记录（或 global 单例）插入/覆盖，value 为新快照。
    deleted：记录删除，无 value。table/key 为 '' 时表示 global 单例写。
    """

    domain: str
    table: str
    key: str
    operation: str
    value: Any = _NONE

    def has_value(self) -> bool:
        return self.value is not _NONE