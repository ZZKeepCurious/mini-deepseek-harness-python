"""插件市场 Host 面（对齐 packages/boot/plugin-market/src/index.ts 的 Remote 子集）。

wire：`listIndexes` / `setIndexes` / `search` / `readEntryDetail`。
安装载体为 Python 模块（format 1 扩展 `source.type: python`）；npm/git 索引仍可
搜索，install 字段保持上游形状或 python 扩展（不执行 pnpm 安装）。
"""
from __future__ import annotations

import time
from typing import Any
from urllib.parse import urlparse

import httpx

from ..core.agent_loop.resident_loop import run_on_resident
from ..core.scope import Context, Service
from .config_editor import ConfigEditor
from .plugin_market_index import (
    MarketIndexError,
    market_install_target,
    parse_market_index_bytes,
)

__all__ = [
    "MarketFault",
    "PluginMarket",
    "install_plugin_market",
    "normalize_index_url",
]

MAX_LIMIT = 100
DEFAULT_FETCH_TIMEOUT_MS = 15000
DEFAULT_MAX_BYTES = 128 * 1024 * 1024
DEFAULT_CACHE_TTL_MS = 600_000


class MarketFault(Exception):
    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def normalize_index_url(text: str) -> str:
    trimmed = text.strip()
    try:
        parsed = urlparse(trimmed)
        if not parsed.scheme or not parsed.netloc:
            raise ValueError("not absolute")
        url = parsed.geturl()
    except ValueError as error:
        raise MarketFault(
            "plugin-market/invalid-index-url",
            f"Not an absolute URL: {text}",
            {"url": text, "reason": "not-absolute"},
        ) from error
    if parsed.scheme not in ("http", "https"):
        raise MarketFault(
            "plugin-market/invalid-index-url",
            f"Index URLs use http or https: {text}",
            {"url": text, "reason": "scheme"},
        )
    if parsed.username or parsed.password or "#" in trimmed:
        raise MarketFault(
            "plugin-market/invalid-index-url",
            f"Index URLs carry no credentials or fragment: {text}",
            {"url": text, "reason": "credentials-or-fragment"},
        )
    return url


def _match_text(plugin: dict, text: str) -> bool:
    query = text.strip().lower()
    if not query:
        return True
    words = [part for part in query.split() if part]
    haystacks: list[str] = [plugin["name"].lower()]
    if "author" in plugin:
        haystacks.append(plugin["author"].lower())
    for key in ("title", "description"):
        value = plugin.get(key)
        if isinstance(value, str):
            haystacks.append(value.lower())
        elif isinstance(value, dict):
            haystacks.extend(member.lower() for member in value.values())
    if "keywords" in plugin:
        haystacks.extend(keyword.lower() for keyword in plugin["keywords"])
    blob = " ".join(haystacks)
    return all(word in blob for word in words)


def _select_plugins(index: dict, query: dict) -> list[dict]:
    plugins = index["plugins"]
    category = query.get("category")
    labels = query.get("labels") or []
    text = query.get("text") or ""
    filtered = []
    for plugin in plugins:
        if category is not None and plugin.get("category") != category:
            continue
        plugin_labels = plugin.get("labels") or []
        if labels and not all(label in plugin_labels for label in labels):
            continue
        if not _match_text(plugin, text):
            continue
        filtered.append(plugin)
    sort = query.get("sort") or {"kind": "index"}
    kind = sort.get("kind") if isinstance(sort, dict) else "index"
    if kind == "published":
        direction = sort.get("direction", "desc")
        filtered.sort(
            key=lambda item: max(version["publishTimestamp"] for version in item["versions"]),
            reverse=direction != "asc",
        )
    elif kind == "relevance" and text.strip():
        needle = text.strip().lower()
        filtered.sort(
            key=lambda item: (
                0 if item["name"].lower().startswith(needle) else 1,
                item["name"].lower(),
            ),
        )
    return filtered


class PluginMarket(Service):
    provide = "pluginMarket"

    def __init__(
        self,
        ctx: Context,
        editor: ConfigEditor,
        *,
        entry_id: str = "plugin-market",
        indexes: list[str] | None = None,
        fetch_timeout_ms: int = DEFAULT_FETCH_TIMEOUT_MS,
        max_bytes: int = DEFAULT_MAX_BYTES,
        cache_ttl_ms: int = DEFAULT_CACHE_TTL_MS,
    ):
        super().__init__(ctx, "pluginMarket")
        self._editor = editor
        self._entry_id = entry_id
        self._indexes = list(indexes or [])
        self._fetch_timeout_ms = fetch_timeout_ms
        self._max_bytes = max_bytes
        self._cache_ttl_ms = cache_ttl_ms
        self._cache: dict[str, dict] = {}
        self._errors: dict[str, str] = {}

    def list_indexes(self) -> list[dict]:
        summaries = []
        for url in self._configured_urls():
            cached = self._cache.get(url)
            error = self._errors.get(url)
            item: dict[str, Any] = {"url": url}
            if cached is not None:
                index = cached["index"]
                item["title"] = index["title"]
                if "description" in index:
                    item["description"] = index["description"]
            if error is not None:
                item["error"] = error
            summaries.append(item)
        return summaries

    def set_indexes(self, urls: list[str]) -> None:
        configured = set(self._configured_urls())
        normalized: list[str] = []
        keys: list[str] = []
        for url in urls:
            kept = url if url in configured else normalize_index_url(url)
            normalized.append(kept)
            keys.append(urlparse(kept).geturl() if urlparse(kept).scheme else kept)
        for position, key in enumerate(keys):
            if key in keys[:position]:
                repeated = normalized[position]
                raise MarketFault(
                    "plugin-market/invalid-index-url",
                    f"Index source listed twice: {repeated}",
                    {"url": repeated, "reason": "duplicate"},
                )
        settings = self.ctx.get("settings")
        if settings is None:
            raise MarketFault(
                "plugin-market/settings-unavailable",
                "Index sources require the settings service and a profile entry",
                {},
            )
        run_on_resident(settings.update(self._entry_id, {"indexes": normalized}))
        self._indexes = normalized
        self._prune_cache()
        self.ctx.emit("plugin-market/changed")

    def search(self, url: str, query: dict) -> dict:
        offset = query.get("offset")
        limit = query.get("limit")
        if not isinstance(offset, int) or offset < 0:
            raise MarketFault(
                "plugin-market/invalid-query",
                "offset must be a non-negative integer",
                {"field": "offset", "reason": "range"},
            )
        if not isinstance(limit, int) or limit < 1 or limit > MAX_LIMIT:
            raise MarketFault(
                "plugin-market/invalid-query",
                f"limit must be an integer from 1 to {MAX_LIMIT}",
                {"field": "limit", "reason": "range"},
            )
        if url not in self._configured_urls():
            raise MarketFault(
                "plugin-market/unknown-index",
                f"Index source is not configured: {url}",
                {"url": url},
            )
        document = self._document(url, query.get("refresh") is True)
        matching = _select_plugins(document["index"], query)
        page = matching[offset: offset + limit]
        entries = [self._listing(plugin) for plugin in page]
        next_offset = offset + len(page)
        index = document["index"]
        result: dict[str, Any] = {
            "url": url,
            "title": index["title"],
            "metadata": index["metadata"],
            "fetchedAt": document["fetchedAt"],
            "entries": entries,
            "total": len(matching),
            "nextOffset": next_offset if next_offset < len(matching) else None,
        }
        if "description" in index:
            result["description"] = index["description"]
        return result

    def read_entry_detail(
        self,
        url: str,
        name: str,
        version: str,
        language: str,
        options: dict | None = None,
    ) -> dict:
        if url not in self._configured_urls():
            raise MarketFault(
                "plugin-market/unknown-index",
                f"Index source is not configured: {url}",
                {"url": url},
            )
        if not isinstance(language, str) or not language.strip():
            raise MarketFault(
                "plugin-market/invalid-query",
                "language must be a non-empty string",
                {"field": "language", "reason": "format"},
            )
        document = self._document(url, False)
        plugin = next((item for item in document["index"]["plugins"]
                       if item["name"] == name), None)
        listed = next((item for item in (plugin or {}).get("versions", [])
                       if item["version"] == version), None)
        if plugin is None or listed is None:
            raise MarketFault(
                "plugin-market/unknown-plugin",
                f"{url} lists no {name}@{version}",
                {"name": name, "version": version},
            )
        return {"status": "none"}

    def _configured_urls(self) -> list[str]:
        live = self._read_live_indexes()
        return live if live is not None else list(self._indexes)

    def _read_live_indexes(self) -> list[str] | None:
        for entry in self._editor.configuration():
            if entry["entry"].options.get("id") == self._entry_id:
                config = entry["entry"].options.get("config") or {}
                indexes = config.get("indexes")
                if isinstance(indexes, list):
                    return [item for item in indexes if isinstance(item, str)]
        return None

    def _prune_cache(self) -> None:
        configured = set(self._configured_urls())
        for url in list(self._cache):
            if url not in configured:
                del self._cache[url]
        for url in list(self._errors):
            if url not in configured:
                del self._errors[url]

    def _document(self, url: str, refresh: bool) -> dict:
        if not refresh:
            cached = self._cache.get(url)
            if cached is not None and cached["expiresAt"] > time.time() * 1000:
                return cached
        try:
            index = self._fetch_index(url)
        except MarketFault as error:
            if error.code == "plugin-market/fetch-failed":
                self._errors[url] = error.message
            raise
        fetched_at = int(time.time() * 1000)
        document = {"index": index, "fetchedAt": fetched_at}
        self._cache[url] = {
            **document,
            "expiresAt": fetched_at + self._cache_ttl_ms,
        }
        self._errors.pop(url, None)
        return document

    def _fetch_index(self, url: str) -> dict:
        timeout = self._fetch_timeout_ms / 1000
        try:
            with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                response = client.get(url, headers={"accept": "application/json"})
        except httpx.HTTPError as error:
            raise MarketFault(
                "plugin-market/fetch-failed",
                f"Could not fetch {url}: {error}",
                {"url": url, "reason": str(error)},
            ) from error
        if response.status_code < 200 or response.status_code >= 300:
            raise MarketFault(
                "plugin-market/fetch-failed",
                f"Could not fetch {url}: HTTP {response.status_code}",
                {"url": url, "reason": f"HTTP {response.status_code}"},
            )
        body = response.content
        if len(body) > self._max_bytes:
            raise MarketFault(
                "plugin-market/fetch-failed",
                f"Could not fetch {url}: body exceeds {self._max_bytes} bytes",
                {"url": url, "reason": f"body exceeds {self._max_bytes} bytes"},
            )
        try:
            return parse_market_index_bytes(body, url)
        except MarketIndexError as error:
            raise MarketFault(
                "plugin-market/invalid-index",
                f"{url}: {error}",
                {"url": url, "path": error.path, "reason": error.reason},
            ) from error

    def _listing(self, plugin: dict) -> dict:
        versions = []
        for version in plugin["versions"]:
            versions.append({
                **version,
                "install": market_install_target(plugin["name"], version),
                "compatibility": {"compatible": True},
            })
        default_version = versions[0]["version"] if versions else None
        installation = self._installation(plugin["name"], default_version or "")
        entry = {key: value for key, value in plugin.items() if key != "versions"}
        entry["versions"] = versions
        if default_version is not None:
            entry["defaultVersion"] = default_version
        entry["installation"] = installation
        return entry

    def _installation(self, name: str, version: str) -> dict:
        manager = self.ctx.get("pluginManager")
        if manager is None:
            return {"status": "not-installed"}
        for row in manager.list_plugins():
            patch_id = row.get("patchId")
            module_name = row.get("moduleName")
            if patch_id == name or (isinstance(module_name, str) and module_name.endswith(name)):
                if row.get("enabled"):
                    return {"status": "installed", "installedVersion": version}
                return {"status": "not-installed"}
        return {"status": "not-installed"}


def install_plugin_market(
    ctx: Context,
    config_editor: ConfigEditor,
    **config: Any,
) -> PluginMarket:
    existing = ctx.get("pluginMarket")
    if existing is not None:
        return existing
    indexes = config.get("indexes")
    service = PluginMarket(
        ctx,
        config_editor,
        entry_id=str(config.get("entry_id") or "plugin-market"),
        indexes=list(indexes) if isinstance(indexes, list) else [],
        fetch_timeout_ms=int(config.get("fetchTimeoutMs") or DEFAULT_FETCH_TIMEOUT_MS),
        max_bytes=int(config.get("maxBytes") or DEFAULT_MAX_BYTES),
        cache_ttl_ms=int(config.get("cacheTtlMs") or DEFAULT_CACHE_TTL_MS),
    )
    return service
