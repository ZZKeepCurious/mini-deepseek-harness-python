"""web 流方法面测试：GatewayStreams 的 session.follow / session.control 帧契约。

对齐上游 `packages/api/session-controller`（follow/control 宿主流）+ `web/mux.py`
的分发：`open_stream(endpoint, payload)` 返回 async 生成器（帧 value 序列），
依次驱动验证 snapshot/baseline 起始帧、实时 event/queue 续帧、参数/未知会话/
未知 endpoint 的 fail-closed。WS 载体本身在 test_web_mux 覆盖。
"""
import asyncio
import os
import unittest

from miniharness.core.scope import Context
from miniharness.core.session import create_message, text_block
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import WebApi
from miniharness.web.streams import GatewayStreams, RemoteStreamError


def _run(coro):
    return asyncio.run(coro)


def _fake(model: str = "fake-model") -> FakeLlmAdapter:
    adapter = FakeLlmAdapter()
    adapter.model = model
    return adapter


class GatewayStreamsTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="test")
        self.api = WebApi(self.ctx, _fake())
        self.gateway: GatewayStreams = self.api.gateway

    def tearDown(self):
        self.gateway.dispose()
        self.ctx.dispose()

    def _create(self, **extra):
        payload = {"cwd": os.getcwd()}
        if "session_id" in extra:
            payload["sessionId"] = extra.pop("session_id")
        payload.update(extra)
        response = self.api.dispatch("session.create", "r1", payload)
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]["sessionId"]


class TestFollow(GatewayStreamsTest):
    def _open(self, sid):
        return self.gateway.open_stream("session.follow", {
            "args": {"address": {"kind": "session", "sessionId": sid}}})

    def test_snapshot_then_event_frames(self):
        async def go():
            sid = self._create(session_id="session-a")
            gen = self._open(sid)
            snap = await gen.__anext__()
            self.assertEqual(snap["type"], "snapshot")
            self.assertEqual(snap["header"]["sessionId"], sid)
            self.assertIn("cursor", snap)
            self.assertIn("records", snap)
            self.assertIn("hasMore", snap)
            self.assertIs(snap["hasMore"], False)
            self.assertIn("projections", snap)
            response = self.api.dispatch("session.prompt", "rp", {
                "sessionId": sid, "mode": "queue", "requestId": "req-" + sid,
                "content": [{"type": "text", "text": "hello"}],
            })
            self.assertTrue(response["result"]["ok"])
            await self.api._agents[sid].when_idle_async()
            # 快照后逐帧取：应出现 user/message 与 turn/end（含 turn/start 等）
            seen = []
            for _ in range(60):
                frame = await gen.__anext__()
                seen.append(frame)
                if (frame["type"] == "event"
                        and frame["event"]["type"] == "turn/end"):
                    break
            return snap, seen, sid

        snap, seen, sid = _run(go())
        event_types = [f["event"]["type"] for f in seen if f["type"] == "event"]
        self.assertIn("user/message", event_types)
        self.assertIn("assistant/message", event_types)
        self.assertIn("turn/end", event_types)
        for f in seen:
            self.assertIsInstance(f["event"]["seq"], int)
            self.assertGreaterEqual(f["event"]["seq"], snap["cursor"])

    def test_snapshot_respects_max_messages(self):
        async def go():
            sid = self._create(session_id="session-m")
            loop = self.api._agents[sid]
            for _ in range(3):
                message = create_message("user", [text_block("x")], {"kind": "user"})
                loop.session.append("user/message", {"message": message, "time": 1},
                                    surfaceOp="append")
            gen = self._open(sid)
            snap = await gen.__anext__()
            return snap

        snap = _run(go())
        # 未设 maxMessages：全量
        self.assertEqual(len(snap["records"]), 3)

    def test_snapshot_max_messages_trims_tail(self):
        async def go():
            sid = self._create(session_id="session-t")
            loop = self.api._agents[sid]
            for _ in range(4):
                loop.session.append("user/message",
                                    {"message": create_message("user", [text_block("x")],
                                                               {"kind": "user"}), "time": 1},
                                    surfaceOp="append")
            gen = self.gateway.open_stream("session.follow", {
                "args": {"address": {"kind": "session", "sessionId": sid},
                         "maxMessages": 2}})
            snap = await gen.__anext__()
            return snap

        snap = _run(go())
        self.assertEqual(len(snap["records"]), 2)
        self.assertIs(snap["hasMore"], True)

    def test_session_not_found(self):
        async def go():
            gen = self.gateway.open_stream("session.follow", {
                "args": {"address": {"kind": "session", "sessionId": "no-such"}}})
            try:
                await gen.__anext__()
                self.fail("expected RemoteStreamError")
            except RemoteStreamError as error:
                return error.code
        self.assertEqual(_run(go()), "session-not-found")

    def test_arguments_invalid(self):
        async def go(payload):
            gen = self.gateway.open_stream("session.follow", payload)
            try:
                await gen.__anext__()
                return None
            except RemoteStreamError as error:
                return error.code
        for payload in ({"args": {}}, {"args": {"address": {"kind": "host"}}},
                        {"args": {"address": {"kind": "session"}}}):
            self.assertEqual(_run(go(payload)), "arguments-invalid")


class TestControl(GatewayStreamsTest):
    def test_baseline_then_live_queue(self):
        async def go():
            sid = self._create(session_id="session-c")
            loop = self.api._agents[sid]
            message = create_message("user", [text_block("hi")], {"kind": "user"})
            loop.inbox.append("next-turn", message)
            gen = self.gateway.open_stream("session.control", {"args": {}})
            baseline = await gen.__anext__()
            self.assertEqual(baseline["type"], "baseline")
            value = baseline["value"]
            self.assertIn("queues", value)
            self.assertIn("jobs", value)
            self.assertIn(sid, value["queues"])
            self.assertEqual(value["queues"][sid][0]["placement"], "queued")
            # 触发一次 inbox splice → 实时 queue 帧（gen 已 running 消费队列）
            loop.inbox.append("next-turn",
                              create_message("user", [text_block("again")], {"kind": "user"}))
            frame_task = asyncio.create_task(gen.__anext__())
            await asyncio.sleep(0)
            frame = await asyncio.wait_for(frame_task, 5)
            return frame, sid

        frame, sid = _run(go())
        self.assertEqual(frame["type"], "queue")
        self.assertEqual(frame["sessionId"], sid)


class TestDispatchErrorHandling(GatewayStreamsTest):
    def test_unknown_endpoint_raises(self):
        with self.assertRaises(RemoteStreamError) as cm:
            self.gateway.open_stream("session.nope", {"args": {}})
        self.assertEqual(cm.exception.code, "internal")

    def test_events_endpoint_dispatches(self):
        # $events 端点到 events registry：open 即 ready 帧
        async def go():
            gen = self.gateway.open_stream("$events", {"args": {}})
            ready = await gen.__anext__()
            await gen.aclose()
            return ready
        ready = _run(go())
        self.assertEqual(ready["type"], "ready")
        self.assertIn("clientId", ready)


if __name__ == "__main__":
    unittest.main()
