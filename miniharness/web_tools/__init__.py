"""web 模型工具族装配面（上游 base 组合：dsh-web + dsh-web-search-deepseek
+ dsh-web-fetch-http + dsh-tool-web，见 bundle/base/cordis.patch.yml:440-470）。

`install_web(ctx, config)` 是唯一装配入口，在 core 服务装配后调用一次：提供
  * ``ctx.web``：WebRuntime，注册 deepseek-official 搜索 + http 抓取两 provider；
  * 解析后的 ``WebConfig`` 挂在 runtime 上，供 ``register_web_tools`` 复用同一份。
`register_web_tools(reg, web, system_prompt, config)` 按开关注册模型面对
web_search/web_fetch 工具与 ``tool:web_search``/``tool:web_fetch`` 节（工具与
节都在工具开启时注册；关闭时不注册空节——上游恒注册但文本返回空串，等价简化）。

默认组合镜像 base 值，产品可传 ``WebConfig`` 覆盖。工具节排序锚点采用上游
system-prompt SECTION_ORDERS 字面量（TOOL_WEB_SEARCH: 2000 / TOOL_WEB_FETCH: 2100，
mini 无 getSectionOrder，见 mcp/server_context.py 同款惯例）。
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from ..core.scope import Context
from .fetch_http import HttpFetchProvider, build_http_limits
from .fetch_tool import web_fetch_section_text, web_fetch_tool
from .runtime import WebRuntime, WebRuntimeConfig
from .search_deepseek import (
    DEEPSEEK_DEFAULT_API_VERSION,
    DEEPSEEK_DEFAULT_BASE_URL,
    DEEPSEEK_DEFAULT_MAX_TOKENS,
    DEEPSEEK_DEFAULT_MAX_USES,
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_PROVIDER_ID,
    DEEPSEEK_SEARCH_BASE_URL_ENV,
    DeepSeekSearchProvider,
)
from .search_tool import web_search_section_text, web_search_tool

__all__ = [
    "WEB_FETCH_SECTION_ORDER",
    "WEB_SEARCH_SECTION_ORDER",
    "WebConfig",
    "install_web",
    "register_web_tools",
]

WEB_SEARCH_SECTION_ORDER = 2000
WEB_FETCH_SECTION_ORDER = 2100

#: 缺省搜索 provider（base cordis.patch.yml web 行）。
DEFAULT_FETCH_PROVIDER_ID = "http"


@dataclass
class WebConfig:
    """web 工具族装配配置；缺省 = base 组合的解析结果。

    `search_timeout_ms` 缺省 60000 而不是 tool-web 默认 30000——base 对
    DeepSeek 搜索路线给了 60s（搜索是一次完整辅助模型请求，服务端检索）。
    """

    search: bool = True
    fetch: bool = True
    search_max_results: int = 8
    search_max_queries: int = 4
    fetch_timeout_ms: int = 30000
    search_timeout_ms: int = 60000
    fetch_max_output_chars: int = 200000
    search_provider: str = DEEPSEEK_PROVIDER_ID
    fetch_provider: str = DEFAULT_FETCH_PROVIDER_ID
    search_api_key_env: str = "DEEPSEEK_API_KEY"
    search_api_key: str | None = None
    search_base_url: str | None = None
    search_model: str = DEEPSEEK_DEFAULT_MODEL
    search_api_version: str = DEEPSEEK_DEFAULT_API_VERSION
    search_max_tokens: int = DEEPSEEK_DEFAULT_MAX_TOKENS
    search_max_uses: int = DEEPSEEK_DEFAULT_MAX_USES
    fetch_http_config: dict | None = field(default=None)


def install_web(ctx: Context, config: WebConfig | None = None) -> WebRuntime:
    """装配 ctx.web 与两 provider（一次调用；工具/节由 register_web_tools 接）。

    @param ctx - 已装配 core 服务的 Context（提供 ctx.provide）。
    @param config - 装配配置；缺省为 base 组合默认值。
    @returns 装配出的 WebRuntime（`tool_config` 挂解析后配置）。
    """
    cfg = config or WebConfig()
    _assert_positive_integer("searchMaxResults", cfg.search_max_results)
    _assert_positive_integer("searchMaxQueries", cfg.search_max_queries)
    _assert_positive_integer("fetchTimeoutMs", cfg.fetch_timeout_ms)
    _assert_positive_integer("searchTimeoutMs", cfg.search_timeout_ms)
    _assert_positive_integer("fetchMaxOutputChars", cfg.fetch_max_output_chars)

    runtime = WebRuntime(WebRuntimeConfig(cfg.search_provider, cfg.fetch_provider))
    runtime.register_search_provider(DeepSeekSearchProvider(_search_options(cfg)))
    runtime.register_fetch_provider(HttpFetchProvider(build_http_limits(cfg.fetch_http_config)))
    ctx.provide("web", runtime)
    runtime.tool_config = cfg
    return runtime


def register_web_tools(reg, web: WebRuntime, system_prompt=None,
                       config: WebConfig | None = None) -> None:
    """把模型面对工具与节注册到工具注册表（default_tools/web 装配在 install_web 后调用）。

    @param reg - 工具注册表（core.tools.ToolRegistry）。
    @param web - install_web 装配的 WebRuntime。
    @param system_prompt - 可选的 systemPrompt 服务；缺省跳过节注册（简化登记）。
    @param config - 装配配置；缺省取 web.tool_config，再无则 WebConfig() 默认。
    """
    cfg = config
    if cfg is None:
        cfg = getattr(web, "tool_config", None) or WebConfig()
    if cfg.search:
        reg.register(web_search_tool(
            web, cfg.search_max_results, cfg.search_max_queries, cfg.search_timeout_ms))
        if system_prompt is not None:
            system_prompt.section(
                "tool:web_search",
                WEB_SEARCH_SECTION_ORDER,
                web_search_section_text(cfg.fetch, cfg.search_max_queries),
            )
    if cfg.fetch:
        reg.register(web_fetch_tool(web, cfg.fetch_timeout_ms, cfg.fetch_max_output_chars))
        if system_prompt is not None:
            system_prompt.section(
                "tool:web_fetch",
                WEB_FETCH_SECTION_ORDER,
                web_fetch_section_text(cfg.search),
            )


def _search_options(cfg: WebConfig):
    """构造 DeepSeek 搜索 provider 的 options thunk（每次操作入口快照一次）。

    baseURL 优先级：配置 search_base_url → 环境 DEEPSEEK_SEARCH_BASE_URL →
    默认端点；apiKey 优先级：字面配置 → 环境 DEEPSEEK_API_KEY。
    """
    base_url = (
        cfg.search_base_url
        or os.environ.get(DEEPSEEK_SEARCH_BASE_URL_ENV)
        or DEEPSEEK_DEFAULT_BASE_URL
    )
    api_key_env = cfg.search_api_key_env
    literal_key = cfg.search_api_key or ""

    def resolve() -> dict:
        return {
            "baseURL": base_url,
            "model": cfg.search_model,
            "apiVersion": cfg.search_api_version,
            "maxTokens": cfg.search_max_tokens,
            "maxUses": cfg.search_max_uses,
            "apiKey": literal_key,
            "apiKeyEnv": api_key_env,
            "resolveApiKey": lambda: os.environ.get(api_key_env) or "",
        }

    return resolve


def _assert_positive_integer(name: str, value: int) -> None:
    if not (isinstance(value, int) and not isinstance(value, bool) and value >= 1):
        raise ValueError(f"tool-web: {name} must be a positive integer")