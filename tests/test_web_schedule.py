"""dsh-v0.2.0-rc.2：schedule Remote 面（`web/api.py`）验收。

上游 `packages/schedule/schedule` 的 `schedule` 命名空间：list/catalog/history/
delete/update；读取与删除不激活会话；`schedule/changed` 经 `$events` 转发。
"""
import asyncio
import os
import tempfile
import unittest

from miniharness.core.agents import install_agents
from miniharness.core.scope import Context
from miniharness.core.session_store import install_sessions
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.schedule import install_schedule
from miniharness.storage import install_storage
from miniharness.web.api import WebApi


class ScheduleWireTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="sched-wire")
        self.addCleanup(self.ctx.dispose)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        install_agents(self.ctx)
        install_sessions(self.ctx)
        install_storage(self.ctx, os.path.join(self.tmp.name, "storage"))
        self.service = install_schedule(self.ctx)
        self.api = WebApi(self.ctx, FakeLlmAdapter())
        self.session_id = self._value(self.api.dispatch(
            "session/create", "r0", {"cwd": os.path.join(self.tmp.name, "work")}))["sessionId"]

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def _error(self, response):
        self.assertFalse(response["result"]["ok"])
        return response["result"]["error"]

    def _create_task(self):
        record = asyncio.run(self.service.create(self.session_id, {
            "prompt": "remind me", "title": "remind me", "after_seconds": 60}))
        return record["id"]

    def test_list_empty(self):
        self.assertEqual(self._value(self.api.dispatch(
            "schedule/list", "s1", {"sessionId": self.session_id})), [])

    def test_catalog_and_list_after_create(self):
        task_id = self._create_task()
        listed = self._value(self.api.dispatch(
            "schedule/list", "s2", {"sessionId": self.session_id}))
        self.assertEqual([row["id"] for row in listed], [task_id])
        catalog = self._value(self.api.dispatch("schedule/catalog", "s3", {}))
        entry = next(row for row in catalog if row["id"] == task_id)
        self.assertEqual(entry["sessionId"], self.session_id)
        self.assertEqual(entry["status"], "active")

    def test_delete_unknown_is_not_found_result(self):
        result = self._value(self.api.dispatch(
            "schedule/delete", "s4", {"sessionId": self.session_id, "id": "ghost"}))
        self.assertEqual(result, {"id": "ghost", "deleted": False,
                                  "code": "schedule_not_found"})

    def test_delete_removes_task(self):
        task_id = self._create_task()
        result = self._value(self.api.dispatch(
            "schedule/delete", "s5", {"sessionId": self.session_id, "id": task_id}))
        self.assertEqual(result, {"id": task_id, "deleted": True})
        self.assertEqual(self._value(self.api.dispatch(
            "schedule/list", "s6", {"sessionId": self.session_id})), [])

    def test_history_unknown_task(self):
        result = self._value(self.api.dispatch(
            "schedule/history", "s7",
            {"sessionId": self.session_id, "id": "ghost", "limit": 10}))
        self.assertEqual(result, {"id": "ghost", "code": "schedule_not_found"})

    def test_schedule_changed_forwarded_on_events(self):
        async def go():
            gen = self.api.gateway.open_stream("$events", {"args": {}})
            await gen.__anext__()  # ready
            await self.service.create(self.session_id, {
                "prompt": "remind me", "title": "remind me", "after_seconds": 60})
            frames = []
            for _ in range(200):
                frame = await gen.__anext__()
                frames.append(frame)
                if frame.get("event") == "schedule/changed":
                    break
            await gen.aclose()
            return frames
        frames = asyncio.run(go())
        self.assertTrue(any(frame.get("event") == "schedule/changed" for frame in frames))

    def test_schedule_not_mounted(self):
        ctx2 = Context(name="bare-sched")
        try:
            api = WebApi(ctx2, FakeLlmAdapter())
            error = self._error(api.dispatch("schedule/list", "s8", {"sessionId": "s"}))
            self.assertEqual(error["code"], "gateway/invocation-unavailable")
        finally:
            ctx2.dispose()


if __name__ == "__main__":
    unittest.main()
