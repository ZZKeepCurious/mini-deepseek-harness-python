"""session-telemetry + otel 验收（对齐 packages/session/session-telemetry{,-otel}）。

采集 coordinator 语义 + OTel 后端经共享 `ctx.otel` 服务的反馈授权上传。用真实
本地 OTLP 采集器（无 mock）。
"""
import os
import shutil
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.core.session.session import Session
from miniharness.telemetry.otel import install_otel
from miniharness.telemetry.session_telemetry import (
    SessionTelemetryCoordinator,
    SessionTelemetryRecord,
    _severity_of,
)
from miniharness.telemetry.session_telemetry_otel import (
    MODE_DISABLED,
    MODE_FEEDBACK_ONLY,
    WITHHELD_ENVELOPE_WARNING,
    OpenTelemetrySessionBackend,
)
from tests.otel_support import OtlpCollector, all_records, contents


class _Sink:
    """裸 SessionTelemetrySink 实现（coordinator 单测载体，非 mock）。"""

    def __init__(self):
        self.records = []
        self.flushes = 0
        self.shutdowns = 0

    def emit(self, record):
        self.records.append(record)

    def flush(self):
        self.flushes += 1

    async def shutdown(self):
        self.shutdowns += 1


class TestSeverity(unittest.TestCase):
    def test_maps_outcome_flags(self):
        self.assertEqual(_severity_of({
            "type": "tool/result",
            "data": {"message": {"content": [{"isError": True}]}}}), "error")
        self.assertEqual(_severity_of({
            "type": "tool/result",
            "data": {"message": {"content": [{"isError": False}]}}}), "info")
        self.assertEqual(_severity_of({"type": "turn/end", "data": {"reason": {"kind": "error"}}}),
                         "error")
        self.assertEqual(_severity_of({"type": "turn/end", "data": {"reason": {"kind": "completed"}}}),
                         "info")
        self.assertEqual(_severity_of({"type": "unknown"}), "info")


class TestCoordinator(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        self.sink = _Sink()
        self.coordinator = SessionTelemetryCoordinator(
            self.ctx, self.sink, {"capture": "live", "includeHistory": True})
        self.session = Session("s1", meta={"cwd": "/w"})

    def tearDown(self):
        self.ctx.dispose()

    def _event(self, seq: int, kind: str = "turn/start", data=None) -> dict:
        return {"type": kind, "seq": seq, "time": 1000 + seq, "data": data or {}}

    def _live(self, event: dict) -> None:
        self.ctx.emit("session/event", {"session": self.session, "event": event})

    def test_live_capture_identity_body_and_source_event(self):
        self.ctx.emit("session/created", {"session": self.session})
        self._live(self._event(0, data={"turn": 1}))
        ledger = [r for r in self.sink.records if r.channel == "ledger"]
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0].attributes["session.id"], "s1")
        self.assertEqual(ledger[0].attributes["event.seq"], 0)
        self.assertEqual(ledger[0].attributes["session.cwd"], "/w")
        self.assertEqual(ledger[0].body, {"turn": 1})
        self.assertEqual(ledger[0].source_event["sessionId"], "s1")
        self.assertEqual(ledger[0].source_event["envelope"]["type"], "turn/start")
        self.assertNotIn("data", ledger[0].source_event["envelope"])

    def test_redaction_waterfall_transforms(self):
        self.ctx.emit("session/created", {"session": self.session})

        def rule(record, next_):
            updated = next_()
            return SessionTelemetryRecord(
                channel=updated.channel, time=updated.time, severity=updated.severity,
                attributes={**updated.attributes, "redacted": 1}, body="<redacted>",
                source_event=updated.source_event)

        self.ctx.on("session-telemetry/record", rule)
        self._live(self._event(0))
        self.assertEqual(self.sink.records[0].body, "<redacted>")
        self.assertEqual(self.sink.records[0].attributes["redacted"], 1)

    def test_on_demand_replay_and_cursor(self):
        self.session = Session("s2", meta={})
        self.session.append("turn/start", {"turn": 1})
        self.session.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
        self.coordinator.capture_session(self.session)
        self.assertEqual(len(self.sink.records), 2)
        self.coordinator.capture_session(self.session)
        self.assertEqual(len(self.sink.records), 2, "cursor 应防重放")

    def test_dispose_emits_shutdown_and_shuts_backend(self):
        self.ctx.emit("session/created", {"session": self.session})
        self._live(self._event(0))
        self.ctx.dispose()
        self.assertEqual(self.sink.records[-1].attributes["telemetry.op"], "shutdown")
        self.assertEqual(self.sink.shutdowns, 1)

    def test_agent_error_relay(self):
        class _Agent:
            agent_id = "a1"

            def __init__(self, session):
                self.session = session

        agent = _Agent(self.session)
        self.ctx.emit("agent/error", {"agent": agent, "turn": 2, "step": 3,
                                      "error": ValueError("boom")})
        ops = [r for r in self.sink.records if r.channel == "ops"]
        self.assertEqual(ops[0].attributes["telemetry.op"], "agent-error")
        self.assertEqual(ops[0].attributes["error.name"], "ValueError")
        self.assertEqual(ops[0].severity, "error")

    def test_capture_step_failure_is_contained(self):
        self.sink.emit = lambda record: (_ for _ in ()).throw(RuntimeError("backend down"))
        self.ctx.emit("session/created", {"session": self.session})
        self._live(self._event(0))  # must not raise
        self.assertTrue(True)


class _BackendCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="mini-otel-home-")
        self._previous_home = os.environ.get("DSH_HOME")
        os.environ["DSH_HOME"] = self._tmp
        self.collector = OtlpCollector()
        self.warnings = []

    def tearDown(self):
        if self._previous_home is None:
            os.environ.pop("DSH_HOME", None)
        else:
            os.environ["DSH_HOME"] = self._previous_home
        shutil.rmtree(self._tmp, ignore_errors=True)
        self.collector.close()

    def _ctx(self):
        ctx = Context(name="root")
        install_otel(ctx)
        ctx.logger.exporter({
            "levels": {"default": 3},
            "export": lambda message: self.warnings.append(message),
        })
        return ctx

    def _warn_messages(self):
        return [message["args"] for message in self.warnings if message["type"] == "warn"]

    async def _dispose(self, ctx):
        result = ctx.dispose()
        if result is not None:
            await result

    def _backend(self, ctx, **overrides):
        config = {
            "mode": MODE_FEEDBACK_ONLY,
            "exporter": {"url": self.collector.url},
            "processor": {"scheduledDelayMillis": 1},
        }
        config.update(overrides)
        return OpenTelemetrySessionBackend(ctx, config)

    def _feedback_session(self, session_id="s1"):
        session = Session(session_id, meta={})
        session.append("turn/start", {"turn": 1})
        session.append("feedback/message-put", {"sessionId": session_id, "messageId": "m1"})
        return session


class TestOtelBackend(_BackendCase):
    async def test_disabled_warns_without_transport(self):
        ctx = Context(name="root")
        try:
            backend = OpenTelemetrySessionBackend(ctx, {"mode": MODE_DISABLED})
            self.assertEqual(backend.sharing, "disabled")
            session = Session("s1", meta={})
            session.append("feedback/message-put", {"sessionId": "s1", "messageId": "m1"})
            ctx.logger.exporter({
                "levels": {"default": 3},
                "export": lambda message: self.warnings.append(message),
            })
            ctx.emit("session/event", {"session": session, "event": session.events[-1]})
            self.assertIn(
                ("OpenTelemetry session upload is DISABLED; this feedback is not uploaded "
                 "through OpenTelemetry",), self._warn_messages())
        finally:
            await self._dispose(ctx)

    async def test_feedback_only_captures_authorized_prefix(self):
        ctx = self._ctx()
        try:
            backend = self._backend(ctx)
            self.assertEqual(backend.sharing, "feedback-only")
            session = self._feedback_session()
            ctx.emit("session/event", {"session": session, "event": session.events[-1]})
            await backend.shutdown()
            await self._dispose(ctx)
            types = []
            for entry in all_records(self.collector.snapshot()):
                for attribute in entry["record"].get("attributes", []):
                    if attribute["key"] == "event.type":
                        types.append(attribute["value"]["stringValue"])
            self.assertEqual(types, ["turn/start", "feedback/message-put"])
            for entry in all_records(self.collector.snapshot()):
                self.assertEqual(entry["record"]["eventName"], "session-log")
                self.assertEqual(entry["record"]["body"], {"stringValue": "session-log"})
        finally:
            shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_direct_emit_is_dropped(self):
        ctx = self._ctx()
        try:
            backend = self._backend(ctx)
            backend.emit(SessionTelemetryRecord("ledger", 1, "info", {}, {"direct": True}))
            await backend.shutdown()
            await self._dispose(ctx)
            self.assertEqual(self.collector.snapshot(), [])
        finally:
            shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_redaction_removing_source_event_withholds_record(self):
        ctx = self._ctx()
        try:
            backend = self._backend(ctx)

            def strip(record, next_):
                updated = next_()
                return SessionTelemetryRecord(
                    channel=updated.channel, time=updated.time, severity=updated.severity,
                    attributes=updated.attributes, body=updated.body, source_event=None)

            ctx.on("session-telemetry/record", strip)
            session = self._feedback_session()
            ctx.emit("session/event", {"session": session, "event": session.events[-1]})
            await backend.shutdown()
            await self._dispose(ctx)
            self.assertEqual(self.collector.snapshot(), [])
            self.assertIn((WITHHELD_ENVELOPE_WARNING,), self._warn_messages())
        finally:
            shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_max_request_bytes_rejects_oversized_record(self):
        ctx = self._ctx()
        try:
            backend = self._backend(ctx, maxRequestBytes=1)
            session = self._feedback_session()
            ctx.emit("session/event", {"session": session, "event": session.events[-1]})
            await backend.shutdown()
            await self._dispose(ctx)
            self.assertEqual(self.collector.snapshot(), [])
            self.assertTrue(any(
                args and args[0] == "Session log record rejected; content was not truncated"
                for args in self._warn_messages()))
        finally:
            shutil.rmtree(self._tmp, ignore_errors=True)

    async def test_url_validation(self):
        ctx = self._ctx()
        try:
            with self.assertRaisesRegex(ValueError, "exporter.url is required"):
                OpenTelemetrySessionBackend(ctx, {"mode": MODE_FEEDBACK_ONLY})
        finally:
            await self._dispose(ctx)
        ctx = self._ctx()
        try:
            with self.assertRaisesRegex(ValueError, "must be http"):
                OpenTelemetrySessionBackend(
                    ctx, {"mode": MODE_FEEDBACK_ONLY, "exporter": {"url": "ftp://x"}})
        finally:
            await self._dispose(ctx)

    async def test_rejects_unknown_mode(self):
        ctx = self._ctx()
        try:
            with self.assertRaisesRegex(ValueError, "unsupported mode"):
                OpenTelemetrySessionBackend(ctx, {"mode": "FULL"})
        finally:
            await self._dispose(ctx)


if __name__ == "__main__":
    unittest.main()
