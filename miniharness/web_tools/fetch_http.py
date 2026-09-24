"""匿名公网 HTTP(S) 抓取 provider（上游 packages/web/web-fetch-http）。

读取侧与传输侧全部在此：br>一个 provider 实体 ``HttpFetchProvider``：
钉桩公网地址 → 逐跳新建独立连接池 → 只跟随同源重定向（预算前置）→
字节帽优先于字符帽（TextDecoder 语义）。provider 无凭据，永远可用。

载体差异（登记 verified-diffs）：
  * 上游 per-request 建 ``undici Agent`` + ``connect.lookup`` 钉桩 → mini 继承
    ``httpcore.AnyIOBackend`` 覆写 ``connect_tcp`` 丢弃 host 直连钉桩地址
    （SNI/证书主机名仍是 origin hostname——httpcore start_tls 用
    ``origin.host`` + httpx 仅在 IP 字面量 origin 时传 ``sni_hostname``）。
    替换 ``httpx.AsyncHTTPTransport._pool`` 注入自建 ``AsyncConnectionPool``
    （httpx 0.28 无 ``pool=`` 参数）。httpcore/anyio 为硬依赖。
  * 代理面（``proxyRouteFor``/``requestVia``）不复现：mini 永远直连钉桩。
  * ``raceWithSignal`` 事件订阅 → 轮询 ``signal.is_set()``（0.01s 一轮）。
  * 传输层错误字符串因载体不同：``web fetch failed: {Type}: {Message}``
    对齐 JS ``${String(error)}``（Error → "Type: message"）的结构。
"""
from __future__ import annotations

import asyncio
import math
import time
from dataclasses import dataclass
from typing import Any, Awaitable, Sequence, TypeVar, cast

import anyio
import httpx
from httpcore import AsyncConnectionPool, ConnectError, ConnectTimeout
from httpcore._backends.anyio import AnyIOBackend, AnyIOStream

from .network import PublicAddress, resolve_public_addresses
from .types import SearchSignal, WebError, is_set_signal
from .policy import (
    FetchUrl,
    classify_content_type,
    decoder_for_charset,
    is_same_origin,
    parse_charset,
    validate_fetch_url,
)

__all__ = [
    "DEFAULT_USER_AGENT",
    "HttpFetchLimits",
    "HttpFetchProvider",
    "MAX_NODE_TIMER_DELAY_MS",
    "PinnedNetworkBackend",
    "build_http_limits",
    "run_deadlined",
]

DEFAULT_USER_AGENT = "deepseek-harness/0.0.1 (+https://github.com/deepseek-ai)"
MAX_NODE_TIMER_DELAY_MS = 2_147_483_647

_ACCEPT_HEADER = "text/html,application/xhtml+xml,text/*;q=0.9,application/json;q=0.8"
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
T = TypeVar("T")


@dataclass
class HttpFetchLimits:
    """解析后的传输与限额（provider.ts:19）。"""

    max_response_bytes: int
    max_body_chars: int
    timeout_ms: int
    max_redirects: int
    user_agent: str


def build_http_limits(config: dict | None) -> HttpFetchLimits:
    """对照 index.ts Config 默认 + apply 校验。"""
    config = config or {}
    resolved = {
        "maxResponseBytes": config.get("maxResponseBytes", 5_000_000),
        "maxBodyChars": config.get("maxBodyChars", 100_000),
        "timeoutMs": config.get("timeoutMs", 30_000),
        "maxRedirects": config.get("maxRedirects", 5),
        "userAgent": config.get("userAgent", DEFAULT_USER_AGENT),
    }
    _assert_positive_finite("maxResponseBytes", resolved["maxResponseBytes"])
    _assert_positive_finite("maxBodyChars", resolved["maxBodyChars"])
    _assert_positive_finite("timeoutMs", resolved["timeoutMs"])
    if resolved["timeoutMs"] > MAX_NODE_TIMER_DELAY_MS:
        raise ValueError(f"web-fetch-http: timeoutMs must be no greater than {MAX_NODE_TIMER_DELAY_MS}")
    _assert_non_negative_integer("maxRedirects", resolved["maxRedirects"])
    return HttpFetchLimits(
        max_response_bytes=int(resolved["maxResponseBytes"]),
        max_body_chars=int(resolved["maxBodyChars"]),
        timeout_ms=int(resolved["timeoutMs"]),
        max_redirects=int(resolved["maxRedirects"]),
        user_agent=str(resolved["userAgent"]),
    )


def _assert_positive_finite(name: str, value: Any) -> None:
    if not (isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(float(value)) and value > 0):
        raise ValueError(f"web-fetch-http: {name} must be a positive finite number")


def _assert_non_negative_integer(name: str, value: Any) -> None:
    if not (isinstance(value, int) and not isinstance(value, bool) and value >= 0):
        raise ValueError(f"web-fetch-http: {name} must be a non-negative integer")


class PinnedNetworkBackend(AnyIOBackend):
    """只连接已验证地址集的后端：忽略 host，逐个钉桩地址直连（network.ts createPinnedLookup → requestPinned）。"""

    def __init__(self, addresses: Sequence[PublicAddress]):
        self._addresses = list(addresses)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        last_error: Exception | None = None
        for entry in self._addresses:
            try:
                with anyio.fail_after(timeout):
                    stream = await anyio.connect_tcp(
                        remote_host=entry.address,
                        remote_port=port,
                        local_host=local_address,
                    )
                for option in socket_options or []:
                    stream._raw_socket.setsockopt(*option)  # type: ignore[attr-defined]
                return AnyIOStream(cast(Any, stream))
            except TimeoutError as error:
                last_error = ConnectTimeout(error)
            except (OSError, anyio.BrokenResourceError) as error:
                last_error = ConnectError(error)
        raise last_error or ConnectError(f"no validated address for {host}")


async def run_deadlined(work: Awaitable[T], timeout_ms: float, signal: SearchSignal | None) -> T:
    """在 timeout/signal 双追求下执行异步工作，分类结局（provider.ts deadline + translateAbortOrNetwork）。

    结局语义与上游一致：
      * 单调时间到限 → ``WEB_FETCH_TIMEOUT`` ``web fetch timed out``；
      * signal 置位 → ``WEB_ABORTED`` ``web fetch aborted``；
      * 工作自身成功 → 返回其值；
      * 工作抛出 WebError → 原样重抛（已完成分类，重定向/限额等）；
      * 工作抛其它异常 → signal 置位归并到 aborted，否则
        ``WEB_PROVIDER_ERROR`` ``web fetch failed: {Type}: {Message}``。
    平局（timer 与工作同时完成）timer 优先——上游 deadline 与响应到达
    同刻时 fetch 仍以 abort 收场。
    """
    work_task = asyncio.create_task(work)
    monitor_task = asyncio.create_task(_monitor(timeout_ms, signal))
    done, _ = await asyncio.wait(
        {work_task, monitor_task},
        return_when=asyncio.FIRST_COMPLETED,
    )
    if monitor_task in done:
        reason = monitor_task.result()
        work_task.cancel()
        await asyncio.gather(work_task, return_exceptions=True)
        if reason == "timeout":
            raise WebError("web fetch timed out", "WEB_FETCH_TIMEOUT")
        raise WebError("web fetch aborted", "WEB_ABORTED")
    monitor_task.cancel()
    await asyncio.gather(monitor_task, return_exceptions=True)
    try:
        return work_task.result()
    except WebError:
        raise
    except Exception as error:  # noqa: BLE001 - 传输/网络失败统一分类
        if is_set_signal(signal):
            raise WebError("web fetch aborted", "WEB_ABORTED") from error
        raise WebError(f"web fetch failed: {type(error).__name__}: {error}", "WEB_PROVIDER_ERROR") from error


async def _monitor(timeout_ms: float, signal: SearchSignal | None) -> str:
    """轮询取消信号与单调时钟，先到者返回 "aborted"/"timeout"（0.01s 一轮）。"""
    deadline = time.monotonic() + timeout_ms / 1000.0
    while True:
        if is_set_signal(signal):
            return "aborted"
        if time.monotonic() >= deadline:
            return "timeout"
        await asyncio.sleep(0.01)


class HttpFetchProvider:
    """匿名公共抓取 provider；无凭据检查故恒可用（provider.ts:39）。"""

    id = "http"

    def __init__(self, limits: HttpFetchLimits, resolve_addresses=None):
        self.limits = limits
        self._resolve = resolve_addresses or resolve_public_addresses

    def available(self) -> bool:
        return True

    async def fetch(self, request: dict, signal: SearchSignal | None = None) -> dict:
        if is_set_signal(signal):
            raise WebError("web fetch aborted", "WEB_ABORTED")
        return await run_deadlined(
            self._follow_and_read(request["url"], signal),
            self.limits.timeout_ms,
            signal,
        )

    async def _follow_and_read(self, initial_url: str, signal: SearchSignal | None) -> dict:
        """跟随同源重定向到跳数上限，再读最终响应（provider.ts:66）。"""
        current_url = validate_fetch_url(initial_url)
        redirects_followed = 0
        while True:
            client, response = await self._request_once(current_url, signal)
            try:
                if response.status_code in _REDIRECT_STATUSES:
                    if redirects_followed >= self.limits.max_redirects:
                        raise WebError(
                            f"exceeded the maximum of {self.limits.max_redirects} redirects",
                            "WEB_REDIRECT_BLOCKED",
                        )
                    location = response.headers.get("location")
                    if location is None:
                        raise WebError(
                            f"redirect response (HTTP {response.status_code}) without a Location header",
                            "WEB_PROVIDER_ERROR",
                        )
                    target = _resolve_redirect(location, current_url)
                    try:
                        validated_target = validate_fetch_url(target)
                        if not is_same_origin(validated_target, current_url):
                            raise WebError(
                                f"cross-origin redirect to {validated_target.origin} "
                                "is not followed automatically; retry against that URL directly",
                                "WEB_REDIRECT_BLOCKED",
                            )
                    except WebError:
                        raise
                    current_url = validated_target
                    redirects_followed += 1
                    continue
                return await self._read_body(response, current_url)
            finally:
                await response.aclose()
                await client.aclose()

    async def _request_once(self, url: FetchUrl, signal: SearchSignal | None):
        """解析钉桩地址，逐跳新建独立 client 发 GET；WebError 直透（provider.ts:117）。"""
        headers = {"user-agent": self.limits.user_agent, "accept": _ACCEPT_HEADER}
        try:
            addresses = await self._resolve(url.hostname, signal)
            transport = httpx.AsyncHTTPTransport()
            transport._pool = AsyncConnectionPool(network_backend=PinnedNetworkBackend(addresses))  # type: ignore[attr-defined]
            client = httpx.AsyncClient(
                transport=transport,
                follow_redirects=False,
                timeout=None,
            )
            try:
                request = client.build_request("GET", url.request_url, headers=headers)
                response = await client.send(request, stream=True)
            except BaseException:
                await client.aclose()
                raise
            return client, response
        except WebError:
            raise
        except Exception as error:  # noqa: BLE001 - 由外层分类
            if is_set_signal(signal):
                raise WebError("web fetch aborted", "WEB_ABORTED") from error
            raise WebError(
                f"web fetch failed: {type(error).__name__}: {error}",
                "WEB_PROVIDER_ERROR",
            ) from error

    async def _read_body(self, response: httpx.Response, final_url: FetchUrl) -> dict:
        """字节帽 → 分类解码 → 字符帽（provider.ts:147）。"""
        content_type = response.headers.get("content-type")
        kind = classify_content_type(content_type)
        if kind is None:
            raise WebError(
                f'unsupported content type "{content_type or "unknown"}"',
                "WEB_UNSUPPORTED_CONTENT_TYPE",
            )
        codec = decoder_for_charset(parse_charset(content_type))
        data, truncated_by_bytes = await self._read_capped(response)
        decoded = data.decode(codec, errors="replace")
        if decoded.startswith("\ufeff"):
            decoded = decoded[1:]
        truncated_by_chars = len(decoded) > self.limits.max_body_chars
        content = decoded[: self.limits.max_body_chars] if truncated_by_chars else decoded
        return {
            "url": final_url.href,
            "statusCode": response.status_code,
            "body": {"kind": kind, "content": content},
            "truncated": truncated_by_bytes or truncated_by_chars,
        }

    async def _read_capped(self, response: httpx.Response) -> tuple[bytes, bool]:
        """读到 maxResponseBytes：声明超限立即拒绝；流超限截断不拒绝（provider.ts:185）。"""
        declared = response.headers.get("content-length")
        if declared is not None:
            try:
                length = float(declared)
            except ValueError:
                length = float("nan")
            if math.isfinite(length) and length > self.limits.max_response_bytes:
                raise WebError(
                    f"response exceeds the maximum of {self.limits.max_response_bytes} bytes",
                    "WEB_FETCH_TOO_LARGE",
                )
        chunks: list[bytes] = []
        total = 0
        truncated = False
        async for chunk in response.aiter_bytes():
            remaining = self.limits.max_response_bytes - total
            if len(chunk) > remaining:
                chunks.append(chunk[:remaining])
                total += remaining
                truncated = True
                break
            chunks.append(chunk)
            total += len(chunk)
        return b"".join(chunks), truncated


def _resolve_redirect(location: str, base: FetchUrl) -> str:
    """把（可能相对的）Location 对当前 URL 求值（provider.ts:247）。"""
    from urllib.parse import urljoin

    try:
        return urljoin(base.request_url, location)
    except ValueError as error:  # 防御：URL 解析对合法绝对 base 几乎不抛
        raise WebError(f'invalid redirect Location "{location}"', "WEB_PROVIDER_ERROR") from error