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
            # snapshot.header = 平铺 SessionWireHeader（上游 history.ts wireHeader）
            self.assertEqual(snap["header"]["id"], sid)
            self.assertEqual(snap["header"]["version"], 2)
            self.assertIs(snap["header"]["isSeeded"], False)
            self.assertIn("createdAt", snap["header"])
            self.assertIn("cursor", snap)
            self.assertIn("records", snap)
            self.assertIn("hasMore", snap)
            self.assertIs(snap["hasMore"], False)
            # records 为 {type:'event', event} 包装（上游 entryFor）；空日志
            # cursor = -1、projections 空基线 {asOfSeq, values}
            for record in snap["records"]:
                self.assertEqual(record["type"], "event")
            self.assertEqual(snap["cursor"],
                             snap["records"][-1]["event"]["seq"] if snap["records"] else -1)
            self.assertEqual(snap["projections"],
                             {"asOfSeq": snap["cursor"], "values": {}})
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
        self.assertEqual(_run(go()), "session/not-found")

    def test_arguments_invalid(self):
        async def go(payload):
            try:
                gen = self.gateway.open_stream("session.follow", payload)
                await gen.__anext__()
                return None
            except RemoteStreamError as error:
                return error.code
        for payload in ({"args": {}}, {"args": {"address": {"kind": "host"}}},
                        {"args": {"address": {"kind": "session"}}}):
            self.assertEqual(_run(go(payload)), "gateway/arguments-invalid")

    def test_field_type_input_invalid(self):
        async def go(payload):
            try:
                gen = self.gateway.open_stream("session.follow", payload)
                await gen.__anext__()
                return None
            except RemoteStreamError as error:
                return (error.code, error.message)
        code, message = _run(go({"args": {"address": {"kind": "session",
                                                      "sessionId": "x"},
                                          "maxMessages": "5"}}))
        self.assertEqual(code, "gateway/input-invalid")
        self.assertIn('wire field "maxMessages" failed boundary validation', message)

    def test_control_extra_key_rejected(self):
        async def go():
            try:
                gen = self.gateway.open_stream("session.control",
                                               {"args": {"bogus": 1}})
                await gen.__anext__()
                return None
            except RemoteStreamError as error:
                return error.code
        self.assertEqual(_run(go()), "gateway/arguments-invalid")


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
            # baseline 对齐上游 control.ts：全部 live 会话每会话一条（空也放）
            # + 每会话 projections 空基线块 {asOfSeq, values}
            self.assertIn(sid, value["queues"])
            self.assertIn(sid, value["jobs"])
            self.assertIn(sid, value["projections"])
            self.assertEqual(value["queues"][sid][0]["placement"], "queued")
            self.assertEqual(value["projections"][sid]["values"], {})
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
        # 未知 endpoint：上游 gateway 折算 invocation-unavailable（index.ts:660）
        with self.assertRaises(RemoteStreamError) as cm:
            self.gateway.open_stream("session.nope", {"args": {}})
        self.assertEqual(cm.exception.code, "gateway/invocation-unavailable")

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


class TestReconnectResilience(GatewayStreamsTest):
    """重连健壮性（wire 无 since；重开流重投全量 + 客户端去重 = gap-free）。

    对齐上游 README「reconnection reopens the `$events` stream」/「return a
    complete opening snapshot followed by deltas」：follow/control 重开即重投
    snapshot/baseline，`$events` 新代次只接 ready + 未来帧（单向不重放、挂起
    waterfall 保留 eventId 随代次重投）。
    """

    def _append(self, loop, i: int) -> None:
        loop.session.append("user/message",
                            {"message": create_message("user", [text_block(f"x{i}")],
                                                       {"kind": "user"}), "time": i + 1},
                            surfaceOp="append")

    def _open(self, sid):
        return self.gateway.open_stream("session.follow", {
            "args": {"address": {"kind": "session", "sessionId": sid}}})

    def test_follow_reconnect_snapshot_gap_free(self):
        async def go():
            sid = self._create(session_id="session-r")
            loop = self.api._agents[sid]
            gen1 = self._open(sid)
            snap1 = await gen1.__anext__()
            c0 = snap1["cursor"]
            self._append(loop, 0)
            self._append(loop, 1)
            evs1 = []
            for _ in range(2):
                evs1.append(await gen1.__anext__())
            await gen1.aclose()
            self._append(loop, 2)
            self._append(loop, 3)
            gen2 = self._open(sid)
            snap2 = await gen2.__anext__()
            await gen2.aclose()
            return c0, evs1, snap2

        c0, evs1, snap2 = _run(go())
        first_gen = {f["event"]["seq"] for f in evs1}
        second_gen = {r["event"]["seq"] for r in snap2["records"]}
        # 事件 seq 为 0 基（seq == 追加前日志长度）；snapshot cursor = 最后已提交
        # 事件 seq（inclusive，上游 history.ts sourceLog.at(-1)?.seq ?? -1，空日志 -1）
        count = (c0 + 1) + 4
        self.assertEqual(snap2["cursor"], count - 1)
        merged = first_gen | second_gen
        self.assertEqual(merged, set(range(0, count)))
        self.assertEqual(len(merged), count)
        self.assertTrue(second_gen.issuperset(first_gen))

    def test_control_reconnect_refreshes_baseline(self):
        async def go():
            sid = self._create(session_id="session-rc")
            gen1 = self.gateway.open_stream("session.control", {"args": {}})
            baseline1 = await gen1.__anext__()
            await gen1.aclose()
            gen2 = self.gateway.open_stream("session.control", {"args": {}})
            baseline2 = await gen2.__anext__()
            await gen2.aclose()
            return baseline1, baseline2, sid

        b1, b2, sid = _run(go())
        for baseline in (b1, b2):
            self.assertEqual(baseline["type"], "baseline")
            self.assertIn(sid, baseline["value"]["queues"])

    def test_events_reconnect_no_emit_replay(self):
        async def go():
            gen1 = self.gateway.open_stream("$events", {"args": {}})
            ready1 = await gen1.__anext__()
            self.gateway.events.broadcast("api-session/added",
                                          {"sessionId": "s1", "running": False})
            f1 = await gen1.__anext__()
            self.assertEqual(f1["type"], "emit")
            await gen1.aclose()
            gen2 = self.gateway.open_stream("$events", {"args": {}})
            ready2 = await gen2.__anext__()
            self.gateway.events.broadcast("api-session/status", "s1", True)
            f2 = await gen2.__anext__()
            await gen2.aclose()
            return ready1, f1, ready2, f2

        ready1, f1, ready2, f2 = _run(go())
        self.assertNotEqual(ready1["clientId"], ready2["clientId"])
        self.assertEqual(f2["event"], "api-session/status")
        self.assertEqual(f2["args"], ["s1", True])

    def test_events_reconnect_delivers_pending_waterfall(self):
        async def go():
            sid = self._create(session_id="session-pw")
            pending = asyncio.create_task(
                self.gateway.events.invoke("approval/request", sid,
                                           {"prompt": "proceed?"}))
            await asyncio.sleep(0)
            await asyncio.sleep(0)
            gen = self.gateway.open_stream("$events", {"args": {}})
            ready = await gen.__anext__()
            wf = await gen.__anext__()
            self.assertEqual(wf["type"], "waterfall")
            self.assertEqual(wf["event"], "approval/request")
            self.assertEqual(wf["agentId"], sid)
            self.gateway.events.receive_result(
                {"clientId": ready["clientId"], "eventId": wf["eventId"],
                 "outcome": {"kind": "result", "value": "ok"}})
            settled = await asyncio.wait_for(pending, 5)
            await gen.aclose()
            return settled

        self.assertEqual(_run(go()), ("result", "ok"))


if __name__ == "__main__":
    unittest.main()
