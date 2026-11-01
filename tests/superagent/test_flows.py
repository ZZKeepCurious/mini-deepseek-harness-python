import os
import tempfile
import unittest

from superagent.app.assessor_loop import deliver_and_decide
from superagent.app.runtime import Runtime
from superagent.core.ids import key
from superagent.executor.scripted import ScriptedExecutor
from superagent.infra.policy import SimplePolicy
from superagent.sources.fake_source import FAKE_SOURCE_KIND, fake_trigger

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import trigger


class FlowTests(unittest.TestCase):
    def test_flow_trigger_receive(self):
        h = Harness()
        self.assertEqual(h.engine.receive(trigger("1")), "stored")
        h.pump()
        self.assertEqual(len(h.assessor.requests), 1)

    def test_flow_eval_decide_accept(self):
        h = Harness()
        decision = accepted(h)
        self.assertIn("ticketId", decision)
        self.assertEqual(len(h.outbox), 1)

    def test_flow_assignment_lifecycle(self):
        h = Harness()
        decision = accepted(h)
        ticket_id = decision["ticketId"]
        h.engine.report(ticket_id, {"state": "progress", "summary": "working", "at": "2026-09-23T00:01:00Z"})
        h.engine.report(ticket_id, {"state": "ready", "summary": "done", "at": "2026-09-23T00:02:00Z"})
        ticket = h.repo.tickets.get(ticket_id)
        self.assertIn("ended", ticket)

    def test_flow_outbox_reply(self):
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.report(ticket_id, {"state": "ready", "summary": "done", "at": "2026-09-23T00:02:00Z"})
        reply = h.engine.reply(
            {"purpose": "result", "ticketId": ticket_id, "text": "done", "channel": trigger("1")["channel"]}
        )
        self.assertIn("id", reply)

    def test_flow_evaluate_due(self):
        h = Harness()
        h.engine.receive(trigger("1"))
        h.pump()
        h.engine.candidate_delivered(h.requests[0]["evalId"])
        h.tick(61)
        h.pump()
        self.assertGreaterEqual(len(h.assessor.released), 1)

    def test_flow_policy_changed(self):
        h = Harness()
        h.engine.receive(trigger("1"))
        h.pump()
        h.set_version("v2")
        h.engine.policy_changed("v1", "v2")
        h.pump()
        self.assertGreaterEqual(len(h.assessor.requests), 2)

    def test_flow_thread_close_github(self):
        h = Harness()
        decision = accepted(h)
        ticket_id = decision["ticketId"]
        h.engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-1"}, True)
        h.engine.thread_closed({"source": "gh", "id": "pr-1"}, "merged", "2026-09-23T00:03:00Z")
        ticket = h.repo.tickets.get(ticket_id)
        self.assertIn("ended", ticket)

    def test_runtime_scripted_executor_with_sqlite(self):
        path = os.path.join(tempfile.mkdtemp(), "flow.sqlite")
        rt = Runtime(db_path=path, policy=SimplePolicy())
        rt.registry.register(FAKE_SOURCE_KIND)
        rt.engine.options.executor = ScriptedExecutor(
            rt.engine,
            [{"kind": "report", "report": {"state": "ready", "summary": "ok", "at": "2026-09-23T00:02:00Z"}}],
        )
        rt.passes.receive(fake_trigger("f1"))
        rt.passes.pump()
        deliver_and_decide(
            rt.engine,
            rt.assessor.requests,
            "f1",
            {"kind": "accept", "title": "T", "scope": "s", "reason": "r", "acknowledgement": "ok"},
        )
        rt.effects.idle("launch")
        rt.effects.idle("reply")
        self.assertEqual(len(rt.sent_messages), 1)
        self.assertEqual(rt.sent_messages[0]["id"], key("ack", "f1"))
        rt.store.close()
