"""web 事件流测试：StreamHub mux/host 两路下游帧（对齐 packages/host/apiproxy/src/api/events.ts）。"""
import asyncio
import os
import unittest

from miniharness.core.scope import Context
from miniharness.core.session import create_message, text_block
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import WebApi
from miniharness.web.streams import StreamHub


def _run(coro):
    return asyncio.run(coro)


def _fake(model: str = "fake-model") -> FakeLlmAdapter:
    adapter = FakeLlmAdapter()
    adapter.model = model
    return adapter


class _FakeAgent:
    def __init__(self, id_):
        self.id = id_


class _FakeJobs:
    """可注入 jobs 服务：on_jobs_changed + list 契约（hub 只依赖这两样）。"""

    def __init__(self, items=None):
        self.items = items or [{
            "id": "job-1", "kind": "test", "label": "l", "status": "running", "startedAt": 0,
        }]
        self._cb = None

    def on_jobs_changed(self, listener):
        self._cb = listener
        return lambda: setattr(self, "_cb", None)

    def list(self, caller=None):
        return self.items


class StreamHubTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="test")
        self.api = WebApi(self.ctx, _fake())
        self.hub = StreamHub(self.ctx, self.api)

    def tearDown(self):
        self.hub.dispose()
        self.ctx.dispose()

    def _create(self, **extra):
        payload = {"cwd": os.getcwd()}
        if "session_id" in extra:
            payload["sessionId"] = extra.pop("session_id")
        if "agent_preset" in extra:
            payload["agentPreset"] = extra.pop("agent_preset")
        payload.update(extra)
        response = self.api.dispatch("session.create", "r1", payload)
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]["sessionId"]


class TestMux(StreamHubTest):
    async def _take(self, agen, n):
        return [await agen.__anext__() for _ in range(n)]

    def test_baseline_subscribed(self):
        async def go():
            sid_a = self._create(session_id="session-a")
            sid_b = self._create(session_id="session-b")
            mux = self.hub.mux()
            first = await mux.__anext__()
            second = await mux.__anext__()
            return first, second, sid_a, sid_b

        first, second, sid_a, sid_b = _run(go())
        # 按 sessionId 排序：session-a 在前
        self.assertEqual(first["type"], "session/subscribed")
        self.assertEqual(first["sessionId"], sid_a)
        self.assertEqual(first["lastSeq"], 0)
        self.assertEqual(second["type"], "session/subscribed")
        self.assertEqual(second["sessionId"], sid_b)
        self.assertEqual(second["lastSeq"], self.api.store.get(sid_b).seq)

    def test_live_session_events(self):
        async def go():
            sid = self._create()
            mux = self.hub.mux()
            baseline = await mux.__anext__()
            self.assertEqual(baseline["type"], "session/subscribed")
            response = self.api.dispatch("session.prompt", "rp", {
                "sessionId": sid, "mode": "queue",
                "content": [{"type": "text", "text": "hello"}],
            })
            self.assertTrue(response["result"]["ok"])
            await self.api._agents[sid].when_idle_async()
            frames = []
            for _ in range(60):
                frame = await mux.__anext__()
                frames.append(frame)
                if frame.get("type") == "session/event" and frame["event"]["type"] == "turn/end":
                    break
            return frames

        frames = _run(go())
        types = [f["event"]["type"] for f in frames if f["type"] == "session/event"]
        self.assertIn("user/message", types)
        self.assertIn("assistant/message", types)
        self.assertIn("turn/end", types)
        for frame in frames:
            if frame["type"] == "session/event":
                self.assertEqual(frame["sessionId"], self.api.store.list()[0].session_id)
                self.assertIsInstance(frame["event"]["seq"], int)

    def test_queue_baseline(self):
        async def go():
            sid = self._create()
            loop = self.api._agents[sid]
            message = create_message("user", [text_block("hi")], {"kind": "user"})
            loop.inbox.append("next-turn", message)
            mux = self.hub.mux()
            baseline = await mux.__anext__()
            self.assertEqual(baseline["type"], "session/subscribed")
            queue_frame = await mux.__anext__()
            return queue_frame, message

        queue_frame, message = _run(go())
        self.assertEqual(queue_frame["type"], "session/queue")
        self.assertEqual(len(queue_frame["items"]), 1)
        self.assertEqual(queue_frame["items"][0]["placement"], "queued")
        self.assertEqual(queue_frame["items"][0]["message"]["id"], message["id"])

    def test_queue_live_after_splice(self):
        async def go():
            sid = self._create()
            mux = self.hub.mux()
            await mux.__anext__()
            loop = self.api._agents[sid]
            message = create_message("user", [text_block("hi")], {"kind": "user"})
            loop.inbox.append("next-turn", message)
            splice = await mux.__anext__()
            queue_frame = await mux.__anext__()
            return splice, queue_frame, message

        splice, queue_frame, message = _run(go())
        self.assertEqual(splice["type"], "session/event")
        self.assertEqual(splice["event"]["type"], "agent/inbox/spliced")
        self.assertEqual(queue_frame["type"], "session/queue")
        self.assertEqual(queue_frame["items"][0]["message"]["id"], message["id"])

    def test_jobs_baseline_and_live(self):
        jobs = _FakeJobs()
        self.ctx.provide("jobs", jobs)

        async def go():
            sid = self._create()
            mux = self.hub.mux()
            baseline = await mux.__anext__()
            self.assertEqual(baseline["type"], "session/subscribed")
            jobs_frame = await mux.__anext__()
            self.assertEqual(jobs_frame["type"], "session/jobs")
            # 实时变更：回调携带 agent owner
            jobs._cb(self.api._agents[sid])
            live = await mux.__anext__()
            return jobs_frame, live

        jobs_frame, live = _run(go())
        for frame in (jobs_frame, live):
            self.assertEqual(frame["type"], "session/jobs")
            self.assertEqual(frame["jobs"], [{
                "id": "job-1", "kind": "test", "label": "l", "status": "running", "startedAt": 0,
            }])


class TestHost(StreamHubTest):
    async def _first(self, agen):
        """启动生成器（触发懒 _attach）并取下一帧。"""
        task = asyncio.create_task(agen.__anext__())
        await asyncio.sleep(0)
        return task

    def test_session_added(self):
        async def go():
            mux = self.hub.host()
            first = await self._first(mux)
            sid = self._create(session_id="session-h", agent_preset="standard")
            frame = await asyncio.wait_for(first, 5)
            return frame, sid

        frame, sid = _run(go())
        self.assertEqual(frame["type"], "host/session-added")
        self.assertEqual(frame["sessionId"], sid)
        self.assertIs(frame["blank"], True)
        self.assertEqual(frame["cwd"], os.getcwd())
        self.assertEqual(frame["agentPreset"], "standard")

    def test_session_removed(self):
        async def go():
            mux = self.hub.host()
            added = await self._first(mux)
            sid = self._create(session_id="session-h")
            await asyncio.wait_for(added, 5)
            removed = asyncio.create_task(mux.__anext__())
            self.hub._on_session_disposed({"session": self.api.store.get(sid)})
            frame = await asyncio.wait_for(removed, 5)
            return frame

        frame = _run(go())
        self.assertEqual(frame["type"], "host/session-removed")
        self.assertEqual(frame["sessionId"], "session-h")

    def test_status_flips(self):
        async def go():
            frames = []
            mux = self.hub.host()

            async def collect():
                for _ in range(80):
                    frame = await mux.__anext__()
                    frames.append(frame)
                    if frame["type"] == "host/session-status" and frame["running"] is False:
                        return True
                return False

            collector = asyncio.create_task(collect())
            await asyncio.sleep(0)
            sid = self._create(session_id="session-s")
            response = self.api.dispatch("session.prompt", "rp", {
                "sessionId": sid, "mode": "queue",
                "content": [{"type": "text", "text": "hi"}],
            })
            self.assertTrue(response["result"]["ok"])
            await self.api._agents[sid].when_idle_async()
            done = await asyncio.wait_for(collector, 10)
            return frames, done

        frames, done = _run(go())
        self.assertTrue(done)
        added = [f for f in frames if f["type"] == "host/session-added"]
        self.assertEqual(len(added), 1)
        self.assertEqual(added[0]["sessionId"], "session-s")
        statuses = [f for f in frames if f["type"] == "host/session-status"]
        self.assertTrue(any(f["running"] is True for f in statuses))
        self.assertTrue(any(f["running"] is False for f in statuses))

    def test_agent_error_mapping(self):
        async def go():
            mux = self.hub.host()
            added = await self._first(mux)
            sid = self._create(session_id="session-e")
            await asyncio.wait_for(added, 5)
            self.hub._on_agent_error({
                "agent": _FakeAgent(sid), "turn": 1, "step": 1,
                "error": {"code": "SERVER", "message": "boom"},
            })
            frame = await mux.__anext__()
            return frame

        frame = _run(go())
        self.assertEqual(frame["type"], "host/agent-error")
        self.assertEqual(frame["sessionId"], "session-e")
        self.assertEqual(frame["message"], "boom")


class TestHubLifecycle(StreamHubTest):
    def test_dispose_then_reopen(self):
        self.hub.dispose()
        sid = self._create(session_id="session-l")

        async def go():
            mux = self.hub.mux()
            frame = await mux.__anext__()
            return frame

        frame = _run(go())
        self.assertEqual(frame["type"], "session/subscribed")
        self.assertEqual(frame["sessionId"], sid)


if __name__ == "__main__":
    unittest.main()