"""共享 OTel 服务：普通事件通道（按条数）+ 会话日志通道（按字节）。

对齐上游 packages/telemetry/otel/src：
  * `OTel` 服务（`ctx.otel`）暴露 `createEventReporter` / `createSessionLogReporter`；
    挂载本身不分配队列/身份/连接——通道由调用方创建并拥有。
  * `EventLogReporter`：普通事件按条数批处理（`emit` 打 `observedTimestamp`，
    severity 缺省 INFO），`shutdown(signal?)` 在取消时丢弃在途导出。
  * `SessionLogReporter`：会话日志按字节批处理——单条记录单独计量、一条传输在途、
    按 `maxRequestBytes` 成批；超限记录（不截断）拒绝、队列满拒绝、导出看门狗
    超时只告警**不释放**传输槽位。
"""
from __future__ import annotations

import asyncio
import json
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Callable

from ..core.scope import Context, Service
from .otel_transport import (
    AbortController,
    AbortSignal,
    AbortedError,
    OtlpJsonTransport,
    OtlpLogRecord,
    SEVERITY_INFO,
    encode_logs_request,
)

__all__ = [
    "DEFAULT_SESSION_LOG_MAX_REQUEST_BYTES",
    "EventLogReporter",
    "OTel",
    "OTelEventRecord",
    "SESSION_LOG_MAX_REQUEST_BYTES",
    "SessionLogRecord",
    "SessionLogReporter",
    "install_otel",
    "resolve_session_log_limits",
]

#: 采集端请求上限（未压缩 UTF-8 字节，含 OTLP 信封）。
SESSION_LOG_MAX_REQUEST_BYTES = 4_000_000
DEFAULT_SESSION_LOG_MAX_REQUEST_BYTES = SESSION_LOG_MAX_REQUEST_BYTES

_MAX_TIMER_DELAY_MILLIS = 2_147_483_647

_DEFAULT_SESSION_PROCESSOR = {
    "maxQueueSize": 2048,
    "maxExportBatchSize": 512,
    "scheduledDelayMillis": 1000,
    "exportTimeoutMillis": 30000,
}
_DEFAULT_EVENT_PROCESSOR = {
    "maxQueueSize": 2048,
    "maxExportBatchSize": 512,
    "scheduledDelayMillis": 5000,
    "exportTimeoutMillis": 30000,
}


@dataclass(frozen=True)
class OTelEventRecord:
    """一条普通分析事件（`eventName`/`body` 由产品方选择，不含自动身份）。"""

    event_name: str
    body: str
    timestamp: int
    severity_number: int | None = None
    attributes: Mapping[str, Any] | None = None


@dataclass(frozen=True)
class SessionLogRecord:
    """一条完整会话事件 + 其独立会话身份 + 已脱敏载荷。"""

    session_id: str
    event: Mapping[str, Any]
    attributes: Mapping[str, Any] | None = None
    severity_number: int | None = None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _running_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _resolve_exporter(options: Mapping[str, Any]) -> dict:
    exporter = options.get("exporter")
    if not isinstance(exporter, Mapping):
        raise ValueError("otel: exporter is required")
    url = exporter.get("url")
    if not isinstance(url, str) or url == "":
        raise ValueError("otel: exporter.url is required")
    timeout_millis = exporter.get("timeoutMillis", 10_000)
    if (not isinstance(timeout_millis, (int, float)) or isinstance(timeout_millis, bool)
            or timeout_millis <= 0):
        raise ValueError("otel: exporter.timeoutMillis must be a positive number")
    headers = exporter.get("headers")
    if headers is not None and not isinstance(headers, Mapping):
        raise ValueError("otel: exporter.headers must be an object")
    compression = exporter.get("compression")
    if compression == "none":
        compression = None
    return {
        "url": url,
        "headers": dict(headers or {}),
        "timeout_seconds": float(timeout_millis) / 1000.0,
        "compression": compression,
        "user_agent": exporter.get("userAgent"),
    }


def _resolve_scope(options: Mapping[str, Any]) -> tuple[str, str | None]:
    scope = options.get("scope")
    if not isinstance(scope, Mapping) or not isinstance(scope.get("name"), str) or not scope["name"]:
        raise ValueError("otel: scope.name is required")
    version = scope.get("version")
    return scope["name"], version


def resolve_session_log_limits(options: Mapping[str, Any]) -> int:
    """校验字节/队列设置并返回采集端请求字节上限（对齐上游 resolveSessionLogLimits）。"""
    limit = options.get("maxRequestBytes", SESSION_LOG_MAX_REQUEST_BYTES)
    if (not isinstance(limit, int) or isinstance(limit, bool)
            or limit < 1 or limit > SESSION_LOG_MAX_REQUEST_BYTES):
        raise ValueError(
            "session log maxRequestBytes must be an integer between 1 and "
            f"{SESSION_LOG_MAX_REQUEST_BYTES}")
    processor = options.get("processor") or {}
    if not isinstance(processor, Mapping):
        raise ValueError("session log processor must be an object")
    for key in ("maxQueueSize", "maxExportBatchSize", "scheduledDelayMillis", "exportTimeoutMillis"):
        value = processor.get(key)
        if value is None:
            continue
        if (not isinstance(value, int) or isinstance(value, bool)
                or value < 1 or value > _MAX_TIMER_DELAY_MILLIS):
            raise ValueError(
                f"session log processor.{key} must be a positive integer no greater than "
                f"{_MAX_TIMER_DELAY_MILLIS}")
    queue = processor.get("maxQueueSize", _DEFAULT_SESSION_PROCESSOR["maxQueueSize"])
    batch = processor.get("maxExportBatchSize", _DEFAULT_SESSION_PROCESSOR["maxExportBatchSize"])
    if batch > queue:
        raise ValueError("session log maxExportBatchSize must not exceed maxQueueSize")
    return limit


def _resolve_processor(options: Mapping[str, Any], defaults: Mapping[str, int]) -> dict:
    processor = options.get("processor") or {}
    if not isinstance(processor, Mapping):
        raise ValueError("otel: processor must be an object")
    resolved = dict(defaults)
    for key in defaults:
        value = processor.get(key)
        if value is None:
            continue
        if (not isinstance(value, int) or isinstance(value, bool)
                or value < 1 or value > _MAX_TIMER_DELAY_MILLIS):
            raise ValueError(f"otel: processor.{key} must be a positive integer")
        resolved[key] = value
    return resolved


class _CountBatchProcessor:
    """普通事件按条数批处理（对齐 SDK BatchLogRecordProcessor 的默认行为）。"""

    def __init__(self, transport: OtlpJsonTransport, resource: Mapping[str, Any],
                 scope_name: str, scope_version: str | None, config: Mapping[str, int],
                 on_failure: Callable[[str, BaseException | None], None]) -> None:
        self._transport = transport
        self._resource = resource
        self._scope_name = scope_name
        self._scope_version = scope_version
        self._max_queue_size = config["maxQueueSize"]
        self._max_export_batch_size = config["maxExportBatchSize"]
        self._scheduled_delay = config["scheduledDelayMillis"] / 1000.0
        self._on_failure = on_failure
        self._queue: list[OtlpLogRecord] = []
        self._timer: asyncio.TimerHandle | None = None
        self._active: asyncio.Task | None = None
        self._stopped = False

    def on_emit(self, record: OtlpLogRecord) -> None:
        if self._stopped:
            return
        if len(self._queue) >= self._max_queue_size:
            return
        self._queue.append(record)
        if self._active is not None:
            return
        if len(self._queue) >= self._max_export_batch_size:
            self._start_drain()
        elif self._timer is None:
            self._schedule_timer()

    def _schedule_timer(self) -> None:
        loop = _running_loop()
        if loop is None:
            return
        self._timer = loop.call_later(self._scheduled_delay, self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        if self._active is None and self._queue:
            self._start_drain()

    def _start_drain(self) -> None:
        loop = _running_loop()
        if loop is None or self._active is not None:
            return
        self._active = loop.create_task(self._drain())

    async def _drain(self, signal: AbortSignal | None = None) -> None:
        try:
            while self._queue:
                batch = self._queue[:self._max_export_batch_size]
                del self._queue[:len(batch)]
                payload = encode_logs_request(
                    self._resource, self._scope_name, self._scope_version, batch)
                try:
                    await self._transport.export(payload, signal)
                except AbortedError:
                    break
                except Exception as error:  # noqa: BLE001 - 上报失败不得拖垮调用方
                    self._on_failure("Product telemetry export failed", error)
        finally:
            if self._active is asyncio.current_task():
                self._active = None

    async def force_flush(self, signal: AbortSignal | None = None) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._active is not None:
            await self._active
        if not self._queue:
            return
        loop = _running_loop()
        if loop is None:
            return
        self._active = loop.create_task(self._drain(signal))
        await self._active

    async def shutdown(self, signal: AbortSignal | None = None) -> None:
        self._stopped = True
        await self.force_flush(signal)


class _ByteBatchProcessor:
    """会话日志按字节批处理：一条传输在途，看门狗超时不释放槽位。"""

    def __init__(self, transport: OtlpJsonTransport, resource: Mapping[str, Any],
                 scope_name: str, scope_version: str | None, limit: int,
                 config: Mapping[str, int],
                 warn: Callable[[str, BaseException | None], None]) -> None:
        self._transport = transport
        self._resource = resource
        self._scope_name = scope_name
        self._scope_version = scope_version
        self._limit = limit
        self._max_queue_size = config["maxQueueSize"]
        self._max_export_batch_size = config["maxExportBatchSize"]
        self._scheduled_delay = config["scheduledDelayMillis"] / 1000.0
        self._export_timeout = config["exportTimeoutMillis"] / 1000.0
        self._warn = warn
        self._queue: list[tuple[OtlpLogRecord, int]] = []
        self._bytes = 0
        self._timer: asyncio.TimerHandle | None = None
        self._active: asyncio.Task | None = None
        self._stopped = False

    def on_emit(self, record: OtlpLogRecord) -> None:
        if self._stopped:
            return
        if len(self._queue) >= self._max_queue_size:
            self._warn("Session log queue is full; record rejected", None)
            return
        try:
            measured = len(encode_logs_request(
                self._resource, self._scope_name, self._scope_version, [record]))
        except Exception as error:  # noqa: BLE001 - 序列化失败拒该条，不拖垮调用方
            self._warn("Session log serialization failed; record rejected", error)
            return
        if measured > self._limit:
            self._warn(
                "Session log record rejected; content was not truncated",
                RuntimeError(
                    f"Session log record exceeds maxRequestBytes: {measured} > {self._limit}"))
            return
        self._queue.append((record, measured))
        self._bytes += measured
        if self._active is not None:
            return
        if len(self._queue) >= self._max_export_batch_size or self._bytes >= self._limit:
            self._start_drain()
        elif self._timer is None:
            self._schedule_timer()

    def _schedule_timer(self) -> None:
        loop = _running_loop()
        if loop is None:
            return
        self._timer = loop.call_later(self._scheduled_delay, self._on_timer)

    def _on_timer(self) -> None:
        self._timer = None
        if self._active is None and self._queue:
            self._start_drain()

    def _start_drain(self) -> None:
        loop = _running_loop()
        if loop is None or self._active is not None:
            return
        self._active = loop.create_task(self._drain())

    async def _drain(self) -> None:
        try:
            while not self._stopped and self._queue:
                batch_bytes = 0
                count = 0
                for _record, measured in self._queue:
                    if (count == self._max_export_batch_size
                            or batch_bytes + measured > self._limit):
                        break
                    batch_bytes += measured
                    count += 1
                if count == 0:
                    break
                records = [record for record, _ in self._queue[:count]]
                del self._queue[:count]
                self._bytes -= batch_bytes
                await self._send(records)
        finally:
            if self._active is asyncio.current_task():
                self._active = None

    def _on_export_timeout(self) -> None:
        self._warn(
            "Session log request exceeded exportTimeoutMillis; waiting for transport settlement",
            None)

    async def _send(self, records: list[OtlpLogRecord]) -> None:
        loop = _running_loop()
        watchdog = None
        if loop is not None:
            watchdog = loop.call_later(self._export_timeout, self._on_export_timeout)
        try:
            payload = encode_logs_request(
                self._resource, self._scope_name, self._scope_version, records)
            await self._transport.export(payload)
        except AbortedError:
            pass
        except Exception as error:  # noqa: BLE001 - 导出失败只告警，继续后续请求
            self._warn("Session log export failed", error)
        finally:
            if watchdog is not None:
                watchdog.cancel()

    async def force_flush(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self._active is not None:
            await self._active
        if not self._queue:
            return
        loop = _running_loop()
        if loop is None:
            return
        self._active = loop.create_task(self._drain())
        await self._active

    def stop_pending(self) -> None:
        self._stopped = True
        self._queue.clear()
        self._bytes = 0
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None

    async def shutdown(self) -> None:
        await self.force_flush()
        await self._transport.shutdown()


class EventLogReporter:
    """普通事件通道：调用方拥有的队列，与任何会话日志队列独立。"""

    def __init__(self, options: Mapping[str, Any]) -> None:
        exporter = _resolve_exporter(options)
        scope_name, scope_version = _resolve_scope(options)
        resource = dict(options.get("resourceAttributes") or {})
        on_failure = options.get("onFailure")
        if not callable(on_failure):
            raise ValueError("otel: onFailure callback is required")
        processor = _resolve_processor(options, _DEFAULT_EVENT_PROCESSOR)
        self._transport = OtlpJsonTransport(
            exporter["url"], headers=exporter["headers"],
            timeout_seconds=exporter["timeout_seconds"],
            compression=exporter["compression"], user_agent=exporter["user_agent"])
        self._processor = _CountBatchProcessor(
            self._transport, resource, scope_name, scope_version, processor, on_failure)
        self._cancellation = AbortController()

    def emit(self, record: OTelEventRecord) -> None:
        severity = record.severity_number if record.severity_number is not None else SEVERITY_INFO
        self._processor.on_emit(OtlpLogRecord(
            timestamp=record.timestamp,
            observed_timestamp=_now_ms(),
            severity_number=severity,
            body=record.body,
            attributes=dict(record.attributes or {}),
            event_name=record.event_name,
        ))

    async def shutdown(self, signal: AbortSignal | None = None) -> None:
        if signal is not None:
            if signal.aborted:
                self._cancellation.abort(signal.reason)
            else:
                signal.add_listener(lambda: self._cancellation.abort(signal.reason))
        await self._processor.shutdown(self._cancellation.signal)
        await self._transport.shutdown()


class SessionLogReporter:
    """会话日志通道：反馈授权后按字节界成批，调用方拥有 shutdown 与外层期限。"""

    def __init__(self, options: Mapping[str, Any]) -> None:
        limit = resolve_session_log_limits(options)
        exporter = _resolve_exporter(options)
        scope_name, scope_version = _resolve_scope(options)
        resource = dict(options.get("resourceAttributes") or {})
        on_failure = options.get("onFailure")
        if not callable(on_failure):
            raise ValueError("otel: onFailure callback is required")
        processor = _resolve_processor(options, _DEFAULT_SESSION_PROCESSOR)
        if processor["maxExportBatchSize"] > processor["maxQueueSize"]:
            raise ValueError("session log maxExportBatchSize must not exceed maxQueueSize")
        self._transport = OtlpJsonTransport(
            exporter["url"], headers=exporter["headers"],
            timeout_seconds=exporter["timeout_seconds"],
            compression=exporter["compression"], user_agent=exporter["user_agent"])
        self._processor = _ByteBatchProcessor(
            self._transport, resource, scope_name, scope_version, limit, processor, on_failure)

    def report_session_log(self, record: SessionLogRecord) -> None:
        severity = record.severity_number if record.severity_number is not None else SEVERITY_INFO
        event = record.event
        attributes = dict(record.attributes or {})
        attributes["sessionId"] = record.session_id
        attributes["content"] = json.dumps(
            event, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        timestamp = event["time"]
        self._processor.on_emit(OtlpLogRecord(
            timestamp=timestamp,
            observed_timestamp=timestamp,
            severity_number=severity,
            body="session-log",
            attributes=attributes,
            event_name="session-log",
        ))

    def stop_pending(self) -> None:
        self._processor.stop_pending()

    async def shutdown(self) -> None:
        await self._processor.shutdown()


class OTel(Service):
    """共享传输提供者：挂载不创建队列、身份或网络连接。"""

    provide = "otel"

    def __init__(self, ctx: Context, config: dict | None = None):
        super().__init__(ctx, "otel")

    def create_event_reporter(self, options: Mapping[str, Any]) -> EventLogReporter:
        """创建独立的普通事件通道（按条数批处理）。"""
        return EventLogReporter(options)

    def create_session_log_reporter(self, options: Mapping[str, Any]) -> SessionLogReporter:
        """创建独立的会话日志通道（按字节批处理）。"""
        return SessionLogReporter(options)


def install_otel(ctx: Context) -> OTel:
    """幂等装配共享 `ctx.otel` 服务（构造即自动注册）。"""
    existing = ctx.get("otel")
    if existing is not None:
        return existing
    return OTel(ctx)
