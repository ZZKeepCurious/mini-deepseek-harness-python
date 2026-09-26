"""job-controller Remote 面（job/list 流、job/follow 流、job/kill unary）验收。

对照上游 `packages/api/job-controller/src/{index,rows,observe}.ts`：`job`
namespace 的 roster 整集替换流、retained output 观察流与 kill unary。
"""
import asyncio
import threading
import time
import unittest

from miniharness.core.agents import install_agents
from miniharness.core.scope import Context
from miniharness.jobs import JobDoneBox, install_jobs
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import WebApi
from miniharness.web.streams import RemoteStreamError


def _make_owner(agents, session_id):
    owner = type("Owner", (), {})()
    owner.id = session_id
    owner.session = type("SessionRef", (), {"session_id": session_id})()
    owner.ctx = Context(name=f"own-{session_id}")
    owner._carrier = owner.ctx
    owner.status = "idle"
    owner.delivered = []
    owner.wakes = 0
    owner.followup = lambda content, source=None: owner.delivered.append(content)
    owner.inject = lambda content: owner.delivered.append(content)
    agents.register(owner)
    return owner


class JobControllerTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="job-controller")
        self.addCleanup(self.ctx.dispose)
        install_agents(self.ctx)
        self.registry = install_jobs(self.ctx)
        self.owner = _make_owner(self.ctx.get("agents"), "o1")
        self.api = WebApi(self.ctx, FakeLlmAdapter())

    def _start(self, kind="bash", label="s", owner="o1", output=None):
        box = JobDoneBox()
        holder: dict = {"cancel": lambda r=None: box.settle({"status": "killed"})}

        def run(job):
            holder["handle"] = job
            return {"done": box, "cancel": lambda r=None: holder["cancel"](r)}

        spec: dict = {"kind": kind, "label": label, "run": run}
        spec["owner"] = owner
        if output is not None:
            spec["output"] = output
        return self.registry.start(spec), box, holder

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def _error(self, response):
        self.assertFalse(response["result"]["ok"])
        return response["result"]["error"]

    # ---------- job/kill unary ----------

    def test_kill_requests_job(self):
        tid, box, holder = self._start()
        value = self._value(self.api.dispatch(
            "job/kill", "k1", {"sessionId": "o1", "jobId": tid}))
        self.assertEqual(value, {"outcome": "requested"})

    def test_kill_already_finished(self):
        tid, box, holder = self._start()
        box.settle({"status": "completed"})
        self._wait_terminal(tid)
        value = self._value(self.api.dispatch(
            "job/kill", "k2", {"sessionId": "o1", "jobId": tid}))
        self.assertEqual(value, {"outcome": "already-finished"})

    def _wait_terminal(self, tid, timeout=5.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            view = self.registry.get(tid, "o1")
            if view["status"] not in ("running", "stopping"):
                return view
            time.sleep(0.01)
        raise AssertionError("job did not settle in time")

    def test_kill_unknown_job_is_not_found(self):
        error = self._error(self.api.dispatch(
            "job/kill", "k3", {"sessionId": "o1", "jobId": "bash-999"}))
        self.assertEqual(error["code"], "job/not-found")
        self.assertEqual(error["details"]["jobId"], "bash-999")
        self.assertEqual(error["details"]["sessionId"], "o1")

    def test_kill_foreign_job_is_not_found(self):
        tid, box, holder = self._start(owner="o1")
        error = self._error(self.api.dispatch(
            "job/kill", "k4", {"sessionId": "other", "jobId": tid}))
        self.assertEqual(error["code"], "job/not-found")

    def test_kill_without_jobs_mounted(self):
        bare = Context(name="bare")
        self.addCleanup(bare.dispose)
        api = WebApi(bare, FakeLlmAdapter())
        error = self._error(api.dispatch(
            "job/kill", "k5", {"sessionId": "o1", "jobId": "bash-1"}))
        self.assertEqual(error["code"], "gateway/invocation-unavailable")

    def test_kill_bad_payload(self):
        error = self._error(self.api.dispatch("job/kill", "k6", {}))
        self.assertEqual(error["code"], "gateway/arguments-invalid")

    # ---------- job/list stream ----------

    def test_list_rows_full_set(self):
        tid1, _, _ = self._start(label="a")
        tid2, _, _ = self._start(label="b")
        rows = asyncio.run(self._first_rows())
        ids = [job["id"] for job in rows["jobs"]]
        self.assertIn(tid1, ids)
        self.assertIn(tid2, ids)
        self.assertTrue(all(job["owner"] == "o1" for job in rows["jobs"]))

    async def _first_rows(self):
        stream = self.api.gateway.open_stream("job/list", {"args": {"sessionId": "o1"}})
        try:
            return await stream.__anext__()
        finally:
            await stream.aclose()

    def test_list_unmounted_namespace(self):
        async def go():
            bare = Context(name="bare2")
            try:
                api = WebApi(bare, FakeLlmAdapter())
                stream = api.gateway.open_stream(
                    "job/list", {"args": {"sessionId": "o1"}})
                with self.assertRaises(RemoteStreamError) as cm:
                    await stream.__anext__()
                return cm.exception.code
            finally:
                bare.dispose()
        code = asyncio.run(go())
        self.assertEqual(code, "gateway/invocation-unavailable")

    def test_list_refreshes_on_lifecycle_change(self):
        tid, box, holder = self._start(label="first")

        async def run():
            stream = self.api.gateway.open_stream(
                "job/list", {"args": {"sessionId": "o1"}})
            first = await stream.__anext__()
            self.assertEqual([j["id"] for j in first["jobs"]], [tid])
            tid2, box2, holder2 = self._start(label="second")
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                try:
                    frame = await asyncio.wait_for(stream.__anext__(), 0.5)
                except (asyncio.TimeoutError, StopAsyncIteration):
                    continue
                ids = [j["id"] for j in frame["jobs"]]
                if tid2 in ids:
                    await stream.aclose()
                    return ids, tid2
            await stream.aclose()
            self.fail("job/list did not refresh with the new job")

        ids, tid2 = asyncio.run(run())
        self.assertEqual(sorted(ids), sorted([tid, tid2]))

    # ---------- job/follow stream ----------

    def test_follow_anchor_and_terminal_status(self):
        tid, box, holder = self._start(label="follow")
        handle = holder["handle"]
        handle.append("line one\n")
        box.settle({"status": "completed"})

        async def run():
            stream = self.api.gateway.open_stream(
                "job/follow", {"args": {"sessionId": "o1", "jobId": tid}})
            opened = await stream.__anext__()
            self.assertEqual(opened["type"], "opened")
            self.assertEqual(opened["job"]["id"], tid)
            self.assertEqual(opened["from"], opened["job"]["output"]["earliest"])
            frames = []
            while True:
                try:
                    frame = await asyncio.wait_for(stream.__anext__(), 5.0)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    await stream.aclose()
                    self.fail("job/follow did not reach terminal status")
                frames.append(frame)
                if frame["type"] == "status":
                    break
            await stream.aclose()
            return frames

        frames = asyncio.run(run())
        self.assertTrue(any(f["type"] == "output" for f in frames))
        final = frames[-1]
        self.assertEqual(final["type"], "status")
        self.assertEqual(final["job"]["status"], "completed")

    def test_follow_unknown_job_is_not_found(self):
        async def go():
            stream = self.api.gateway.open_stream(
                "job/follow", {"args": {"sessionId": "o1", "jobId": "bash-999"}})
            with self.assertRaises(RemoteStreamError) as cm:
                await stream.__anext__()
            return cm.exception.code
        code = asyncio.run(go())
        self.assertEqual(code, "job/not-found")

    def test_follow_bad_offset_rejected(self):
        tid, box, holder = self._start()
        async def go():
            stream = self.api.gateway.open_stream(
                "job/follow", {"args": {"sessionId": "o1", "jobId": tid, "from": -1}})
            with self.assertRaises(RemoteStreamError) as cm:
                await stream.__anext__()
            return cm.exception.code
        code = asyncio.run(go())
        self.assertEqual(code, "gateway/arguments-invalid")


if __name__ == "__main__":
    unittest.main()