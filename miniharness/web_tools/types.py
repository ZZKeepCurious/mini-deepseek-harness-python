"""web 能力 seam 的词汇与错误集（上游 packages/web/web/src/types.ts）。

四个"包"agreed case：web-web（应用 seam 词汇）、web-fetch-http、web-search-deepseek、
tool-web。本模块承载 seam 词汇（请求/结果/provider 协议）与全部 WebError 码；
各 provider/工具模块 import 本模块，跨模块不重复定义。

数据集契约（与上游 types.ts 一致）：
  * WebSearchRequest = {"query", "maxResults?"}
  * WebSearchSource = {"url", "title?", "snippet?", "publishedAt?"}
  * WebSearchResult = {"content?", "sources", "truncated"}
  * WebFetchRequest = {"url"}
  * WebFetchBody    = {"kind": "html"|"text", "content"}
  * WebFetchResult  = {"url", "statusCode", "body", "truncated"}
canonical 值一律省略缺席可选字段（与上游 `...spread` 投影一致）。
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = [
    "WebError",
    "WebFetchRequest",
    "WebFetchResult",
    "WebSearchRequest",
    "WebSearchResult",
    "WebSearchSource",
    "is_set_signal",
]


class WebError(ValueError):
    """web seam 结构化错误（上游 WebError extends HarnessError）：message + code。

    code 为开放字符串（与上游一致），描述不支持时容忍 provider 专属码。
    共享码覆盖：WEB_PROVIDER_CONFIGURED_MISSING / _UNAVAILABLE /
    WEB_PROVIDER_UNAVAILABLE / WEB_PROVIDER_AMBIGUOUS / WEB_DUPLICATE_PROVIDER /
    WEB_ABORTED / WEB_PROVIDER_ERROR；fetch provider 另含 INVALID_URL /
    BLOCKED_URL / REDIRECT_BLOCKED / FETCH_TOO_LARGE / FETCH_TIMEOUT /
    UNSUPPORTED_CONTENT_TYPE；search provider 追加 WEB_PROVIDER_CREDENTIAL_MISSING。
    """

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code


def is_set_signal(signal: Any) -> bool:
    """统一读取执行期取消信号（threading.Event / FusedSignal 均提供 is_set）。"""
    return bool(signal is not None and signal.is_set())


@runtime_checkable
class SearchSignal(Protocol):
    """provider 收到的取消信号最小接口（上游 AbortSignal 的 mini 替身）。

    core.tools threading.Event 与 FusedSignal 天然满足；异步侧轮询 is_set()
    等效于上游 abort 事件（见 AGENTS 差异清单：事件订阅→轮询）。
    """

    def is_set(self) -> bool: ...


class WebSearchProvider(Protocol):
    """search 能力提供者（上游 WebSearchProvider）。id 在 search 域内唯一。"""

    id: str

    def available(self) -> bool:
        """廉价本地可用性检查；不得发起网络调用。"""

    async def search(self, request: dict, signal: SearchSignal | None = None) -> dict:
        """执行一次搜索；请求 shape = WebSearchRequest，结果 shape = WebSearchResult。"""


class WebFetchProvider(Protocol):
    """fetch 能力提供者（上游 WebFetchProvider）。id 在 fetch 域内唯一。"""

    id: str

    def available(self) -> bool:
        """廉价本地可用性检查；不得发起网络调用。"""

    async def fetch(self, request: dict, signal: SearchSignal | None = None) -> dict:
        """取回一个 URL；非 2xx 响应是结果而非抛错。结果 shape = WebFetchResult。"""