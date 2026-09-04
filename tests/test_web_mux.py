"""web 测试：/api/remote.mux WebSocket 载体（RemoteStreamMuxConnection）。

对齐 `packages/api/gateway/src/stream-server.ts`：open/cancel 帧分发、item/end/
error 帧写出、binary→1003、非法/重复 open→1008、流外错误隔离。
"""
import asyncio
import json
import unittest

from miniharness.web.mux import RemoteStreamMuxConnection


class _FakeWs:
    def __init__(self):
        self.sent = []
        self.closed = None
        self.closed_reason = None
        self.input = asyncio.Queue()

    def send_text(self, text):
        self.sent.append(json.loads(text))
        return _noop()

    def receive(self):
        return self.input.get()

    def close(self, code=1000, reason=""):
        self.closed = code
        self.closed_reason = reason
        return _noop()


async def _noop():
    return None


class _FakeGateway:
    """可路由到带 fuzz 流的 fake gateway，模拟 open_stream 的分发行为。"""

    def __init__(self, values=("a", "b"), fail_open=None, fail_mid=None,
                 endpoints=None):
        self._values = values
        self._fail_open = fail_open
        self._fail_mid = fail_mid
        self._endpoints = endpoints or {}
        self.opened = []

    def open_stream(self, endpoint, payload, signal=None):
        self.opened.append((endpoint, payload))
        if self._fail_open is not None:
            raise self._fail_open()
        if endpoint in self._endpoints:
            return self._endpoints[endpoint]
        return self._stream(self._fail_mid)

    async def _stream(self, fail_mid):
        for i, value in enumerate(self._values):
            await asyncio.sleep(0)
            if fail_mid is not None and i == 1:
                raise fail_mid()
            yield value


class MuxConnectionTest(unittest.TestCase):
    def _run(self, coro):
        return asyncio.run(coro)

    def _drive(self, gateway, frames, wait_sent=None, then_disconnect=True):
        """喂客户端帧，等待 pump 产出 wait_sent 帧，然后断开。

        conn.run() 在 receive()（queue.get）上阻塞，故 pump 任务可并发写完帧。
        """
        async def go():
            ws = _FakeWs()
            for f in frames:
                await ws.input.put({"type": "websocket.receive", "text": json.dumps(f)})
            conn = RemoteStreamMuxConnection(gateway, ws)
            run_task = asyncio.ensure_future(conn.run())
            if wait_sent is not None:
                async def wait():
                    while len(ws.sent) < wait_sent:
                        await asyncio.sleep(0.005)
                try:
                    await asyncio.wait_for(wait(), timeout=2)
                except asyncio.TimeoutError:
                    pass
            if then_disconnect:
                try:
                    await ws.input.put({"type": "websocket.disconnect"})
                    await run_task
                except asyncio.CancelledError:
                    pass
            else:
                run_task.cancel()
            return ws
        return self._run(go())

    def test_open_yields_items_then_end(self):
        ws = self._drive(_FakeGateway(), [{"type": "open", "streamId": "s1",
                                           "endpoint": "e", "payload": {}}],
                         wait_sent=3)
        self.assertEqual([i["type"] for i in ws.sent], ["item", "item", "end"])
        self.assertEqual(ws.sent[0]["streamId"], "s1")
        self.assertEqual(ws.sent[0]["value"], "a")
        self.assertEqual(ws.sent[2], {"type": "end", "streamId": "s1"})

    def test_open_without_value_keeps_null_value_key(self):
        # item 帧 value 恒在（上游 `{type,streamId,value}` 构造后由
        # JSON.stringify 丢 undefined；null 是合法 wire 值不丢）
        ws = self._drive(_FakeGateway(values=(None,)),
                         [{"type": "open", "streamId": "s", "endpoint": "e",
                           "payload": {}}], wait_sent=2)
        self.assertEqual(ws.sent[0], {"type": "item", "streamId": "s", "value": None})

    def test_open_failure_emits_error_only(self):
        # error 即终态帧（上游 pump catch 只发 error、不补 end）
        gateway = _FakeGateway(fail_open=lambda: RuntimeError("boom"))
        ws = self._drive(gateway, [{"type": "open", "streamId": "s1",
                                    "endpoint": "e", "payload": {}}],
                         wait_sent=1)
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(ws.sent[0]["type"], "error")
        self.assertEqual(ws.sent[0]["error"]["code"], "gateway/internal")
        self.assertEqual(ws.sent[0]["error"]["message"], "boom")

    def test_midstream_failure_emits_error_only(self):
        def fail():
            raise RuntimeError("mid")
        ws = self._drive(_FakeGateway(fail_mid=fail),
                         [{"type": "open", "streamId": "s1", "endpoint": "e",
                           "payload": {}}], wait_sent=2)
        self.assertEqual(ws.sent[0]["value"], "a")
        self.assertEqual(ws.sent[1]["type"], "error")
        self.assertEqual(len(ws.sent), 2)

    def test_binary_message_closes_1003(self):
        async def go():
            ws = _FakeWs()
            await ws.input.put({"type": "websocket.receive", "bytes": b"\x01\x02"})
            conn = RemoteStreamMuxConnection(_FakeGateway(), ws)
            await conn.run()
            return ws.closed
        self.assertEqual(self._run(go()), 1003)

    def test_invalid_json_closes_1008(self):
        async def go():
            ws = _FakeWs()
            await ws.input.put({"type": "websocket.receive", "text": "not json"})
            conn = RemoteStreamMuxConnection(_FakeGateway(), ws)
            await conn.run()
            return ws.closed
        self.assertEqual(self._run(go()), 1008)

    def test_invalid_shape_closes_1008(self):
        async def go():
            ws = _FakeWs()
            await ws.input.put({"type": "websocket.receive",
                                "text": json.dumps({"type": "nope"})})
            conn = RemoteStreamMuxConnection(_FakeGateway(), ws)
            await conn.run()
            return ws.closed
        self.assertEqual(self._run(go()), 1008)

    def test_duplicate_open_closes_1008(self):
        gateway = _FakeGateway()
        async def go():
            ws = _FakeWs()
            await ws.input.put({"type": "websocket.receive",
                                "text": json.dumps({"type": "open", "streamId": "s1",
                                                    "endpoint": "e", "payload": {}})})
            await ws.input.put({"type": "websocket.receive",
                                "text": json.dumps({"type": "open", "streamId": "s1",
                                                    "endpoint": "e", "payload": {}})})
            conn = RemoteStreamMuxConnection(gateway, ws)
            await conn.run()
            return (ws.closed, ws.sent)
        closed, _ = self._run(go())
        self.assertEqual(closed, 1008)

    def test_cancel_stops_stream(self):
        async def go():
            ws = _FakeWs()
            gateway = _FakeGateway(values=tuple("abcdef"))
            await ws.input.put({"type": "websocket.receive",
                                "text": json.dumps({"type": "open", "streamId": "s1",
                                                    "endpoint": "e", "payload": {}})})
            await ws.input.put({"type": "websocket.receive",
                                "text": json.dumps({"type": "cancel", "streamId": "s1"})})
            await ws.input.put({"type": "websocket.disconnect"})
            conn = RemoteStreamMuxConnection(gateway, ws)
            await conn.run()
            return ws.sent
        sent = self._run(go())
        # cancel 后不再产出 end/item（任务被取消，天然不写 end）
        self.assertTrue(all(f["type"] != "end" for f in sent))

    def test_unknown_endpoint_fails_open(self):
        # FakeGateway 不拦截未知 endpoint（open_stream 直接产 item）；mux 不 close 连接
        gateway = _FakeGateway()
        ws = self._drive(gateway, [{"type": "open", "streamId": "s1",
                                    "endpoint": "nope", "payload": {}}],
                         wait_sent=1)
        self.assertEqual(ws.sent[0]["type"], "item")
        self.assertIsNone(ws.closed)


if __name__ == "__main__":
    unittest.main()
