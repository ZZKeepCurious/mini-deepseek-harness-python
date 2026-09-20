"""session-telemetry + otel 验收（对齐 packages/session/session-telemetry{,-otel}）。"""
import unittest

from miniharness.core.scope import Context
from miniharness.core.session.session import Session
from miniharness.telemetry.session_telemetry import (
    SessionTelemetryCoordinator,
    SessionTelemetryRecord,
    _severity_of,
)
from miniharness.telemetry.session_telemetry_otel import (
    MODE_DISABLED,
    MODE_FEEDBACK_ONLY,
    OpenTelemetrySessionBackend,
)


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

    def test_live_capture_identity_and_body(self):
        self.ctx.emit("session/created", {"session": self.session})
        self._live(self._event(0, data={"turn": 1}))
        ledger = [r for r in self.sink.records if r.channel == "ledger"]
        self.assertEqual(len(ledger), 1)
        self.assertEqual(ledger[0].attributes["session.id"], "s1")
        self.assertEqual(ledger[0].attributes["event.seq"], 0)
        self.assertEqual(ledger[0].attributes["session.cwd"], "/w")
        self.assertEqual(ledger[0].body, {"turn": 1})

    def test_redaction_waterfall_transforms(self):
        self.ctx.emit("session/created", {"session": self.session})

        def rule(record, next_):
            updated = next_()
            return SessionTelemetryRecord(
                channel=updated.channel, time=updated.time, severity=updated.severity,
                attributes={**updated.attributes, "redacted": 1}, body="<redacted>")

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


class _OtelCase(unittest.TestCase):
    def _provider(self):
        from opentelemetry.sdk._logs import (
            LoggerProvider,
            SynchronousMultiLogRecordProcessor,
        )
        from opentelemetry.sdk._logs.export import (
            InMemoryLogRecordExporter,
            SimpleLogRecordProcessor,
        )
        from opentelemetry.sdk.resources import Resource
        self.exporter = InMemoryLogRecordExporter()
        multi = SynchronousMultiLogRecordProcessor()
        multi.add_log_record_processor(SimpleLogRecordProcessor(self.exporter))
        return LoggerProvider(resource=Resource.create({"service.name": "test"}),
                              multi_log_record_processor=multi)


class TestOtelBackend(_OtelCase):
    def test_disabled_warns_without_provider(self):
        ctx = Context(name="root")
        try:
            backend = OpenTelemetrySessionBackend(ctx, {"mode": MODE_DISABLED})
            self.assertEqual(backend.sharing, "disabled")
            session = Session("s1", meta={})
            session.append("feedback/message-put", {"sessionId": "s1", "messageId": "m1"})
            ctx.emit("session/event", {"session": session, "event": session.events[-1]})
        finally:
            ctx.dispose()

    def test_feedback_only_captures_on_feedback(self):
        ctx = Context(name="root")
        try:
            backend = OpenTelemetrySessionBackend(
                ctx, {"mode": MODE_FEEDBACK_ONLY}, provider=self._provider())
            self.assertEqual(backend.sharing, "feedback-only")
            session = Session("s1", meta={})
            session.append("turn/start", {"turn": 1})
            session.append("feedback/message-put", {"sessionId": "s1", "messageId": "m1"})
            feedback = session.events[-1]
            ctx.emit("session/event", {"session": session, "event": feedback})
            backend._provider.force_flush()
            logs = [r.log_record for r in self.exporter.get_finished_logs()]
            self.assertEqual(len(logs), 2)
            self.assertEqual(logs[0].attributes["event.type"], "turn/start")
            self.assertEqual(logs[1].body["sessionId"], "s1")
            backend.emit(SessionTelemetryRecord("ledger", 1, "info", {}, {}))
        finally:
            ctx.dispose()

    def test_url_validation(self):
        with self.assertRaisesRegex(ValueError, "exporter.url is required"):
            ctx = Context(name="u1")
            try:
                OpenTelemetrySessionBackend(ctx, {"mode": MODE_FEEDBACK_ONLY})
            finally:
                ctx.dispose()
        with self.assertRaisesRegex(ValueError, "must be http"):
            ctx = Context(name="u2")
            try:
                OpenTelemetrySessionBackend(
                    ctx, {"mode": MODE_FEEDBACK_ONLY, "exporter": {"url": "ftp://x"}})
            finally:
                ctx.dispose()


if __name__ == "__main__":
    unittest.main()
