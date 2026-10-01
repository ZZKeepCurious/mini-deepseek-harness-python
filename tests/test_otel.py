"""共享 otel 服务验收（对齐 packages/telemetry/otel）。

用真实本地 OTLP 采集器（stdlib http.server 线程）验证：会话日志 wire 形状与
字节界、普通事件通道、独立通道、传输重试/上限/显式头。无 mock。
"""
import asyncio
import json
import threading
import unittest

from miniharness.core.scope import Context
from miniharness.telemetry.otel import (
    EventLogReporter,
    OTel,
    OTelEventRecord,
    SessionLogRecord,
    SessionLogReporter,
    install_otel,
    resolve_session_log_limits,
)
from miniharness.telemetry.otel_transport import (
    MAX_RETRIES,
    OtlpJsonTransport,
    OtlpLogRecord,
    encode_logs_request,
)
from miniharness.telemetry.product_telemetry_otel import (
    ProductTelemetry,
    resolve_product_telemetry_config,
)
from tests.otel_support import OtlpCollector, all_records, contents


def session_event(text, seq=0, time=1_800_000_000_000):
    return {
        "type": "user/message", "seq": seq, "time": time, "surfaceOp": "append",
        "data": {"content": [{"type": "text", "text": text}]},
    }


class _ReporterCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.collector = OtlpCollector()
        self.failures = []

    def tearDown(self):
        self.collector.close()

    def _reporter(self, cls=SessionLogReporter, **overrides):
        options = {
            "scope": {"name": "session-telemetry-otel", "version": "9.9.9"},
            "exporter": {"url": self.collector.url},
            "resourceAttributes": {"service.name": "session-test"},
            "processor": {"scheduledDelayMillis": 60000},
            "onFailure": lambda message, error=None: self.failures.append((message, error)),
        }
        options.update(overrides)
        return cls(options)


class TestSessionLogWire(_ReporterCase):
    async def test_preserves_full_event_as_content_attribute(self):
        reporter = self._reporter()
        record = SessionLogRecord(
            session_id="synthetic-session",
            event=session_event('中文\n"quote"\\path 😀'))
        reporter.report_session_log(record)
        await reporter.shutdown()

        captures = self.collector.snapshot()
        self.assertEqual(len(captures), 1)
        records = all_records(captures)
        self.assertEqual(len(records), 1)
        wire = records[0]["record"]
        self.assertEqual(wire["eventName"], "session-log")
        self.assertEqual(wire["body"], {"stringValue": "session-log"})
        self.assertEqual(wire["severityNumber"], 9)
        self.assertEqual(wire["severityText"], "INFO")
        self.assertEqual(wire["timeUnixNano"], str(1_800_000_000_000 * 1_000_000))
        attributes = {a["key"]: a["value"] for a in wire["attributes"]}
        self.assertEqual(attributes["sessionId"], {"stringValue": "synthetic-session"})
        self.assertEqual(json.loads(attributes["content"]["stringValue"]), record.event)
        self.assertIn("中文", attributes["content"]["stringValue"])
        self.assertEqual(records[0]["scope"],
                         {"name": "session-telemetry-otel", "version": "9.9.9"})
        resource = captures[0]["body"]["resourceLogs"][0]["resource"]["attributes"]
        resource_map = {a["key"]: a["value"] for a in resource}
        self.assertEqual(resource_map["service.name"], {"stringValue": "session-test"})

    async def test_integer_attributes_are_proto3_strings(self):
        reporter = self._reporter(
            resourceAttributes={"service.name": "s", "session.format_version": 4})
        reporter.report_session_log(SessionLogRecord("s", session_event("x")))
        await reporter.shutdown()
        resource = self.collector.snapshot()[0]["body"]["resourceLogs"][0]["resource"]["attributes"]
        resource_map = {a["key"]: a["value"] for a in resource}
        self.assertEqual(resource_map["session.format_version"], {"intValue": "4"})

    async def test_oversized_record_is_rejected_without_truncation(self):
        reporter = self._reporter(maxRequestBytes=1)
        reporter.report_session_log(SessionLogRecord("s", session_event("x")))
        await reporter.shutdown()
        self.assertEqual(self.collector.snapshot(), [])
        self.assertEqual(self.failures[0][0], "Session log record rejected; content was not truncated")
        self.assertIsInstance(self.failures[0][1], BaseException)

    async def test_batches_stay_within_byte_limit_and_preserve_order(self):
        reporter = self._reporter(maxRequestBytes=1500)
        events = [session_event("y" * 500, seq=seq) for seq in range(3)]
        for event in events:
            reporter.report_session_log(SessionLogRecord("s", event))
        await reporter.shutdown()
        captures = self.collector.snapshot()
        self.assertGreater(len(captures), 1)
        self.assertTrue(all(capture["bytes"] <= 1500 for capture in captures))
        self.assertEqual([json.loads(text) for text in contents(captures)], events)

    async def test_explicit_headers_are_sent_and_not_inherited(self):
        reporter = self._reporter(exporter={
            "url": self.collector.url,
            "headers": {"authorization": "Bearer test-token", "x-channel": "test-channel"},
        })
        reporter.report_session_log(SessionLogRecord("s", session_event("x")))
        await reporter.shutdown()
        headers = self.collector.snapshot()[0]["headers"]
        self.assertEqual(headers["authorization"], "Bearer test-token")
        self.assertEqual(headers["x-channel"], "test-channel")

    async def test_queue_full_is_rejected(self):
        gate = threading.Event()
        self.collector.gate = gate
        reporter = self._reporter(
            processor={"maxExportBatchSize": 1, "maxQueueSize": 2, "scheduledDelayMillis": 60000})
        for seq in range(4):
            reporter.report_session_log(SessionLogRecord("s", session_event("x", seq=seq)))
        await asyncio.sleep(0.05)
        gate.set()
        await reporter.shutdown()
        self.assertIn(("Session log queue is full; record rejected", None), self.failures)

    async def test_failed_http_request_does_not_block_later_records(self):
        collector = OtlpCollector(statuses=[400])
        try:
            reporter = SessionLogReporter({
                "scope": {"name": "s"}, "exporter": {"url": collector.url},
                "resourceAttributes": {}, "processor": {"maxExportBatchSize": 1,
                                                        "scheduledDelayMillis": 60000},
                "onFailure": lambda message, error=None: self.failures.append((message, error)),
            })
            reporter.report_session_log(SessionLogRecord("s", session_event("first")))
            reporter.report_session_log(SessionLogRecord("s", session_event("second", seq=1)))
            await reporter.shutdown()
            texts = contents(collector.snapshot())
            self.assertEqual([json.loads(text)["data"]["content"][0]["text"] for text in texts],
                             ["first", "second"])
            self.assertIn("Session log export failed", [m for m, _ in self.failures])
        finally:
            collector.close()

    async def test_export_timeout_warns_without_releasing_slot(self):
        gate = threading.Event()
        self.collector.gate = gate
        reporter = self._reporter(
            processor={"maxExportBatchSize": 1, "exportTimeoutMillis": 100,
                       "scheduledDelayMillis": 60000})
        reporter.report_session_log(SessionLogRecord("s", session_event("x")))
        await asyncio.sleep(0.3)
        self.assertIn(
            ("Session log request exceeded exportTimeoutMillis; waiting for transport settlement", None),
            self.failures)
        gate.set()
        await reporter.shutdown()

    async def test_stop_pending_drops_queue(self):
        reporter = self._reporter()
        reporter.report_session_log(SessionLogRecord("s", session_event("x")))
        reporter.stop_pending()
        reporter.report_session_log(SessionLogRecord("s", session_event("late", seq=1)))
        await reporter.shutdown()
        self.assertEqual(self.collector.snapshot(), [])


class TestEventLogReporter(_ReporterCase):
    async def test_emits_ordinary_event(self):
        reporter = self._reporter(cls=EventLogReporter, scope={"name": "ordinary", "version": "1"})
        reporter.emit(OTelEventRecord(event_name="ui.click", body="click", timestamp=1234))
        await reporter.shutdown()
        records = all_records(self.collector.snapshot())
        self.assertEqual(len(records), 1)
        wire = records[0]["record"]
        self.assertEqual(wire["eventName"], "ui.click")
        self.assertEqual(wire["body"], {"stringValue": "click"})
        self.assertEqual(wire["timeUnixNano"], str(1234 * 1_000_000))
        self.assertIn("observedTimeUnixNano", wire)


class TestSharedService(unittest.IsolatedAsyncioTestCase):
    async def test_mount_allocates_nothing_and_keeps_channels_independent(self):
        collector = OtlpCollector()
        ctx = Context(name="root")
        try:
            otel = install_otel(ctx)
            self.assertIsInstance(otel, OTel)
            self.assertIs(ctx.get("otel"), otel)
            ordinary = otel.create_event_reporter({
                "exporter": {"url": collector.url},
                "resourceAttributes": {"service.name": "ordinary-test"},
                "scope": {"name": "ordinary-consumer", "version": "1"},
                "processor": {"scheduledDelayMillis": 60000},
                "onFailure": lambda message, error=None: (_ for _ in ()).throw(Exception(message)),
            })
            sender = otel.create_session_log_reporter({
                "scope": {"name": "session-telemetry-otel"},
                "exporter": {"url": collector.url},
                "resourceAttributes": {"service.name": "session-test"},
                "processor": {"scheduledDelayMillis": 60000},
                "onFailure": lambda message, error=None: None,
            })
            ordinary.emit(OTelEventRecord(event_name="ui.click", body="click", timestamp=1))
            sender.report_session_log(SessionLogRecord("s", session_event("authorized")))
            self.assertEqual(collector.snapshot(), [])
            await sender.shutdown()
            names = [r["record"].get("eventName") for r in all_records(collector.snapshot())]
            self.assertEqual(names, ["session-log"])
            await ordinary.shutdown()
            names = [r["record"].get("eventName") for r in all_records(collector.snapshot())]
            self.assertEqual(names, ["session-log", "ui.click"])
        finally:
            collector.close()
            result = ctx.dispose()
            if result is not None:
                await result


class TestProductTelemetry(unittest.IsolatedAsyncioTestCase):
    async def test_emit_reaches_collector_with_channel(self):
        collector = OtlpCollector()
        ctx = Context(name="root")
        try:
            install_otel(ctx)
            product = ProductTelemetry(ctx, {
                "endpoint": collector.url, "channel": "test-channel",
                "serviceName": "app", "serviceVersion": "1.0",
                "scheduledDelayMillis": 60000,
            })
            product.emit(OTelEventRecord(event_name="ui.click", body="click", timestamp=1))
            result = ctx.dispose()
            if result is not None:
                await result
            captures = collector.snapshot()
            self.assertEqual(captures[0]["headers"]["x-channel"], "test-channel")
            names = [entry["record"]["eventName"] for entry in all_records(captures)]
            self.assertEqual(names, ["ui.click"])
        finally:
            collector.close()

    def test_config_validation(self):
        with self.assertRaisesRegex(ValueError, "endpoint must use HTTP"):
            resolve_product_telemetry_config({
                "endpoint": "ftp://x", "serviceName": "a", "serviceVersion": "1"})
        with self.assertRaisesRegex(ValueError, "valid HTTP header"):
            resolve_product_telemetry_config({
                "serviceName": "a", "serviceVersion": "1", "channel": "bad\nvalue"})
        with self.assertRaisesRegex(ValueError, "serviceName is required"):
            resolve_product_telemetry_config({"serviceVersion": "1"})
        with self.assertRaisesRegex(ValueError, "must not exceed maxQueueSize"):
            resolve_product_telemetry_config({
                "serviceName": "a", "serviceVersion": "1",
                "maxExportBatchSize": 4, "maxQueueSize": 2})

    def test_defaults(self):
        resolved = resolve_product_telemetry_config({
            "serviceName": "a", "serviceVersion": "1"})
        self.assertEqual(resolved["channel"], "dsh_otel_report")
        self.assertEqual(resolved["maxQueueSize"], 2048)


class TestSessionLogLimits(unittest.TestCase):
    def test_rejects_out_of_range_max_request_bytes(self):
        for value in (0, -1, 1.5, 4_000_001, float("inf")):
            with self.assertRaisesRegex(ValueError, "maxRequestBytes"):
                resolve_session_log_limits({"maxRequestBytes": value})

    def test_rejects_invalid_processor_values(self):
        for key in ("maxQueueSize", "maxExportBatchSize", "scheduledDelayMillis",
                    "exportTimeoutMillis"):
            with self.assertRaisesRegex(ValueError, f"processor.{key}"):
                resolve_session_log_limits({"processor": {key: 0}})

    def test_rejects_batch_above_queue(self):
        with self.assertRaisesRegex(ValueError, "must not exceed maxQueueSize"):
            resolve_session_log_limits({"processor": {"maxQueueSize": 1, "maxExportBatchSize": 2}})

    def test_default_is_four_million(self):
        from miniharness.telemetry.otel import SESSION_LOG_MAX_REQUEST_BYTES
        self.assertEqual(resolve_session_log_limits({}), SESSION_LOG_MAX_REQUEST_BYTES)


class TestTransport(unittest.IsolatedAsyncioTestCase):
    async def test_retries_retryable_statuses_then_succeeds(self):
        collector = OtlpCollector(statuses=[503, 502, 429, 504, 503, 200])
        try:
            transport = OtlpJsonTransport(collector.url, retry_backoff_base=0.0)
            await transport.export(_payload())
            await transport.shutdown()
            self.assertEqual(len(collector.snapshot()), 6)
        finally:
            collector.close()

    async def test_gives_up_after_retry_limit(self):
        collector = OtlpCollector(statuses=[503] * 10)
        try:
            transport = OtlpJsonTransport(collector.url, retry_backoff_base=0.0)
            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                await transport.export(_payload())
            await transport.shutdown()
            self.assertEqual(len(collector.snapshot()), MAX_RETRIES + 1)
        finally:
            collector.close()

    async def test_gzip_compression_sets_header(self):
        collector = OtlpCollector()
        try:
            transport = OtlpJsonTransport(collector.url, compression="gzip")
            await transport.export(_payload())
            await transport.shutdown()
            self.assertEqual(collector.snapshot()[0]["headers"]["content-encoding"], "gzip")
        finally:
            collector.close()


def _payload() -> bytes:
    return encode_logs_request(
        {"service.name": "t"}, "scope", None,
        [OtlpLogRecord(timestamp=1, severity_number=9, body="x")])


if __name__ == "__main__":
    unittest.main()
