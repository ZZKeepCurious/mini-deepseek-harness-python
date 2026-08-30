"""web 测试：$events 远程事件注册表（RemoteEventRegistry）+ api-session/* 转发源。

对齐上游 `packages/api/gateway` 的 remote-event 子系统：open→ready+续帧、
emit 广播、waterfall 结算（result/next/rejected/cancelled）、幂等结算、dispose
全量 cancelled。
"""
import asyncio
import unittest

from miniharness.web.events import EventSourceFailure, RemoteEventRegistry


def _run(coro):
    return asyncio.run(coro)


def _consume(agen, n):
    """从一个 async 生成器同步取出前 n 帧。"""
    async def take():
        frames = []
        it = agen.__aiter__()
        try:
            for _ in range(n):
                frames.append(await it.__anext__())
        except StopAsyncIteration:
            pass
        return frames
    return _run(take())


def _last_frame(coro_factory, timeout=2.0):
    """同步拿到一个 asyncio 任务的末帧（用于瀑布回调收集）。"""
    async def gather(coros):
        return await asyncio.gather(*coros)
    return coro_factory


class RemoteEventRegistryTest(unittest.TestCase):
    def setUp(self):
        self.reg = RemoteEventRegistry(home="C:/Users/me")

    def tearDown(self):
        self.reg.dispose()

    def test_open_rejects_non_empty_args(self):
        for payload in (None, "x", [], {}, {"args": "x"}, {"args": {1: 2}},
                        {"args": {}, "extra": 1}, {"args": None}):
            with self.assertRaises(EventSourceFailure):
                _consume(self.reg.open(payload), 1)

    def test_open_ready_frame(self):
        ok = {"args": {}}
        frames = _consume(self.reg.open(ok), 1)
        self.assertEqual(frames[0]["type"], "ready")
        self.assertTrue(frames[0]["clientId"])
        self.assertEqual(frames[0]["host"], {"home": "C:/Users/me"})

    def test_broadcast_reaches_open_clients(self):
        async def go():
            it = self.reg.open({"args": {}}).__aiter__()
            ready = await it.__anext__()
            self.reg.broadcast("api-session/added", {"sessionId": "s1"})
            frame = await it.__anext__()
            return ready, frame
        ready, frame = _run(go())
        self.assertEqual(frame["type"], "emit")
        self.assertEqual(frame["event"], "api-session/added")
        self.assertEqual(frame["args"], [{"sessionId": "s1"}])

    def test_broadcast_rejects_bad_event_name(self):
        with self.assertRaises(EventSourceFailure):
            self.reg.broadcast("", 1)
        with self.assertRaises(EventSourceFailure):
            self.reg.broadcast(123, 1)

    def test_invoke_result_roundtrip(self):
        async def go():
            client = self.reg.open({"args": {}}).__aiter__()
            await client.__anext__()  # ready
            task = asyncio.ensure_future(
                self.reg.invoke("approval/request", "s1", {"toolName": "bash"}))
            await asyncio.sleep(0)
            frame = await client.__anext__()
            self.assertEqual(frame["type"], "waterfall")
            self.assertEqual(frame["event"], "approval/request")
            self.assertEqual(frame["agentId"], "s1")
            self.assertEqual(frame["request"], {"toolName": "bash"})
            event_id = frame["eventId"]
            client_id = frame.get("clientId")
            self.assertIsNone(client_id)  # 帧内不须带 clientId；clientId 走 ready
            self.reg.receive_result({
                "clientId": self._any_client_id(), "eventId": event_id,
                "outcome": {"kind": "result", "value": "allowed-once"}})
            return await task
        kind, value = _run(go())
        self.assertEqual((kind, value), ("result", "allowed-once"))

    def _any_client_id(self):
        # 从注册表当前客户端里取第一个
        return next(iter(self.reg._clients))

    def test_invoke_no_client_hangs_then_cancelled_on_dispose(self):
        async def go():
            task = asyncio.ensure_future(
                self.reg.invoke("approval/request", "s1", {"toolName": "x"}))
            await asyncio.sleep(0)
            self.reg.dispose()
            return await task
        kind, value = _run(go())
        self.assertEqual(kind, "cancelled")
        self.assertEqual(value, "forwarded Remote event source was removed")

    def test_result_to_unknown_client_raises(self):
        with self.assertRaises(EventSourceFailure):
            self.reg.receive_result({
                "clientId": "nope", "eventId": "e1",
                "outcome": {"kind": "next"}})

    def test_result_for_unknown_pending_is_noop(self):
        async def go():
            client = self.reg.open({"args": {}}).__aiter__()
            ready = await client.__anext__()
            # 无 pending：settle 走 unknown → noop
            self.reg.receive_result({
                "clientId": ready["clientId"], "eventId": "missing",
                "outcome": {"kind": "next"}})
            return True
        self.assertTrue(_run(go()))

    def test_rejected_maps_and_next_settles(self):
        async def go():
            client = self.reg.open({"args": {}}).__aiter__()
            await client.__anext__()
            task = asyncio.ensure_future(
                self.reg.invoke("approval/request", "s1", {"toolName": "x"}))
            await asyncio.sleep(0)
            frame = await client.__anext__()
            self.reg.receive_result({
                "clientId": self._any_client_id(), "eventId": frame["eventId"],
                "outcome": {"kind": "rejected",
                            "error": {"name": "Err", "message": "nope"}}})
            kind, value = await task
            return kind, value
        kind, value = _run(go())
        self.assertEqual(kind, "rejected")
        self.assertEqual(value, {"name": "Err", "message": "nope"})

    def test_next_settles_after_all_deliveries(self):
        async def go():
            task = asyncio.ensure_future(
                self.reg.invoke("approval/request", "s1", {"toolName": "x"}))
            await asyncio.sleep(0)
            # 无客户端；交付集合空 → 收到 result 前 next() 结算 'next'
            client = self.reg.open({"args": {}}).__aiter__()
            ready = await client.__anext__()
            self.reg.receive_result({
                "clientId": ready["clientId"],
                "eventId": next(iter(self.reg._pending)),
                "outcome": {"kind": "next"}})
            return await task
        kind, value = _run(go())
        self.assertEqual((kind, value), ("next", None))

    def test_dispose_with_pending_client_cancels_and_ends(self):
        async def go():
            client = self.reg.open({"args": {}}).__aiter__()
            ready = await client.__anext__()
            task = asyncio.ensure_future(
                self.reg.invoke("approval/request", "s1", {"toolName": "x"}))
            await asyncio.sleep(0)
            await client.__anext__()  # waterfall
            self.reg.dispose()
            return await task
        kind, value = _run(go())
        self.assertEqual(kind, "cancelled")


if __name__ == "__main__":
    unittest.main()
