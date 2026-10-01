"""测试支持：真实 OTLP/HTTP 采集器（无 mock，线程内跑 stdlib http.server）。

对齐上游 telemetry 测试的 `collector()` 辅助：捕获每个请求的头/原始字节/JSON，
可按序返回状态码，可用 threading.Event 门控响应（模拟在途传输）。
"""
from __future__ import annotations

import gzip
import http.server
import json
import threading


class OtlpCollector:
    """一个本地 OTLP logs 采集器。"""

    def __init__(self, statuses=None, delay=0.0):
        self.captures = []
        self._lock = threading.Lock()
        self._statuses = list(statuses) if statuses else []
        self.delay = delay
        self.arrived = threading.Event()
        self.gate = None  # 置为 threading.Event 则每次响应前等待其 set

        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        server.collector = self
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()
        self.port = server.server_address[1]

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/v1/logs"

    def record(self, headers, raw):
        body = raw
        if headers.get("Content-Encoding") == "gzip" or headers.get("content-encoding") == "gzip":
            body = gzip.decompress(raw)
        parsed = json.loads(body.decode("utf-8")) if body else None
        with self._lock:
            self.captures.append({"headers": headers, "body": parsed, "bytes": len(body)})
            self.arrived.set()

    def next_status(self) -> int:
        with self._lock:
            if self._statuses:
                return self._statuses.pop(0)
        return 200

    def snapshot(self):
        with self._lock:
            return list(self.captures)

    def close(self):
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        collector = self.server.collector
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        collector.record(dict(self.headers), raw)
        gate = collector.gate
        if gate is not None:
            gate.wait(timeout=5)
        if collector.delay:
            import time
            time.sleep(collector.delay)
        status = collector.next_status()
        payload = b"{}"
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args):  # silence
        return


def resource_logs(capture):
    return capture["body"]["resourceLogs"]


def all_records(captures):
    records = []
    for capture in captures:
        for resource in capture["body"]["resourceLogs"]:
            for scope_log in resource["scopeLogs"]:
                for record in scope_log["logRecords"]:
                    records.append({"scope": scope_log["scope"], "record": record})
    return records


def contents(captures):
    """取所有 logRecord 的 content 属性（JSON 串）。"""
    out = []
    for capture in captures:
        for resource in capture["body"]["resourceLogs"]:
            for scope_log in resource["scopeLogs"]:
                for record in scope_log["logRecords"]:
                    for attribute in record.get("attributes", []):
                        if attribute["key"] == "content":
                            out.append(attribute["value"]["stringValue"])
    return out
