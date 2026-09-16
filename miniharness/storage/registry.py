"""具名 backend 注册表（storage hub 的 backend 表）。

上游对照：packages/storage/storage/src/registry.ts（BackendRegistry）。

契约：可变 name → backend 表；多 backend 并行挂载互不干扰。哪个 backend 服务
哪个消费者是消费者的配置（domain 层的 route 表），绝非 hub 全局决定。
"""
from __future__ import annotations

from typing import Callable

from .backend import StorageBackend
from .error import StorageError

__all__ = ["BackendRegistry"]


class BackendRegistry:
    """具名 backend 注册表。register 是 effect：返回的 disposer 移除该名。

    dispose 不 close backend——拥有方插件在注销后负责 close。
    """

    def __init__(self) -> None:
        self._backends: dict[str, StorageBackend] = {}

    def register(self, name: str, backend: StorageBackend) -> Callable[[], None]:
        if name in self._backends:
            raise StorageError(
                "duplicate-backend",
                f"storage backend {name!r} is already registered",
            )
        self._backends[name] = backend

        def dispose() -> None:
            # 只移除本注册的贡献：dispose + 重注册后，过期 disposer 再触发
            # 不得移除后继者。
            if self._backends.get(name) is backend:
                del self._backends[name]

        return dispose

    def get(self, name: str) -> StorageBackend:
        backend = self._backends.get(name)
        if backend is None:
            registered = ", ".join(self._backends) or "none"
            raise StorageError(
                "backend-not-found",
                f"storage backend {name!r} is not registered (registered: {registered})",
            )
        return backend

    def names(self) -> list[str]:
        """已注册 backend 名快照（诊断用）。"""
        return list(self._backends)