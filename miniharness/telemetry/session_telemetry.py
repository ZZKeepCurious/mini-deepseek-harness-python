"""会话遥测采集侧（对齐 packages/session/session-telemetry）。

- `SessionTelemetryBackend`：`ctx.sessionTelemetry` 服务定义（sharing / emit / flush? / shutdown）；
- `SessionTelemetryCoordinator`：采集侧——live（订阅 `session/created|event|disposed|flush` +
  `agent/error` 并清扫已在世会话）或 on-demand（显式读 canonical log）；每条事件经
  `session-telemetry/record` waterfall（本包不内置脱敏规则，部署挂载）→ 交给 backend；
  每个同步处理器自包含（异常 contain），失败后端绝不拖垮 agent loop。
"""
from __future__ import annotations

import dataclasses
import weakref
from typing import Any, Callable

from ..core.scope import Context, Service
from ..core.session import SESSION_FORMAT_VERSION

__all__ = [
    "SessionTelemetryBackend",
    "SessionTelemetryCoordinator",
    "SessionTelemetryRecord",
    "SessionTelemetrySink",
]


@dataclasses.dataclass(frozen=True)
class SessionTelemetryRecord:
    """交给 backend 的一条逻辑记录（ledger 镜像会话事件 / ops 运营信号）。"""

    channel: str  # 'ledger' | 'ops'
    time: int
    severity: str  # 'info' | 'warn' | 'error'
    attributes: dict
    body: Any


class SessionTelemetrySink:
    """coordinator 需要的最小 backend 契约（测试可用裸实现）。"""

    def emit(self, record: SessionTelemetryRecord) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        pass

    async def shutdown(self) -> None:
        raise NotImplementedError


class SessionTelemetryBackend(Service):
    """可装载的 backend 契约（`ctx.sessionTelemetry`）。"""

    provide = "sessionTelemetry"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "sessionTelemetry")

    @property
    def sharing(self) -> str:
        """部署选择的会话共享模式：'full' | 'feedback-only' | 'disabled'。"""
        raise NotImplementedError

    def emit(self, record: SessionTelemetryRecord) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        pass

    async def shutdown(self) -> None:
        raise NotImplementedError


# 交接游标（模块级 ambient 状态，登记为「注册即副作用」纪律的窄例外）：按 Session
# 对象键控，重采纳的 fiber 可续传而不重放历史。emit 时推进（标记已交接，非已投递）。
_handoff_cursor: "weakref.WeakKeyDictionary[Any, int]" = weakref.WeakKeyDictionary()


def _shutdown_record(session: Any) -> SessionTelemetryRecord:
    return SessionTelemetryRecord(
        channel="ops", time=_now_ms(), severity="info",
        attributes={"telemetry.op": "shutdown", "session.id": session.session_id},
        body={"op": "shutdown"})


def _now_ms() -> int:
    import time as _time
    return int(_time.time() * 1000)


def _severity_of(event: dict) -> str:
    """按事件自身结果位预映射告警级别（对齐 coordinator.severityOf）。"""
    if event.get("type") == "tool/result":
        try:
            content = event["data"]["message"]["content"]
            return "error" if content[0].get("isError") is True else "info"
        except (KeyError, IndexError, TypeError):
            return "info"
    if event.get("type") == "turn/end":
        try:
            return "error" if event["data"]["reason"].get("kind") == "error" else "info"
        except (KeyError, TypeError):
            return "info"
    return "info"


def _error_detail(error: Any) -> dict:
    if isinstance(error, BaseException):
        return {"name": type(error).__name__, "message": str(error)}
    if isinstance(error, dict):
        return {"name": str(error.get("name") or error.get("code") or "Error"),
                "message": str(error.get("message", error))}
    return {"name": "Error", "message": str(error)}


def _identity_of(session: Any, event: dict) -> dict:
    attributes = {
        "session.id": session.session_id,
        "session.format_version": SESSION_FORMAT_VERSION,
        "event.type": event.get("type"),
        "event.seq": event.get("seq"),
    }
    meta = getattr(session, "meta", {}) or {}
    if meta.get("cwd") is not None:
        attributes["session.cwd"] = meta["cwd"]
    if meta.get("parentSession") is not None:
        attributes["session.parent_id"] = meta["parentSession"]
    if meta.get("isSeeded"):
        attributes["session.seed_length"] = session.inherited_event_count
    return attributes


class SessionTelemetryCoordinator:
    """采集侧协调器（对齐上游 SessionTelemetryCoordinator）。"""

    def __init__(self, ctx: Context, backend: SessionTelemetrySink,
                 options: dict | None = None):
        self.ctx = ctx
        self.backend = backend
        self.options = options or {}
        self.adopted: set = set()
        self._capture = self.options.get("capture", "live")
        if self._capture == "live":
            ctx.on("session/created", lambda payload: self.adopt(payload["session"]))
            ctx.on("session/disposed", lambda payload: self._on_disposed(payload["session"]))
            ctx.on("session/event", lambda payload: self.contain(
                lambda: self._capture_event(payload["session"], payload["event"])))
            ctx.on("session/flush", lambda payload: self.contain(
                lambda: self._hint_flush(payload["session"])))
            ctx.on("agent/error", lambda payload: self.contain(
                lambda: self._relay_agent_error(payload)))
            sessions = ctx.get("sessions")
            if sessions is not None:
                for session in sessions.list():
                    self.adopt(session)
        ctx.effect(lambda: lambda: self._dispose(), "telemetry capture")

    # ---------- 采纳 / 采集 ----------

    def adopt(self, session: Any) -> None:
        if session in self.adopted:
            return
        self.adopted.add(session)
        self.capture_session(session)

    def capture_session(self, session: Any, through_seq: int | None = None) -> None:
        cursor = _handoff_cursor.get(session)
        if cursor is None:
            if self.options.get("includeHistory") is True:
                cursor = -1
            else:
                events = getattr(session, "events", []) or []
                cursor = events[-1]["seq"] if events else -1
        for event in list(getattr(session, "events", []) or []):
            seq = event.get("seq")
            if seq is None or seq <= cursor:
                continue
            if through_seq is not None and seq > through_seq:
                break
            self.contain(lambda event=event: self._capture_event(session, event))

    def _capture_event(self, session: Any, event: dict) -> None:
        record = self.redact(SessionTelemetryRecord(
            channel="ledger", time=event.get("time", _now_ms()),
            severity=_severity_of(event), attributes=_identity_of(session, event),
            body=event.get("data")))
        self._deliver(session, record, event.get("seq"))

    def redact(self, record: SessionTelemetryRecord) -> SessionTelemetryRecord:
        """运行 `session-telemetry/record` waterfall（本包无规则；部署挂载）。"""
        return self.ctx.waterfall("session-telemetry/record", record,
                                  base=lambda current: current)

    def _deliver(self, session: Any, record: SessionTelemetryRecord,
                 seq: int | None = None) -> None:
        self.backend.emit(record)
        if seq is not None:
            _handoff_cursor[session] = seq

    def _hint_flush(self, session: Any) -> None:
        if session in self.adopted:
            self.backend.flush()

    def _on_disposed(self, session: Any) -> None:
        self.contain(lambda: self._disposed(session))

    def _disposed(self, session: Any) -> None:
        if session not in self.adopted:
            return
        self.adopted.discard(session)
        self._deliver(session, self.redact(_shutdown_record(session)))

    def _relay_agent_error(self, payload: dict) -> None:
        agent = payload.get("agent")
        session = getattr(agent, "session", None)
        if session is None:
            return
        detail = _error_detail(payload.get("error"))
        record = self.redact(SessionTelemetryRecord(
            channel="ops", time=_now_ms(), severity="error",
            attributes={
                "telemetry.op": "agent-error",
                "session.id": session.session_id,
                "agent.id": str(getattr(agent, "agent_id", session.session_id)),
                "error.name": detail["name"],
                "turn": payload.get("turn"),
                "step": payload.get("step"),
            },
            body=detail))
        self._deliver(session, record)

    def contain(self, step: Callable[[], None]) -> None:
        """单个采集步的异常 containment：失败绝不逃逸到 loop。"""
        try:
            step()
        except BaseException as error:  # noqa: BLE001 - 采集侧必须自包含
            logger = self.ctx.root.logger
            if logger is not None:
                logger.warn(f"telemetry: capture step failed: {error}")

    def _dispose(self) -> None:
        for session in list(self.adopted):
            self.contain(lambda session=session: self._deliver(
                session, self.redact(_shutdown_record(session))))
        try:
            result = self.backend.shutdown()
            if hasattr(result, "__await__"):
                import asyncio
                try:
                    asyncio.get_running_loop()
                except RuntimeError:
                    asyncio.run(result)
        except BaseException as error:  # noqa: BLE001 - 最佳努力上报不得失败拆解
            logger = self.ctx.root.logger
            if logger is not None:
                logger.warn(f"telemetry: backend shutdown failed: {error}")
