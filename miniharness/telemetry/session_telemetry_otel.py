"""OpenTelemetry 后端（对齐 packages/session/session-telemetry-otel）。

后端只做反馈授权 + 记录转发：把 coordinator 交来的 ledger 记录重建为完整事件，
经共享 `ctx.otel.createSessionLogReporter` 的按字节界通道上报；资源身份与
外层 shutdown 期限归本插件所有。

**载体差异（登记）**：Node `@opentelemetry/sdk-logs` → 共享 `otel` 服务的
自实现 OTLP/JSON 通道（按字节批处理 SDK 无对应能力）；`APP_IDENTITY` 取自
mini `llm` 协议；`getOrCreateAnonymousUserId` → mini `identity`。
"""
from __future__ import annotations

import asyncio
import urllib.parse

from ..core.scope import Context
from ..core.version import __version__
from ..identity import get_or_create_anonymous_user_id
from ..llm import APP_IDENTITY
from .otel import SessionLogRecord
from .otel_transport import SEVERITY_ERROR, SEVERITY_INFO, SEVERITY_WARN
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
MAX_TIMER_DELAY_MILLIS = 2_147_483_647

DISABLED_FEEDBACK_WARNING = (
    "OpenTelemetry session upload is DISABLED; this feedback is not uploaded through OpenTelemetry")
NON_CANONICAL_EVENT_WARNING = (
    "session telemetry ignored an event absent from the canonical session log")
WITHHELD_ENVELOPE_WARNING = "Session log record withheld: redaction removed sourceEvent"

_SCOPE_NAME = "session-telemetry-otel"

_SEVERITY = {
    "info": SEVERITY_INFO,
    "warn": SEVERITY_WARN,
    "error": SEVERITY_ERROR,
}


def _is_feedback(session, event: dict) -> bool:
    if event.get("seq", 0) < session.inherited_event_count:
        return False
    kind = event.get("type")
    if kind == "feedback/record":
        return True
    if kind in ("feedback/message-put", "feedback/message-delete"):
        return (event.get("data") or {}).get("sessionId") == session.session_id
    return False


def _consume_task(task: asyncio.Task) -> None:
    """外层期限到点后，后台 shutdown 任务仍会结算；消费其异常避免告警。"""
    if task.cancelled():
        return
    task.exception()


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

    inject = ["sessions", "otel"]

    def __init__(self, ctx: Context, config: dict | None = None):
        config = config or {}
        mode = config.get("mode") or DEFAULT_TELEMETRY_MODE
        if mode not in (MODE_FEEDBACK_ONLY, MODE_DISABLED):
            raise ValueError(f"session-telemetry-otel: unsupported mode {mode!r}")
        super().__init__(ctx)
        self._mode = mode
        self._sharing = ("feedback-only" if mode == MODE_FEEDBACK_ONLY else "disabled")
        self._reporter = None
        self._shutdown_timeout = DEFAULT_SHUTDOWN_TIMEOUT_MILLIS

        if mode == MODE_DISABLED:
            ctx.on("session/event", lambda payload: self._warn_disabled(payload))
            return

        otel = ctx.get("otel")
        if otel is None:
            raise RuntimeError("session-telemetry-otel: the shared otel service is required")
        self._reporter = self._build_reporter(otel, config)
        coordinator = SessionTelemetryCoordinator(
            ctx, _Sink(self), {"capture": "on-demand", "includeHistory": True})
        ctx.on("session/event", lambda payload: self._on_event(payload, coordinator))

    @property
    def sharing(self) -> str:
        return self._sharing

    def _build_reporter(self, otel, config: dict):
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
        if batch_size is not None and (not isinstance(batch_size, int)
                                       or isinstance(batch_size, bool) or batch_size < 1):
            raise ValueError(
                "session-telemetry-otel: processor.maxExportBatchSize must be a positive integer")
        timeout = config.get("shutdownTimeoutMillis", DEFAULT_SHUTDOWN_TIMEOUT_MILLIS)
        if (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                or timeout <= 0 or timeout > MAX_TIMER_DELAY_MILLIS):
            raise ValueError(
                "session-telemetry-otel: shutdownTimeoutMillis must be a positive number no "
                f"greater than {MAX_TIMER_DELAY_MILLIS}")
        self._shutdown_timeout = int(timeout)

        options = {
            "scope": {"name": _SCOPE_NAME, "version": __version__},
            "exporter": exporter_config,
            "resourceAttributes": {
                "service.name": APP_IDENTITY.product,
                "service.version": APP_IDENTITY.version,
                "user.id": get_or_create_anonymous_user_id(),
            },
            "onFailure": lambda message, error=None: self._warn(message, error),
        }
        if processor_config:
            options["processor"] = processor_config
        if config.get("maxRequestBytes") is not None:
            options["maxRequestBytes"] = config["maxRequestBytes"]
        return otel.create_session_log_reporter(options)

    def _warn(self, message: str, error: BaseException | None = None) -> None:
        logger = self.ctx.root.logger
        if logger is None:
            return
        if error is None:
            logger.warn(message)
        else:
            logger.warn(message, error)

    # ---------- 上传路径 ----------

    def _enqueue(self, record: SessionTelemetryRecord) -> None:
        if self._reporter is None:
            return
        source = record.source_event
        if source is None:
            self._warn(WITHHELD_ENVELOPE_WARNING)
            return
        event = dict(source["envelope"])
        event["data"] = record.body
        self._reporter.report_session_log(SessionLogRecord(
            session_id=source["sessionId"],
            event=event,
            attributes=record.attributes,
            severity_number=_SEVERITY[record.severity],
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
            self._warn(NON_CANONICAL_EVENT_WARNING)
            return
        coordinator.capture_session(session, event.get("seq"))

    def _warn_disabled(self, payload: dict) -> None:
        session = payload.get("session")
        event = payload.get("event")
        if session is None or event is None or not _is_feedback(session, event):
            return
        self._warn(DISABLED_FEEDBACK_WARNING)

    # ---------- 拆解 ----------

    async def shutdown(self) -> None:
        """排空队列至部署期限；到点停止后续请求并拒绝（在途传输仍可结算）。"""
        if self._reporter is None:
            return
        reporter = self._reporter
        task = asyncio.ensure_future(reporter.shutdown())
        task.add_done_callback(_consume_task)
        try:
            await asyncio.wait_for(
                asyncio.shield(task), timeout=self._shutdown_timeout / 1000.0)
        except asyncio.TimeoutError:
            reporter.stop_pending()
            raise RuntimeError(
                "session-telemetry-otel: provider shutdown exceeded "
                f"{self._shutdown_timeout}ms")
