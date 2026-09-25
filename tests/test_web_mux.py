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
    """可路由到带 fuzz 流的 fake gateway，模拟 open_stream 的分发行为。

    `open_stream` 签名对齐上游 `RemoteStreamOpener`（endpoint, payload, uplink, peer,
    control）里 mini 承载的部分：上行 inbox 交到端点手里，端点可读可不读。
    """

    def __init__(self, values=("a", "b"), fail_open=None, fail_mid=None,
                 endpoints=None, read_uplink=False, hang=False):
        self._values = values
        self._fail_open = fail_open
        self._fail_mid = fail_mid
        self._endpoints = endpoints or {}
        self._read_uplink = read_uplink
        self._hang = hang
        self.opened = []
        self.uplinks = []

    def open_stream(self, endpoint, payload, uplink=None, signal=None):
        self.opened.append((endpoint, payload))
        self.uplinks.append(uplink)
        if self._fail_open is not None:
            raise self._fail_open()
        if endpoint in self._endpoints:
            return self._endpoints[endpoint]
        if self._read_uplink:
            return self._echo_uplink(uplink)
        if self._hang:
            return _never()
        return self._stream(self._fail_mid)

    async def _stream(self, fail_mid):
        for i, value in enumerate(self._values):
            await asyncio.sleep(0)
            if fail_mid is not None and i == 1:
                raise fail_mid()
            yield value

    async def _echo_uplink(self, uplink):
        """读端点：把客户端 item 帧回显为下行 value（上游 `invocation.uplink()` 消费面）。"""
        if uplink is None:
            for value in self._values:
                yield value
            return
        async for value in uplink:
            yield {"echo": value}


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


class MuxUplinkTest(unittest.TestCase):
    """上行帧的载体行为（上游 stream-server.ts UplinkInbox + pump 的 control 分支）。"""

    def _run(self, coro):
        return asyncio.run(coro)

    def _drive(self, gateway, frames, wait_sent, inbox_bytes=None, later_frames=()):
        async def go():
            ws = _FakeWs()
            for f in frames:
                await ws.input.put({"type": "websocket.receive", "text": json.dumps(f)})
            kwargs = {} if inbox_bytes is None else {"stream_inbox_bytes": inbox_bytes}
            conn = RemoteStreamMuxConnection(gateway, ws, **kwargs)
            run_task = asyncio.ensure_future(conn.run())

            async def wait():
                while len(ws.sent) < wait_sent:
                    await asyncio.sleep(0.005)
            try:
                await asyncio.wait_for(wait(), timeout=2)
            except asyncio.TimeoutError:
                pass
            for f in later_frames:
                await ws.input.put({"type": "websocket.receive", "text": json.dumps(f)})
            await asyncio.sleep(0.05)
            await ws.input.put({"type": "websocket.disconnect"})
            try:
                await run_task
            except asyncio.CancelledError:
                pass
            return ws
        return self._run(go())

    def test_uplink_item_reaches_the_endpoint(self):
        # 读上行端点：客户端 item 帧被端点读到并回显为下行 value
        ws = self._drive(_FakeGateway(read_uplink=True),
                         [{"type": "open", "streamId": "s1", "endpoint": "e",
                           "payload": {}},
                          {"type": "item", "streamId": "s1", "value": {"k": 1}},
                          {"type": "end", "streamId": "s1"}], wait_sent=2)
        self.assertEqual(ws.sent[0], {"type": "item", "streamId": "s1",
                                      "value": {"echo": {"k": 1}}})
        self.assertEqual(ws.sent[1], {"type": "end", "streamId": "s1"})

    def test_item_after_end_fails_stream_with_gateway_protocol(self):
        # 流仍活着（hang 端点）时 end 之后的 item 才算违例；下行已定的流之后
        # 的上行帧不改结局（上游 push 的 failure/closed 早退）
        ws = self._drive(_FakeGateway(hang=True),
                         [{"type": "open", "streamId": "s1", "endpoint": "e",
                           "payload": {}},
                          {"type": "end", "streamId": "s1"},
                          {"type": "item", "streamId": "s1", "value": 1}],
                         wait_sent=1)
        self.assertEqual(ws.sent[-1]["type"], "error")
        self.assertEqual(ws.sent[-1]["error"]["code"], "gateway/protocol")
        self.assertEqual(ws.sent[-1]["error"]["details"], {"endpoint": "e"})
        self.assertIsNone(ws.closed)

    def test_item_after_a_finished_stream_is_dropped(self):
        ws = self._drive(_FakeGateway(),
                         [{"type": "open", "streamId": "s1", "endpoint": "e",
                           "payload": {}}], wait_sent=3,
                         later_frames=[{"type": "end", "streamId": "s1"},
                                       {"type": "item", "streamId": "s1", "value": 1}])
        self.assertEqual([f["type"] for f in ws.sent], ["item", "item", "end"])
        self.assertIsNone(ws.closed)

    def test_uplink_overflow_fails_stream(self):
        # 缓冲上限按整帧 UTF-8 字节计：16 字节上限放不下第二条 item
        ws = self._drive(_FakeGateway(hang=True),
                         [{"type": "open", "streamId": "s1", "endpoint": "e",
                           "payload": {}},
                          {"type": "item", "streamId": "s1", "value": "1234567890"},
                          {"type": "item", "streamId": "s1", "value": "1234567890"}],
                         wait_sent=1, inbox_bytes=16)
        self.assertEqual(len(ws.sent), 1)
        self.assertEqual(ws.sent[0]["type"], "error")
        self.assertEqual(ws.sent[0]["error"]["code"], "gateway/uplink-overflow")
        self.assertIn("16 buffered bytes", ws.sent[0]["error"]["message"])
        self.assertIsNone(ws.closed)

    def test_uplink_frames_for_unknown_stream_are_dropped(self):
        # 已结束的流：客户端在途 item/end 帧丢弃，不报错（上游 receive 的
        # `this.streams.get(id)?.inbox` 可选链）
        ws = self._drive(_FakeGateway(),
                         [{"type": "item", "streamId": "gone", "value": 1},
                          {"type": "end", "streamId": "gone"},
                          {"type": "cancel", "streamId": "gone"}], wait_sent=1)
        self.assertEqual(ws.sent, [])
        self.assertIsNone(ws.closed)

    def test_released_uplink_drops_items_without_violation(self):
        # `$events` 是网关自有流：open 时释放 inbox（上游 openWireStream），
        # 之后的 item 帧直接丢弃——不记字节、不触发 overflow
        async def go():
            ws = _FakeWs()
            released = []

            def open_stream(endpoint, payload, uplink=None, signal=None):
                released.append(uplink)
                uplink.release()
                return _never()

            gateway = _FakeGateway()
            gateway.open_stream = open_stream
            for f in ({"type": "open", "streamId": "s1", "endpoint": "$events",
                       "payload": {"args": {}}},
                      {"type": "item", "streamId": "s1", "value": "1234567890123"},
                      {"type": "item", "streamId": "s1", "value": "1234567890123"}):
                await ws.input.put({"type": "websocket.receive", "text": json.dumps(f)})
            conn = RemoteStreamMuxConnection(gateway, ws, stream_inbox_bytes=8)
            run_task = asyncio.ensure_future(conn.run())
            await asyncio.sleep(0.05)
            await ws.input.put({"type": "websocket.disconnect"})
            try:
                await run_task
            except asyncio.CancelledError:
                pass
            return ws, released
        ws, released = self._run(go())
        self.assertEqual(len(released), 1)
        self.assertEqual(ws.sent, [])
        self.assertIsNone(ws.closed)

    def test_cancel_ends_a_pending_uplink_read(self):
        # 读上行端点挂在 inbox 上：客户端 cancel 结束挂起读，不发终态帧
        async def go():
            ws = _FakeWs()
            gateway = _FakeGateway(read_uplink=True)
            for f in ({"type": "open", "streamId": "s1", "endpoint": "e",
                       "payload": {}},
                      {"type": "cancel", "streamId": "s1"}):
                await ws.input.put({"type": "websocket.receive", "text": json.dumps(f)})
            conn = RemoteStreamMuxConnection(gateway, ws)
            run_task = asyncio.ensure_future(conn.run())
            await asyncio.sleep(0.05)
            await ws.input.put({"type": "websocket.disconnect"})
            try:
                await run_task
            except asyncio.CancelledError:
                pass
            return ws
        ws = self._run(go())
        self.assertEqual(ws.sent, [])


async def _never():
    await asyncio.Event().wait()
    yield None


if __name__ == "__main__":
    unittest.main()
