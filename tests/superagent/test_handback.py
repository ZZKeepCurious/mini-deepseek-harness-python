import unittest

from superagent.core.errors import UserError

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import GUEST, trigger


class HandBackTests(unittest.TestCase):
    def test_queues_handback_for_fresh_eval_once_per_ticket_keeps_subscription(self):
        """Queues a message that reached a ticket for a fresh Eval with the gate's strength, once per ticket, and keeps the subscription."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.receive(trigger("2"))
        h.engine.receive(trigger("3", actor=GUEST))
        h.pump()
        self.assertTrue(h.engine.handback(ticket_id, "2", "A new request"))
        h.pump()
        self.assertEqual(h.requests[-1]["strength"], "explicit")
        self.assertEqual(h.requests[-1]["triggers"][0]["id"], "2")
        handbacks = h.repo.triggers.disposition("2")["handbacks"]
        self.assertEqual(len(handbacks), 1)
        self.assertEqual(handbacks[0]["ticketId"], ticket_id)
        self.assertEqual(handbacks[0]["reason"], "A new request")
        self.assertIn("at", handbacks[0])
        h.decide(h.requests[-1], {"kind": "ignore", "reason": "chat"})
        with self.assertRaises(UserError) as ctx:
            h.engine.handback(ticket_id, "2", "Again")
        self.assertEqual(ctx.exception.code, "handedBack")
        self.assertTrue(h.engine.handback(ticket_id, "3", "A guest asks for more"))
        h.pump()
        self.assertEqual(h.requests[-1]["strength"], "inferred")
        self.assertEqual(h.requests[-1]["triggers"][0]["id"], "3")
        self.assertIn(ticket_id, h.repo.threads.get("chat:t1")["subscriptions"])
        h.engine.receive(trigger("4", threads=[{"source": "chat", "id": "elsewhere"}]))
        with self.assertRaises(UserError) as ctx:
            h.engine.handback(ticket_id, "4", "Not mine")
        self.assertEqual(ctx.exception.code, "notReached")

    def test_handback_false_when_ticket_did_not_decide_message(self):
        """Queues again no message the ticket did not decide: one a person sent to Eval, a bot's, or one on a Thread followed for context."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.follow_for_context(ticket_id, {"source": "chat", "id": "issue"})
        h.engine.receive(trigger("2"))
        h.engine.reevaluate("2")
        h.engine.receive(trigger("3", human=False))
        h.engine.receive(trigger("4", threads=[{"source": "chat", "id": "issue"}]))
        h.pump()
        for candidate in h.requests[-2:]:
            h.decide(candidate, {"kind": "ignore", "reason": "chat"})
        for tid in ("2", "3", "4"):
            self.assertFalse(h.engine.handback(ticket_id, tid, "Not this ticket"))
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], [])
        self.assertEqual(h.repo.threads.get("chat:issue")["pending"], [])
        handbacks = h.repo.triggers.disposition("4")["handbacks"]
        self.assertEqual(handbacks[0]["ticketId"], ticket_id)
        self.assertEqual(handbacks[0]["reason"], "Not this ticket")
        with self.assertRaises(UserError) as ctx:
            h.engine.attach(ticket_id, "4")
        self.assertEqual(ctx.exception.code, "handedBackHere")

    def test_attached_message_handback_sends_to_eval(self):
        """Leaves a message the main Session attached to a ticket to its worker, whose hand-back sends it to Eval."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.receive(trigger("2", threads=[{"source": "chat", "id": "x"}]))
        h.pump()
        h.engine.attach(ticket_id, "2")
        h.settle()
        accept = {"kind": "accept", "title": "Y", "scope": "small", "reason": "requested"}
        with self.assertRaises(UserError) as ctx:
            h.engine.decide("2", accept)
        self.assertEqual(ctx.exception.code, "workerDecides")
        self.assertTrue(h.engine.handback(ticket_id, "2", "Asks for another change"))
        self.assertEqual(h.repo.threads.get("chat:x")["pending"], ["2"])

