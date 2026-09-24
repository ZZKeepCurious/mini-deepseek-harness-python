"""DeepSeek 搜索 provider（上游 packages/web/web-search-deepseek/src/provider.ts）。

一次搜索 = 一次 Anthropic 兼容 Messages API 调用（原生 ``web_search_20250305``
server tool），返回结构化 result block；无 result block 是错误而非散文兜底。
wire 格式与 HTTP 客户端是 provider 私有的，不经过 ctx.llm。

载体差异（登记 verified-diffs）：
  * 凭据面：上游经 credentials 服务 / settings 区解析；mini 用 env +
    字面配置（先例 llm/deepseek.py）。resolveApiKey 同步，无 abortable race。
  * recordRequest（``web/deepseek-search-llm-request`` 会话事件）不复现。
  * 重定向拒绝：上游 ``redirect: 'error'`` 使 fetch 抛错；mini 手动判定 3xx
    后抛错（undici 具体错误串不同，模型可见消息前缀一致，端点指引文案一致）。
  * abort 传播：轮询 ``signal.is_set()``（0.01s 一轮）后取消 in-flight 请求。
"""
from __future__ import annotations

import asyncio
import json
from urllib.parse import urlsplit

import httpx

from .types import WebError, is_set_signal

__all__ = [
    "DEEPSEEK_DEFAULT_API_VERSION",
    "DEEPSEEK_DEFAULT_BASE_URL",
    "DEEPSEEK_DEFAULT_MAX_TOKENS",
    "DEEPSEEK_DEFAULT_MAX_USES",
    "DEEPSEEK_DEFAULT_MODEL",
    "DEEPSEEK_PROVIDER_ID",
    "DEEPSEEK_SEARCH_BASE_URL_ENV",
    "DeepSeekSearchProvider",
    "citation_snippets",
    "map_anthropic_response",
]

DEEPSEEK_PROVIDER_ID = "deepseek-official"
DEEPSEEK_DEFAULT_BASE_URL = "https://api.deepseek.com/anthropic/v1"
DEEPSEEK_DEFAULT_MODEL = "deepseek-v4-flash"
DEEPSEEK_DEFAULT_API_VERSION = "2023-06-01"
DEEPSEEK_DEFAULT_MAX_TOKENS = 4096
DEEPSEEK_DEFAULT_MAX_USES = 5
DEEPSEEK_SEARCH_BASE_URL_ENV = "DEEPSEEK_SEARCH_BASE_URL"
DEEPSEEK_API_KEY_ENV = "DEEPSEEK_API_KEY"

#: 每次请求携带的归属头（provider.ts:49，随包版本递增）。
SEARCH_USER_AGENT = "deepseek-harness/0.0.1"

_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})


def citation_snippets(blocks: list[dict]) -> dict[str, str]:
    """从每个 text block 的 citations[] 装配 url → cited_text 映射（首次出现胜）。

    Anthropic ``web_search_result`` 项通常不含内联摘要，摘要在独立 text block
    的 citation 里，按 url 键取（provider.ts:120）。
    """
    mapping: dict[str, str] = {}
    for block in blocks:
        if block.get("type") != "text":
            continue
        for cite in block.get("citations") or []:
            url = cite.get("url")
            text = cite.get("cited_text")
            if url and len(url) > 0 and text and len(text) > 0 and url not in mapping:
                mapping[url] = text
    return mapping


def map_anthropic_response(response: dict) -> dict:
    """把 DeepSeek Anthropic Messages 响应归一为 WebSearchResult（provider.ts:144）。

    按 url 去重；缺席可选字段一律省略；`truncated` 恒 false（web 服务在
    seam 层做最终 maxResults 截断）。无 result block 即抛 WEB_PROVIDER_ERROR。
    """
    blocks = response.get("content") or []
    result_blocks = [b for b in blocks if b.get("type") == "web_search_tool_result"]
    if not result_blocks:
        raise WebError(
            "DeepSeek returned no web_search_tool_result blocks; the request may not "
            "have triggered native web search",
            "WEB_PROVIDER_ERROR",
        )
    snippets = citation_snippets(blocks)
    seen: set[str] = set()
    sources: list[dict] = []
    for block in result_blocks:
        for item in block.get("content") or []:
            if item.get("type") != "web_search_result":
                continue
            url = item.get("url")
            if not url or not len(url) or url in seen:
                continue
            seen.add(url)
            source: dict = {"url": url}
            title = item.get("title")
            snippet = snippets.get(url)
            page_age = item.get("page_age")
            if title and len(title) > 0:
                source["title"] = title
            if snippet and len(snippet) > 0:
                source["snippet"] = snippet
            if page_age and len(page_age) > 0:
                source["publishedAt"] = page_age
            sources.append(source)
    return {"sources": sources, "truncated": False}


def search_endpoint_error(endpoint: str, message: str) -> WebError:
    """给 dispatch 之后出现的失败附加端点恢复指引（provider.ts:312）。"""
    return WebError(
        f"{message}\n\nThe web search request used endpoint {json.dumps(endpoint)}. "
        "Search endpoint configuration is separate from chat. If that endpoint is not "
        "intended, guide the user to Settings > Plugins > Plugin configuration > Web "
        "search, where they can change and save Endpoint. If that settings page is "
        "unavailable, the user can set DEEPSEEK_SEARCH_BASE_URL or configure "
        "web-search-deepseek.baseURL to a trusted Anthropic-compatible Messages API "
        "base. Only the user should choose or change the endpoint.",
        "WEB_PROVIDER_ERROR",
    )


def search_aborted() -> WebError:
    """provider 的稳定取消错误（provider.ts:355 'DeepSeek search aborted'）。"""
    return WebError("DeepSeek search aborted", "WEB_ABORTED")


class DeepSeekSearchProvider:
    """DeepSeek 搜索 provider。options 经 thunk 在每次操作入口快照一次——设置区
    变更无需重注册，一次搜索绝不混用两个 section（provider.ts:179）。"""

    id = DEEPSEEK_PROVIDER_ID

    def __init__(self, resolve_options):
        self._resolve_options = resolve_options

    def available(self) -> bool:
        options = self._resolve_options()
        return (
            _url_parseable(options["baseURL"])
            and _positive_int(options["maxTokens"])
            and _positive_int(options["maxUses"])
        )

    async def search(self, request: dict, signal=None) -> dict:
        options = self._resolve_options()
        api_key = self._api_key(options)
        _throw_if_aborted(signal)
        endpoint = f"{options['baseURL']}/messages"
        body = {
            "model": options["model"],
            "max_tokens": options["maxTokens"],
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "text",
                    "text": f"Perform a web search for the query: {request['query']}",
                }],
            }],
            "tools": [{
                "type": "web_search_20250305",
                "name": "web_search",
                "max_uses": options["maxUses"],
            }],
        }
        try:
            response = await self._post_messages(endpoint, body, options, signal)
        except WebError:
            raise
        except Exception as error:  # noqa: BLE001 - dispatch 阶段失败
            if is_set_signal(signal):
                raise search_aborted() from error
            raise search_endpoint_error(
                endpoint,
                f"DeepSeek search request failed: {type(error).__name__}: {error}",
            ) from error

        if not 200 <= response["statusCode"] < 300:
            message = f"DeepSeek API error (HTTP {response['statusCode']})"
            detail = _error_detail(response["bodyBytes"])
            if detail:
                message += f": {detail}"
            raise search_endpoint_error(endpoint, message)

        try:
            payload = json.loads(response["bodyBytes"].decode("utf-8"))
            return map_anthropic_response(payload)
        except WebError as error:
            raise search_endpoint_error(endpoint, str(error)) from error
        except (ValueError, UnicodeDecodeError) as error:
            raise search_endpoint_error(
                endpoint,
                f"DeepSeek returned an unprocessable response body: "
                f"{type(error).__name__}: {error}",
            ) from error

    def _api_key(self, options: dict) -> str:
        literal = options.get("apiKey") or ""
        if len(literal) > 0:
            return literal
        ref = options.get("apiKeyEnv") or DEEPSEEK_API_KEY_ENV
        resolved = options.get("resolveApiKey")()
        if resolved and len(resolved) > 0:
            return resolved
        raise WebError(
            f'DeepSeek search has no API key for "{ref}"; store it through the '
            "credentials service (the web Models page writes it), export it in the "
            'launching environment, or set a literal "apiKey" in the '
            "web-search-deepseek config",
            "WEB_PROVIDER_CREDENTIAL_MISSING",
        )

    async def _post_messages(self, endpoint: str, body: dict, options: dict, signal) -> dict:
        headers = {
            "x-api-key": options["apiKey"],
            "authorization": f"Bearer {options['apiKey']}",
            "anthropic-version": options["apiVersion"],
            "content-type": "application/json",
            "accept": "application/json",
            "user-agent": SEARCH_USER_AGENT,
        }

        async def _do():
            async with httpx.AsyncClient(timeout=None, follow_redirects=False,
                                         trust_env=False) as client:
                response = await client.post(endpoint, headers=headers, json=body)
                if response.status_code in _REDIRECT_STATUSES:
                    raise RuntimeError(
                        f"web request rejected redirect (HTTP {response.status_code})")
                payload = await response.aread()
                return {"statusCode": response.status_code, "bodyBytes": payload}

        task = asyncio.create_task(_do())
        while not task.done():
            if is_set_signal(signal):
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
                raise search_aborted()
            await asyncio.sleep(0.01)
        return task.result()


def _throw_if_aborted(signal) -> None:
    if is_set_signal(signal):
        raise search_aborted()


def _error_detail(raw: bytes) -> str | None:
    """从错误响应体抽取 provider detail（provider.ts:251-258 的解析语义）。"""
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None
    if not isinstance(parsed, dict):
        return None
    detail = parsed.get("error")
    if isinstance(detail, str) and len(detail) > 0:
        return detail
    if isinstance(detail, dict) and isinstance(detail.get("message"), str) \
            and len(detail["message"]) > 0:
        return detail["message"]
    if isinstance(parsed.get("message"), str) and len(parsed["message"]) > 0:
        return parsed["message"]
    return None


def _url_parseable(value: str) -> bool:
    try:
        parts = urlsplit(value)
        return bool(parts.scheme) and bool(parts.hostname)
    except ValueError:
        return False


def _positive_int(value: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0