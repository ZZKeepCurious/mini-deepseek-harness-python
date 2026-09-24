"""web 能力 seam 的运行时（上游 packages/web/web/src/index.ts）。

注册表 + 执行期 provider 选择：配置 id 在执行时解析（绝不依赖注册顺序）；
重复 id 拒绝；``search`` 结果按 ``request.maxResults`` 截断并置 ``truncated``。

选择语义（index.ts:65 注释与 resolveProvider 逐条对齐）：
  * 配置 id 已注册且可用 → 该 provider；
  * 配置 id 未注册 → WEB_PROVIDER_CONFIGURED_MISSING；
  * 配置 id 已注册但不可用 → WEB_PROVIDER_CONFIGURED_UNAVAILABLE；
  * 未配置、恰好一个可用 → 自动选中；
  * 未配置、多个可用 → WEB_PROVIDER_AMBIGUOUS（消息带可用 id 列表）；
  * 未配置、零可用 → WEB_PROVIDER_UNAVAILABLE。
配置来源 ``config.searchProvider/fetchProvider`` 缺省回落环境变量
``DSH_WEB_SEARCH_PROVIDER`` / ``DSH_WEB_FETCH_PROVIDER``（index.ts:92-93——
环境覆写进同一字段，不是隐藏优先级链）。
"""
from __future__ import annotations

import os
from typing import Callable, TypeVar

from .types import WebError

__all__ = ["WebRuntime", "WebRuntimeConfig"]

T = TypeVar("T")


class WebRuntimeConfig:
    """provider 钉选配置（index.ts:55）。"""

    __slots__ = ("searchProvider", "fetchProvider")

    def __init__(self, searchProvider: str | None = None, fetchProvider: str | None = None):
        self.searchProvider = searchProvider
        self.fetchProvider = fetchProvider


class WebRuntime:
    """web 访问服务：双注册表 + 执行期选择 + maxResults 封顶。"""

    def __init__(self, config: WebRuntimeConfig | None = None):
        config = config or WebRuntimeConfig()
        self._search_providers: dict[str, object] = {}
        self._fetch_providers: dict[str, object] = {}
        self._search_provider_id = config.searchProvider or os.environ.get("DSH_WEB_SEARCH_PROVIDER")
        self._fetch_provider_id = config.fetchProvider or os.environ.get("DSH_WEB_FETCH_PROVIDER")

    def register_search_provider(self, provider) -> Callable[[], None]:
        """注册 search provider；重复 id 抛 WEB_DUPLICATE_PROVIDER；返回注销闭包。"""
        return self._register(self._search_providers, provider)

    def register_fetch_provider(self, provider) -> Callable[[], None]:
        """注册 fetch provider；重复 id 抛 WEB_DUPLICATE_PROVIDER；返回注销闭包。"""
        return self._register(self._fetch_providers, provider)

    @staticmethod
    def _register(store: dict[str, object], provider) -> Callable[[], None]:
        if provider.id in store:
            raise WebError(
                f'a web provider with id "{provider.id}" is already registered',
                "WEB_DUPLICATE_PROVIDER",
            )
        store[provider.id] = provider

        def dispose() -> None:
            store.pop(provider.id, None)

        return dispose

    async def search(self, request: dict, signal=None) -> dict:
        """经选中 provider 执行一次搜索；结果按 request.maxResults 封顶。"""
        provider = _resolve_provider(
            configured_id=self._search_provider_id,
            providers=self._search_providers,
        )
        result = await provider.search(request, signal)
        return _cap_sources(result, request.get("maxResults"))

    async def fetch(self, request: dict, signal=None) -> dict:
        """经选中 provider 取回一个 URL；非 2xx 是结果而非抛错。"""
        provider = _resolve_provider(
            configured_id=self._fetch_provider_id,
            providers=self._fetch_providers,
        )
        return await provider.fetch(request, signal)


def _resolve_provider(configured_id: str | None, providers: dict[str, object]):
    """执行期解析 provider，或抛对应 WebError（index.ts:172）。"""
    if configured_id is not None:
        provider = providers.get(configured_id)
        if provider is None:
            raise WebError(
                f'configured web provider "{configured_id}" is not registered',
                "WEB_PROVIDER_CONFIGURED_MISSING",
            )
        if not provider.available():
            raise WebError(
                f'configured web provider "{configured_id}" is registered but unavailable',
                "WEB_PROVIDER_CONFIGURED_UNAVAILABLE",
            )
        return provider
    usable = [p for p in providers.values() if p.available()]
    if not usable:
        raise WebError("no usable web provider is registered", "WEB_PROVIDER_UNAVAILABLE")
    if len(usable) > 1:
        ids = ", ".join(p.id for p in usable)
        raise WebError(
            f"multiple usable web providers are registered ({ids}); configure one explicitly",
            "WEB_PROVIDER_AMBIGUOUS",
        )
    return usable[0]


def _cap_sources(result: dict, max_results: int | None) -> dict:
    """封顶 search 结果：截断 sources 并置 truncated（index.ts:197）。"""
    sources = result.get("sources", [])
    if max_results is None or len(sources) <= max_results:
        return result
    return {**result, "sources": sources[:max_results], "truncated": True}
