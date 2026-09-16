"""Storage hub（`ctx.storage`）：具名 backend 表 + 可挂载数据形态。

上游对照：packages/storage/storage/src/index.ts（Storage + storageBackendServiceKey）。

hub 本身不执行 IO——backend 拥有 medium，数据形态（先是 domain 层）拥有语义。
"""
from __future__ import annotations

from typing import Any, Callable

from ..core.scope import Context, Service
from .error import StorageError
from .registry import BackendRegistry

__all__ = ["Storage", "storageBackendServiceKey"]


def storage_backend_service_key(name: str) -> str:
    """一个具名 backend 插件的生命周期服务键（对齐上游 storageBackendServiceKey）。

    domain-form provider 注入该键，让激活不能与 backend 注册竞速——即便调用方
    始终经 storage 注册表解析 backend。
    """
    return f"storage.backend.{name}"


class Storage(Service):
    """storage hub 服务。backend 注册在 `backend` 下；数据形态挂在各自
    StorageForms 键下，经 `ctx.storage.<form>` 到达。

    构造即经 ctx.provide('storage') 登记，随拥有 fiber 注销（cordis Service 基座）。
    """

    provide = "storage"

    def __init__(self, ctx: Context) -> None:
        self.backend = BackendRegistry()
        self._forms: dict[str, Any] = {}
        super().__init__(ctx)

    def mount(self, form: str, facility: Any) -> Callable[[], None]:
        """挂载一个数据形态设施。挂载是 effect：返回的 disposer 卸载该形态。

        同键重复挂载 → StorageError('duplicate-mount')。
        """
        if form in self._forms:
            raise StorageError(
                "duplicate-mount",
                f"storage form {form!r} is already mounted",
            )
        self._forms[form] = facility

        def unmount() -> None:
            # 与 BackendRegistry.register 同款过期守卫：dispose + 重挂后过期
            # disposer 不再移除后继。
            if self._forms.get(form) is facility:
                del self._forms[form]

        return unmount

    def form(self, form: str) -> Any:
        """解析一个已挂载的数据形态；未挂载 → StorageError('form-not-mounted')。"""
        facility = self._forms.get(form)
        if facility is None:
            raise StorageError(
                "form-not-mounted",
                f"storage form {form!r} is not mounted",
            )
        return facility

    @property
    def domain(self) -> Any:
        """domain 数据形态（domain 层插件加载后存在）。"""
        return self.form("domain")