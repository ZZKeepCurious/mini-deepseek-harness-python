"""OpenTelemetry 后端（对齐 packages/session/session-telemetry-otel）。

组合 OTel Python SDK：`LoggerProvider` + `BatchLogRecordProcessor` + OTLP/HTTP exporter，
把 coordinator 交来的每条记录映射为 `logger.emit()`；此后批处理/重试/队列/丢弃策略由
SDK 承担。本包拥有采集模式（`FEEDBACK_ONLY` 按反馈 on-demand 采集；`DISABLED` 不建 SDK
状态、仅对本地反馈告警）与一个外层 shutdown 期限。

**载体差异（登记）**：Node `@opentelemetry/sdk-logs` → Python `opentelemetry-sdk`；
`getOrCreateAnonymousUserId` → mini `identity` 的匿名用户 id（缺失时省略 `user.id`）。
测试可注入 `provider=`（真实 `LoggerProvider` + in-memory exporter），非 mock。
"""
from __future__ import annotations

import urllib.parse

from ..core.scope import Context
from .session_telemetry import (
    SessionTelemetryBackend,
    SessionTelemetryCoordinator,
    SessionTelemetryRecord,
)

__all__ = [
    "DEFAULT_SHUTDOWN_TIMEOUT_MILLIS",
    "DEFAULT_TELEMETRY_MODE",
    "MODE_DISABLED",
    "MODE_FEEDBACK_ONLY",
    "OpenTelemetrySessionBackend",
]

MODE_FEEDBACK_ONLY = "FEEDBACK_ONLY"
MODE_DISABLED = "DISABLED"
DEFAULT_TELEMETRY_MODE = MODE_FEEDBACK_ONLY
DEFAULT_SHUTDOWN_TIMEOUT_MILLIS = 3000

DISABLED_FEEDBACK_WARNING = (
    "OpenTelemetry session upload is DISABLED; this feedback is not uploaded through OpenTelemetry")
NON_CANONICAL_EVENT_WARNING = (
    "session telemetry ignored an event absent from the canonical session log")

_SEVERITY = {"info": (9, "INFO"), "warn": (13, "WARN"), "error": (17, "ERROR")}


def _is_feedback(session, event: dict) -> bool:
    if event.get("seq", 0) < session.inherited_event_count:
        return False
    kind = event.get("type")
    if kind == "feedback/record":
        return True
    if kind in ("feedback/message-put", "feedback/message-delete"):
        return (event.get("data") or {}).get("sessionId") == session.session_id
    return False


class _Sink:
    """coordinator 的私有 sink：只有反馈授权路径经它上传。"""

    def __init__(self, backend: "OpenTelemetrySessionBackend"):
        self._backend = backend

    def emit(self, record: SessionTelemetryRecord) -> None:
        self._backend._enqueue(record)

    def shutdown(self):
        return self._backend.shutdown()


class OpenTelemetrySessionBackend(SessionTelemetryBackend):
    """`sessionTelemetry` 的 OTel 后端插件。"""

    def __init__(self, ctx: Context, config: dict | None = None, *, provider=None):
        config = config or {}
        mode = config.get("mode") or DEFAULT_TELEMETRY_MODE
        if mode not in (MODE_FEEDBACK_ONLY, MODE_DISABLED):
            raise ValueError(f"session-telemetry-otel: unsupported mode {mode!r}")
        super().__init__(ctx)
        self._mode = mode
        self._sharing = ("feedback-only" if mode == MODE_FEEDBACK_ONLY else "disabled")
        self._provider = None
        self._logger = None
        self._shutdown_timeout = DEFAULT_SHUTDOWN_TIMEOUT_MILLIS

        if mode == MODE_DISABLED:
            ctx.on("session/event", lambda payload: self._warn_disabled(payload))
            return

        if provider is None:
            provider = self._build_provider(config)
        self._provider = provider
        self._logger = provider.get_logger("session-telemetry-otel")
        coordinator = SessionTelemetryCoordinator(
            ctx, _Sink(self), {"capture": "on-demand", "includeHistory": True})
        ctx.on("session/event", lambda payload: self._on_event(payload, coordinator))

    @property
    def sharing(self) -> str:
        return self._sharing

    def _build_provider(self, config: dict):
        from opentelemetry.sdk._logs import (
            LoggerProvider,
            SynchronousMultiLogRecordProcessor,
        )
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.sdk.resources import Resource

        exporter_config = config.get("exporter") or {}
        if not isinstance(exporter_config, dict):
            raise ValueError("session-telemetry-otel: exporter must be an object")
        url = exporter_config.get("url")
        if not isinstance(url, str) or url == "":
            raise ValueError(
                "session-telemetry-otel: exporter.url is required (the full OTLP logs endpoint)")
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("http", "https"):
            raise ValueError(
                f"session-telemetry-otel: exporter.url must be http(s), got {parsed.scheme!r}")
        processor_config = config.get("processor") or {}
        batch_size = processor_config.get("maxExportBatchSize")
        if batch_size is not None and (not isinstance(batch_size, int) or batch_size < 1):
            raise ValueError(
                "session-telemetry-otel: processor.maxExportBatchSize must be a positive integer")
        timeout = config.get("shutdownTimeoutMillis", DEFAULT_SHUTDOWN_TIMEOUT_MILLIS)
        if not isinstance(timeout, (int, float)) or timeout <= 0:
            raise ValueError(
                "session-telemetry-otel: shutdownTimeoutMillis must be a positive number")
        self._shutdown_timeout = int(timeout)

        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        resource_attrs = {
            "service.name": "mini-harness",
            "service.version": "0.0.0",
        }
        try:
            from ..identity import get_or_create_anonymous_user_id
            resource_attrs["user.id"] = get_or_create_anonymous_user_id()
        except Exception:  # noqa: BLE001 - 身份缺失时省略 user.id（载体差异）
            pass
        exporter = OTLPLogExporter(endpoint=url, headers=exporter_config.get("headers"))
        multi = SynchronousMultiLogRecordProcessor()
        multi.add_log_record_processor(BatchLogRecordProcessor(exporter))
        return LoggerProvider(
            resource=Resource.create(resource_attrs),
            multi_log_record_processor=multi,
        )

    # ---------- 上传路径 ----------

    def _enqueue(self, record: SessionTelemetryRecord) -> None:
        if self._logger is None:
            return
        from opentelemetry._logs import SeverityNumber
        from opentelemetry.sdk._logs._internal import LogRecord
        severity_number, severity_text = _SEVERITY[record.severity]
        self._logger.emit(LogRecord(
            timestamp=record.time * 1_000_000,
            observed_timestamp=record.time * 1_000_000,
            severity_number=SeverityNumber(severity_number),
            severity_text=severity_text,
            body=record.body,
            attributes=record.attributes,
        ))

    def emit(self, record: SessionTelemetryRecord) -> None:
        """直接记录不上传：只有新的 canonical 反馈提交能授权采集。"""

    def _on_event(self, payload: dict, coordinator: SessionTelemetryCoordinator) -> None:
        session = payload.get("session")
        event = payload.get("event")
        if session is None or event is None or not _is_feedback(session, event):
            return
        canonical = any(item is event for item in getattr(session, "events", []) or [])
        if not canonical:
            logger = self.ctx.root.logger
            if logger is not None:
                logger.warn(NON_CANONICAL_EVENT_WARNING)
            return
        coordinator.capture_session(session, event.get("seq"))

    def _warn_disabled(self, payload: dict) -> None:
        session = payload.get("session")
        event = payload.get("event")
        if session is None or event is None or not _is_feedback(session, event):
            return
        logger = self.ctx.root.logger
        if logger is not None:
            logger.warn(DISABLED_FEEDBACK_WARNING)

    # ---------- 拆解 ----------

    async def shutdown(self) -> None:
        if self._provider is None:
            return
        try:
            self._provider.force_flush(timeout_millis=self._shutdown_timeout)
        finally:
            self._provider.shutdown()
