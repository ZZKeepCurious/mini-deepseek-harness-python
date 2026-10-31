"""deepseek-official 路由的 ``GET /models`` listing（对齐 llm-deepseek-api-key）。

对应上游：packages/llm/llm-deepseek-api-key/src/index.ts +
packages/llm/llm-deepseek/src/listed-catalog.ts + listing-cache.ts。
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Callable
from urllib.parse import urlparse

from .deepseek_account import (
    AccountModelCatalog,
    CatalogSettings,
    DEFAULT_MODELS_MAX_AGE_MS,
    DEFAULT_MODELS_TIMEOUT_MS,
    request_model_listing,
)
from .listed_catalog import ListedCatalog, refuse_listed_model, resolve_listed_catalog
from .protocol import APP_IDENTITY, INVALID_CREDENTIAL, MISSING_CREDENTIAL, LlmFailure

__all__ = [
    "OfficialRoute",
    "build_official_route",
    "is_builtin_catalog",
    "model_listing_url",
    "should_follow_official_listing",
]

logger = logging.getLogger(__name__)

LOG_TAG = "[llm-deepseek-api-key]"
OFFICIAL_ORIGIN = "https://api.deepseek.com"


def _stderr_warn(message: str) -> None:
    print(f"{LOG_TAG} {message}", file=sys.stderr)


def model_listing_url(base_url: str) -> str:
    """Messages base 同 origin 的 ``/models`` 端点（listing.ts modelListingURL）。"""
    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return f"{origin}/models"


def is_builtin_catalog(models: tuple[Any, ...]) -> bool:
    from .deepseek_files import DEFAULT_MODELS

    return tuple(model.id for model in models) == tuple(model.id for model in DEFAULT_MODELS)


def should_follow_official_listing(base_url: str, models: tuple[Any, ...]) -> bool:
    parsed = urlparse(base_url.rstrip("/"))
    origin = f"{parsed.scheme}://{parsed.netloc}"
    return origin == OFFICIAL_ORIGIN and is_builtin_catalog(models)


class OfficialRoute:
    """official 路由连接事实（api-key index.ts connectionOf / discoverModels）。"""

    def __init__(self, *, catalog: AccountModelCatalog, supported, route_max_tokens: int,
                 client_version: str | None = None):
        self._catalog = catalog
        self._supported = tuple(supported)
        self._route_max_tokens = route_max_tokens
        self._client_version = client_version or APP_IDENTITY.version
        self._reported_listing = None
        self._resolution: tuple[Any, ...] = self._supported

    def _resolve(self) -> ListedCatalog:
        view = self._catalog.current()
        listing = view["listing"]
        result = resolve_listed_catalog(
            listing=(None if listing is None else listing.entries),
            supported=self._supported,
            overrides={},
            route_max_tokens=self._route_max_tokens,
            client_version=self._client_version,
        )
        if listing is not None and listing is not self._reported_listing:
            self._reported_listing = listing
            for problem in result.problems:
                _stderr_warn(problem)
        self._resolution = result.resolution
        return result

    def resolution_models(self) -> tuple[Any, ...]:
        return self._resolution

    def admit(self, model: str) -> None:
        refuse_listed_model(model, self._resolve().refusals.get(model))

    async def refresh(self) -> None:
        await self._catalog.view()

    async def connect(self) -> None:
        """每次模型请求前刷新 listing 视图（上游 registerDeepSeekProvider.connect）。"""
        await self.refresh()
        self._resolve()

    def discover_models_sync(self) -> list:
        from .deepseek_files import catalog_model_info

        view = self._catalog.current()
        if not view["signed_in"]:
            return []
        result = self._resolve()
        return [
            catalog_model_info("deepseek-official", model, result.badges.get(model.id))
            for model in result.offered
        ]


def build_official_route(*, base_url: str, supported, route_max_tokens: int,
                         present_api_key: Callable[[], str | None],
                         transport: Any = None,
                         models_timeout_ms: int = DEFAULT_MODELS_TIMEOUT_MS,
                         models_max_age_ms: int = DEFAULT_MODELS_MAX_AGE_MS) -> OfficialRoute:
    settings = CatalogSettings(
        url=model_listing_url(base_url),
        timeout_ms=models_timeout_ms,
        max_age_ms=models_max_age_ms,
    )

    async def token(_url: str) -> str | None:
        return present_api_key()

    def load(url: str, api_key: str, timeout_ms: int):
        return request_model_listing(
            url, {"x-api-key": api_key}, timeout_ms=timeout_ms, transport=transport)

    catalog = AccountModelCatalog(
        settings=lambda: settings,
        token=token,
        load=load,
        warn=lambda message: _stderr_warn(message),
        info=lambda message: logger.info("%s %s", LOG_TAG, message),
    )
    return OfficialRoute(
        catalog=catalog,
        supported=supported,
        route_max_tokens=route_max_tokens,
    )


def present_api_key_from_adapter(adapter) -> str | None:
    """解析 API key；缺失 → None，畸形 → 抛 INVALID_CREDENTIAL（index.ts presentApiKey）。"""
    resolver = getattr(adapter, "_resolve_api_key", None)
    if resolver is not None:
        try:
            return resolver()
        except LlmFailure:
            raise
    if getattr(adapter, "_auth", None) != "official":
        return None
    key = (getattr(adapter, "_key", "") or "").strip()
    if len(key) == 0:
        return None
    from .deepseek import _LEGAL_API_KEY

    if not _LEGAL_API_KEY.match(key):
        raise LlmFailure(
            INVALID_CREDENTIAL,
            "llm-deepseek: the API key resolved from DEEPSEEK_API_KEY contains "
            "characters no HTTP header can carry; set DEEPSEEK_API_KEY to the raw "
            "key alone (the web Models page writes it)",
        )
    return key
