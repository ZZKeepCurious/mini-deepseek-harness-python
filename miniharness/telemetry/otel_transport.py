"""显式 OTLP/JSON HTTP 传输 + 可取消信号 + OTLP logs 编码。

对齐上游 packages/telemetry/otel/src/{transport,event-transport}.ts：

  * 端点、请求头、agent 只来自显式配置——绝不继承环境（mini 用
    `httpx.AsyncClient(trust_env=False)` 保证不读 OTEL_* 环境与系统代理）；
  * 429/502/503/504 重试（retry limit 5）；响应体上限 4 MiB；不跟随重定向；
    可选 gzip 压缩；超时上限；
  * `AbortSignal`/`AbortController` 对齐 DOM：abort 会取消在途请求与重试等待
    （上游 event-transport 用 `AbortSignal.any([signal, timeout])`）。

OTLP/JSON 编码对齐 `JsonLogsSerializer.serializeRequest`：
`resourceLogs[].resource.attributes` + `scopeLogs[].scope` + 每条 logRecord；
int64 按 proto3 JSON 映射为字符串，`body`/属性值包成 AnyValue。
"""
from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

__all__ = [
    "AbortController",
    "AbortSignal",
    "AbortedError",
    "MAX_RESPONSE_BODY_BYTES",
    "MAX_RETRIES",
    "OtlpJsonTransport",
    "OtlpLogRecord",
    "RETRYABLE_STATUS_CODES",
    "SEVERITY_ERROR",
    "SEVERITY_INFO",
    "SEVERITY_TEXT",
    "SEVERITY_WARN",
    "encode_logs_request",
    "severity_text",
]

SEVERITY_INFO = 9
SEVERITY_WARN = 13
SEVERITY_ERROR = 17

SEVERITY_TEXT = {SEVERITY_INFO: "INFO", SEVERITY_WARN: "WARN", SEVERITY_ERROR: "ERROR"}

#: 响应体上限（上游 event-transport downloadProgress 4 * 1024 * 1024）。
MAX_RESPONSE_BODY_BYTES = 4 * 1024 * 1024

#: 上游 got `retry.limit: 5`——首次失败后最多再试 5 次（共 6 次尝试）。
MAX_RETRIES = 5

#: got `retry.statusCodes`。
RETRYABLE_STATUS_CODES = frozenset({429, 502, 503, 504})

_BACKOFF_LIMIT_SECONDS = 5.0


class AbortedError(Exception):
    """取消（AbortSignal）导致的操作中止。"""


class AbortSignal:
    """单发取消信号（对齐 DOM AbortSignal 的 aborted/reason/监听面）。"""

    __slots__ = ("_aborted", "_reason", "_listeners", "_event")

    def __init__(self) -> None:
        self._aborted = False
        self._reason: Any = None
        self._listeners: list[Any] = []
        self._event: asyncio.Event | None = None

    @property
    def aborted(self) -> bool:
        return self._aborted

    @property
    def reason(self) -> Any:
        return self._reason

    def throw_if_aborted(self) -> None:
        if self._aborted:
            raise AbortedError(str(self._reason) if self._reason is not None else "aborted")

    async def wait(self) -> None:
        """等待取消；已取消则立即返回。"""
        if self._aborted:
            return
        if self._event is None:
            self._event = asyncio.Event()
        await self._event.wait()

    def add_listener(self, callback: Any) -> None:
        if self._aborted:
            callback()
            return
        self._listeners.append(callback)

    def _abort(self, reason: Any) -> None:
        if self._aborted:
            return
        self._aborted = True
        self._reason = reason
        if self._event is not None:
            self._event.set()
        for callback in list(self._listeners):
            callback()
        self._listeners.clear()


class AbortController:
    """`abort(reason?)` 铸造并触发其 `signal`（对齐 DOM AbortController）。"""

    def __init__(self) -> None:
        self.signal = AbortSignal()

    def abort(self, reason: Any = None) -> None:
        self.signal._abort(reason)


@dataclass(frozen=True)
class OtlpLogRecord:
    """一条待编码的 OTLP log record（时间单位毫秒）。"""

    timestamp: int
    severity_number: int
    body: Any
    attributes: Mapping[str, Any] = field(default_factory=dict)
    event_name: str | None = None
    observed_timestamp: int | None = None


def severity_text(number: int) -> str:
    return SEVERITY_TEXT.get(number, "INFO")


def _any_value(value: Any) -> dict:
    """JSON 值 → OTLP AnyValue（对齐 JsonLogsSerializer.toAnyValue）。"""
    if value is None:
        return {}
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        # proto3 JSON：int64/uint32 等整型以字符串序列化。
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [_any_value(item) for item in value]}}
    if isinstance(value, Mapping):
        return {"kvlistValue": {"values": [
            {"key": str(key), "value": _any_value(item)} for key, item in value.items()]}}
    raise TypeError(f"OTLP attribute value is not JSON-serializable: {value!r}")


def _key_values(attributes: Mapping[str, Any]) -> list[dict]:
    return [{"key": key, "value": _any_value(value)} for key, value in attributes.items()]


def encode_logs_request(
    resource_attributes: Mapping[str, Any],
    scope_name: str,
    scope_version: str | None,
    records: list[OtlpLogRecord],
) -> bytes:
    """按 OTLP/JSON 编码一个 ExportLogsServiceRequest。"""
    scope: dict = {"name": scope_name}
    if scope_version is not None:
        scope["version"] = scope_version
    log_records = []
    for record in records:
        observed = record.observed_timestamp
        if observed is None:
            observed = record.timestamp
        entry: dict = {
            "timeUnixNano": str(record.timestamp * 1_000_000),
            "observedTimeUnixNano": str(observed * 1_000_000),
            "severityNumber": record.severity_number,
            "severityText": severity_text(record.severity_number),
            "body": _any_value(record.body),
        }
        if record.attributes:
            entry["attributes"] = _key_values(record.attributes)
        if record.event_name is not None:
            entry["eventName"] = record.event_name
        log_records.append(entry)
    document = {
        "resourceLogs": [{
            "resource": {"attributes": _key_values(resource_attributes)},
            "scopeLogs": [{"scope": scope, "logRecords": log_records}],
        }],
    }
    return json.dumps(
        document, ensure_ascii=False, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")


async def _abortable_sleep(seconds: float, signal: AbortSignal | None) -> None:
    if signal is None:
        await asyncio.sleep(seconds)
        return
    signal.throw_if_aborted()
    try:
        await asyncio.wait_for(signal.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        return
    signal.throw_if_aborted()


class OtlpJsonTransport:
    """一个通道私有的 OTLP/JSON HTTP 传输（不复用其它通道的连接/头）。

    `export` 是协程：返回响应体字节；HTTP >= 300 或响应体超限时抛出。
    停止时 `shutdown()` 释放自有 httpx 客户端。
    """

    def __init__(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 10.0,
        compression: str | None = None,
        user_agent: str | None = None,
        client: httpx.AsyncClient | None = None,
        retry_backoff_base: float = 0.5,
    ) -> None:
        parsed = httpx.URL(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(f"otel: exporter url must be http(s), got {parsed.scheme!r}")
        if compression not in (None, "none", "gzip"):
            raise ValueError(f"otel: exporter compression must be 'none' or 'gzip', got {compression!r}")
        self.url = url
        self._headers = dict(headers or {})
        self._timeout = timeout_seconds
        self._compression = compression
        self._user_agent = user_agent
        self._retry_backoff_base = retry_backoff_base
        self._client = client if client is not None else httpx.AsyncClient(trust_env=False)
        self._owns_client = client is None

    def _request_headers(self, compressed: bool) -> dict:
        headers = {"Content-Type": "application/json", **self._headers}
        if compressed:
            headers["content-encoding"] = "gzip"
        if self._user_agent is not None:
            headers["user-agent"] = self._user_agent
        return headers

    async def _send(self, payload: bytes, headers: Mapping[str, str]) -> tuple[int, bytes]:
        async with self._client.stream(
            "POST", self.url, content=payload, headers=headers,
            timeout=self._timeout, follow_redirects=False,
        ) as response:
            total = 0
            chunks: list[bytes] = []
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > MAX_RESPONSE_BODY_BYTES:
                    raise RuntimeError(
                        "otel: OTLP response body exceeded "
                        f"{MAX_RESPONSE_BODY_BYTES} bytes")
                chunks.append(chunk)
            return response.status_code, b"".join(chunks)

    async def export(self, body: bytes, signal: AbortSignal | None = None) -> bytes:
        if signal is not None:
            signal.throw_if_aborted()
        compressed = self._compression == "gzip"
        payload = gzip.compress(body) if compressed else body
        headers = self._request_headers(compressed)
        attempt = 0
        while True:
            if signal is not None:
                signal.throw_if_aborted()
            try:
                status, response_body = await self._send(payload, headers)
            except httpx.HTTPError as error:
                if attempt >= MAX_RETRIES:
                    raise
                attempt += 1
                await _abortable_sleep(
                    min(self._retry_backoff_base * attempt, _BACKOFF_LIMIT_SECONDS), signal)
                continue
            if status in RETRYABLE_STATUS_CODES and attempt < MAX_RETRIES:
                attempt += 1
                await _abortable_sleep(
                    min(self._retry_backoff_base * attempt, _BACKOFF_LIMIT_SECONDS), signal)
                continue
            if status >= 300:
                raise RuntimeError(f"otel: OTLP collector returned HTTP {status}")
            return response_body

    async def shutdown(self) -> None:
        if self._owns_client:
            await self._client.aclose()
