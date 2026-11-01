import unittest

from tests.superagent.support.harness import Harness, accepted, ended
from tests.superagent.support.triggers import GUEST, trigger


class ResumeAndStopTests(unittest.TestCase):
    def test_resumes_ended_ticket_on_person_message_same_mandate(self):
        h = Harness()
        ticket_id = ended(h, "executor")
        decisions = len(h.repo.decisions.byThread("chat:t1"))
        h.engine.receive(trigger("2", actor=GUEST))
        self.assertNotIn("ended", h.repo.tickets.get(ticket_id))
        h.pump()
        ticket = h.repo.tickets.get(ticket_id)
        self.assertEqual(len(ticket["assignments"]), 2)
        self.assertEqual(ticket["mandate"]["policyVersion"], "v1")
        self.assertEqual([i["kind"] for i in ticket["inputs"]], ["trigger", "resumed"])
        self.assertEqual(h.executor.start_count, 2)
        non_ignore = [d for d in h.repo.decisions.byThread("chat:t1") if d["kind"] != "ignore"]
        self.assertEqual(len(non_ignore), decisions)

    def test_resumes_with_waiting_input_on_next_pass(self):
        h = Harness()
        ticket_id = ended(h, "executor")
        h.engine.receive(trigger("2", actor=GUEST, threads=[{"source": "chat", "id": "elsewhere"}]))
        stored = h.repo.tickets.get(ticket_id)
        inp = {"id": "waiting", "kind": "trigger", "triggerId": "2", "at": stored["updatedAt"], "human": True}
        h.repo.tickets.save({**stored, "inputs": [*stored["inputs"], inp], "undelivered": ["waiting"]})
        h.pump()
        ticket = h.repo.tickets.get(ticket_id)
        kinds = [i.get("kind") for i in ticket["inputs"]]
        self.assertIn("resumed", kinds)
        self.assertNotIn("ended", ticket)

    def test_resumes_when_person_attaches_message(self):
        h = Harness()
        ticket_id = ended(h, "executor")
        h.engine.receive(trigger("2", actor=GUEST, threads=[{"source": "chat", "id": "elsewhere"}]))
        subject = h.engine.attach(ticket_id, "2")
        self.assertEqual(len(subject["assignments"]), 2)
        self.assertEqual(subject["assignments"][-1]["state"], "pending")
        self.assertNotIn("ended", subject)

    def test_person_stop_keeps_ticket_ended(self):
        h = Harness()
        ticket_id = ended(h, "human")
        self.assertEqual(h.executor.stop_count, 1)
        h.engine.receive(trigger("2"))
        h.pump()
        ticket = h.repo.tickets.get(ticket_id)
        self.assertEqual(ticket["ended"]["by"], "human")
        self.assertEqual(h.executor.start_count, 1)

    def test_revoked_mandate_blocks_resume(self):
        h = Harness()
        ticket_id = ended(h, "executor")
        h.set_version("revoked")
        h.engine.receive(trigger("2"))
        h.pump()
        self.assertIn("ended", h.repo.tickets.get(ticket_id))

    def test_stops_live_ticket_on_explicit_instruction(self):
        h = Harness()
        decision = accepted(h)
        h.engine.receive(trigger("2", actor=GUEST))
        h.pump()
        d2 = h.engine.decide("2", {"kind": "stop", "ticketId": decision["ticketId"], "reason": "x"})
        self.assertEqual(d2["kind"], "ignore")
        h.engine.receive(trigger("3"))
        h.pump()
        d3 = h.engine.decide("3", {"kind": "stop", "ticketId": decision["ticketId"], "reason": "human said stop"})
        self.assertEqual(d3["kind"], "stop")
        ticket = h.repo.tickets.get(decision["ticketId"])
        self.assertEqual(ticket["ended"]["by"], "human")
        self.assertEqual(h.executor.stop_count, 1)
