"""registry 探测（对齐 packages/client/ui-plugin-manager PluginRegistryProbe）。

mini 探测 PyPI simple 索引（Python 插件安装面），wire 仍返回 registry 根 URL 或 null。
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx

from ..core.scope import Context, Service

__all__ = [
    "PluginRegistryProbe",
    "install_plugin_registry_probe",
]

PYPI_SIMPLE = "https://pypi.org/simple/"
DEFAULT_TIMEOUT_MS = 1500
DEFAULT_CACHE_TTL_MS = 300_000


class PluginRegistryProbe(Service):
    provide = "pluginRegistryProbe"

    def __init__(
        self,
        ctx: Context,
        *,
        registry_probe_enabled: bool = True,
        registry_probe_timeout_ms: int = DEFAULT_TIMEOUT_MS,
        registry_probe_cache_ttl_ms: int = DEFAULT_CACHE_TTL_MS,
    ):
        super().__init__(ctx, "pluginRegistryProbe")
        self._enabled = registry_probe_enabled
        self._timeout_ms = registry_probe_timeout_ms
        self._cache_ttl_ms = registry_probe_cache_ttl_ms
        self._cached: tuple[str | None, float] | None = None

    def fastest(self) -> str | None:
        if not self._enabled:
            return None
        now = time.time() * 1000
        if self._cached is not None and self._cached[1] > now:
            return self._cached[0]
        registry = self._probe()
        self._cached = (registry, now + self._cache_ttl_ms)
        return registry

    def _probe(self) -> str | None:
        timeout = self._timeout_ms / 1000
        endpoints = [PYPI_SIMPLE]
        for endpoint in endpoints:
            try:
                with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                    response = client.head(endpoint)
                    if response.status_code >= 200 and response.status_code < 300:
                        return f"{urlparse(endpoint).scheme}://{urlparse(endpoint).netloc}/"
            except httpx.HTTPError:
                continue
        return None


def install_plugin_registry_probe(ctx: Context, **config: Any) -> PluginRegistryProbe:
    existing = ctx.get("pluginRegistryProbe")
    if existing is not None:
        return existing
    return PluginRegistryProbe(
        ctx,
        registry_probe_enabled=bool(config.get("registryProbeEnabled", True)),
        registry_probe_timeout_ms=int(config.get("registryProbeTimeoutMs") or DEFAULT_TIMEOUT_MS),
        registry_probe_cache_ttl_ms=int(config.get("registryProbeCacheTtlMs") or DEFAULT_CACHE_TTL_MS),
    )
