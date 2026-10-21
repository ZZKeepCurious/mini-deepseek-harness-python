"""DeepSeek Search / Fetch API providers（packages/web/web-deepseek-api）。

一次操作一个直连请求、无辅助模型请求：`deepseek-search-api` 返回
`{sources, truncated:false}`，`deepseek-fetch-api` 返回页面 Markdown 文本体。
认证用 `Authorization: Bearer <key>`（literal key 优先，否则 apiKeyEnv）；账户
token 与浏览器宿主在此载体不可用（登记于 verified-diffs）。
"""
from __future__ import annotations

import os

import httpx

from .types import WebError, is_set_signal

__all__ = [
    "DEEPSEEK_FETCH_API_PROVIDER_ID",
    "DEEPSEEK_SEARCH_API_PROVIDER_ID",
    "DEEPSEEK_WEB_API_BASE_URL",
    "DeepSeekFetchApiProvider",
    "DeepSeekSearchApiProvider",
]

DEEPSEEK_SEARCH_API_PROVIDER_ID = "deepseek-search-api"
DEEPSEEK_FETCH_API_PROVIDER_ID = "deepseek-fetch-api"
DEEPSEEK_WEB_API_BASE_URL = "https://api.deepseek.com/v1/tools"


def _credential(options: dict) -> str:
    literal = options.get("apiKey")
    if isinstance(literal, str) and literal:
        return literal
    env_name = options.get("apiKeyEnv") or "DEEPSEEK_API_KEY"
    value = os.environ.get(env_name)
    return value if isinstance(value, str) else ""


def _valid_base_url(base_url: object) -> bool:
    if not isinstance(base_url, str) or not base_url:
        return False
    try:
        parsed = httpx.URL(base_url)
    except (TypeError, ValueError):
        return False
    return parsed.scheme in ("http", "https") and bool(parsed.host)


class DeepSeekSearchApiProvider:
    id = DEEPSEEK_SEARCH_API_PROVIDER_ID

    def __init__(self, resolve_options, transport: httpx.AsyncBaseTransport | None = None):
        self._resolve_options = resolve_options
        self._transport = transport

    def available(self) -> bool:
        options = self._resolve_options()
        count = options.get("count")
        return (bool(_credential(options)) and _valid_base_url(options.get("baseURL"))
                and isinstance(count, int) and not isinstance(count, bool)
                and 1 <= count <= 50)

    async def search(self, request: dict, signal=None) -> dict:
        options = self._resolve_options()
        if not self.available():
            raise WebError(
                "DeepSeek Search API is unavailable: missing credential or invalid baseURL/count",
                "WEB_PROVIDER_CREDENTIAL_MISSING")
        if is_set_signal(signal):
            raise WebError("web search was aborted", "WEB_ABORTED")
        base_url = str(options["baseURL"]).rstrip("/")
        endpoint = f"{base_url}/search"
        count = options["count"]
        max_results = request.get("maxResults")
        body = {
            "query": request.get("query"),
            "count": min(count, max_results) if isinstance(max_results, int) else count,
            "need_content": False,
        }
        payload = await self._dispatch(options, endpoint, body, signal)
        results = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(results, list):
            raise WebError(
                f"DeepSeek Search API returned an invalid response from {endpoint}",
                "WEB_PROVIDER_ERROR")
        sources = []
        for item in results:
            if (not isinstance(item, dict) or not isinstance(item.get("url"), str)
                    or not item["url"] or not isinstance(item.get("title"), str)
                    or not isinstance(item.get("snippet"), str)
                    or (item.get("date") is not None and not isinstance(item["date"], str))):
                raise WebError(
                    f"DeepSeek Search API returned an invalid response from {endpoint}",
                    "WEB_PROVIDER_ERROR")
            source = {"url": item["url"]}
            if item["title"]:
                source["title"] = item["title"]
            if item["snippet"]:
                source["snippet"] = item["snippet"]
            if isinstance(item.get("date"), str) and item["date"]:
                source["publishedAt"] = item["date"]
            sources.append(source)
        return {"sources": sources, "truncated": False}

    async def _dispatch(self, options: dict, endpoint: str, body: dict, signal) -> object:
        key = _credential(options)
        try:
            async with httpx.AsyncClient(
                    transport=self._transport, timeout=60.0,
                    follow_redirects=False) as client:
                response = await client.post(
                    endpoint, json=body,
                    headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as error:
            raise WebError(
                f"DeepSeek API request to {endpoint} failed: {error}",
                "WEB_PROVIDER_ERROR") from error
        if is_set_signal(signal):
            raise WebError("web request was aborted", "WEB_ABORTED")
        if 300 <= response.status_code < 400:
            raise WebError(
                f"DeepSeek API request to {endpoint} was redirected; redirects are rejected",
                "WEB_PROVIDER_ERROR")
        if response.status_code != 200:
            raise WebError(
                f"DeepSeek API request to {endpoint} failed with HTTP {response.status_code}; "
                "check the web-deepseek-api baseURL setting",
                "WEB_PROVIDER_ERROR")
        try:
            return response.json()
        except ValueError as error:
            raise WebError(
                f"DeepSeek API returned a non-JSON response from {endpoint}",
                "WEB_PROVIDER_ERROR") from error


class DeepSeekFetchApiProvider:
    id = DEEPSEEK_FETCH_API_PROVIDER_ID

    def __init__(self, resolve_options, transport: httpx.AsyncBaseTransport | None = None):
        self._resolve_options = resolve_options
        self._transport = transport

    def available(self) -> bool:
        options = self._resolve_options()
        return bool(_credential(options)) and _valid_base_url(options.get("baseURL"))

    async def fetch(self, request: dict, signal=None) -> dict:
        options = self._resolve_options()
        if not self.available():
            raise WebError(
                "DeepSeek Fetch API is unavailable: missing credential or invalid baseURL",
                "WEB_PROVIDER_CREDENTIAL_MISSING")
        if is_set_signal(signal):
            raise WebError("web fetch was aborted", "WEB_ABORTED")
        url = request.get("url")
        endpoint = f"{str(options['baseURL']).rstrip('/')}/fetch"
        key = _credential(options)
        try:
            async with httpx.AsyncClient(
                    transport=self._transport, timeout=60.0,
                    follow_redirects=False) as client:
                response = await client.post(
                    endpoint, json={"url": url},
                    headers={"Authorization": f"Bearer {key}"})
        except httpx.HTTPError as error:
            raise WebError(
                f"DeepSeek API request to {endpoint} failed: {error}",
                "WEB_PROVIDER_ERROR") from error
        if is_set_signal(signal):
            raise WebError("web fetch was aborted", "WEB_ABORTED")
        if 300 <= response.status_code < 400:
            raise WebError(
                f"DeepSeek API request to {endpoint} was redirected; redirects are rejected",
                "WEB_PROVIDER_ERROR")
        if response.status_code != 200:
            raise WebError(
                f"DeepSeek API request to {endpoint} failed with HTTP {response.status_code}; "
                "check the web-deepseek-api baseURL setting",
                "WEB_PROVIDER_ERROR")
        try:
            payload = response.json()
        except ValueError as error:
            raise WebError(
                f"DeepSeek API returned a non-JSON response from {endpoint}",
                "WEB_PROVIDER_ERROR") from error
        if not isinstance(payload, dict) or not isinstance(payload.get("content"), str):
            raise WebError(
                f"DeepSeek Fetch API returned an invalid response from {endpoint}",
                "WEB_PROVIDER_ERROR")
        return {
            "url": url,
            "statusCode": 200,
            "body": {"kind": "text", "content": payload["content"]},
            "truncated": False,
        }