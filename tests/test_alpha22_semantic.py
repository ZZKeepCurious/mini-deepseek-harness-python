"""alpha.2 语义补齐：session observation 冷读等集成测。"""
import os
import tempfile
import unittest
from pathlib import Path

from miniharness.core.scope import Context
from miniharness.core.session.persistence import JsonlPersistence
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import WebApi


class ColdSessionEventsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = os.path.join(self._tmp.name, "work")
        os.makedirs(self.work)

        source_ctx = Context(name="cold-events-source")
        self.addCleanup(source_ctx.dispose)
        source_api = WebApi(source_ctx, FakeLlmAdapter())
        self.source_id = self._value(source_api.dispatch(
            "session.create", "r0", {"cwd": self.work}))["sessionId"]
        self._value(source_api.dispatch(
            "session/prompt", "r1",
            {"requestId": "req-1", "sessionId": self.source_id,
             "content": [{"type": "text", "text": "hi"}], "mode": "queue"}))
        source = source_api.store.get(self.source_id)
        self.persistence = JsonlPersistence(Path(self._tmp.name) / "sessions",
                                            compression="none")
        self.persistence.declare(self.source_id, meta={"cwd": self.work},
                                 created_at=source.created_at, cwd=self.work)
        for event in source.events:
            self.persistence.append(self.source_id, dict(event), cwd=self.work)
        self.persistence.flush()

        self.ctx = Context(name="cold-events-target")
        self.addCleanup(self.ctx.dispose)
        self.ctx.provide("sessionPersistence", self.persistence)
        self.api = WebApi(self.ctx, FakeLlmAdapter())

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def test_events_through_seq_minus_one_on_prepared_log(self):
        value = self._value(self.api.dispatch("session/events", "e1", {
            "address": {"kind": "session", "sessionId": self.source_id},
            "throughSeq": -1,
        }))
        self.assertEqual(value["cursor"], -1)
        self.assertEqual(value["records"], [])
        self.assertFalse(value["hasMore"])

    def test_events_tail_omitted_through_seq(self):
        last = self.persistence.inspect(self.source_id)["events"][-1]["seq"]
        value = self._value(self.api.dispatch("session/events", "e2", {
            "address": {"kind": "session", "sessionId": self.source_id},
            "maxMessages": 50,
        }))
        self.assertEqual(value["cursor"], last)
        self.assertGreater(len(value["records"]), 0)


if __name__ == "__main__":
    unittest.main()
