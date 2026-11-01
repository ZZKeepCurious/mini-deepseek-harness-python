import unittest

from superagent.core.errors import UserError
from superagent.core.model import running_assignment
from superagent.core.stage import ticket_stage

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import trigger


class FailedDeliveriesTests(unittest.TestCase):
    def test_forgets_failed_delivery_on_retry_or_end(self):
        """Forgets a failed delivery once a new Assignment takes over or the ticket ends."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]

        def failed():
            aid = running_assignment(h.repo.tickets.get(ticket_id))["id"]
            h.engine.delivery_failed(ticket_id, aid, "Session busy")
            self.assertEqual(h.repo.tickets.get(ticket_id)["delivery"]["error"], "Session busy")

        failed()
        h.engine.report(ticket_id, {"state": "blocked", "summary": "needs access"})
        h.engine.retry_ticket(ticket_id)
        h.settle()
        self.assertNotIn("delivery", h.repo.tickets.get(ticket_id))
        failed()
        h.engine.stop_ticket(ticket_id)
        self.assertNotIn("delivery", h.repo.tickets.get(ticket_id))


class ProblemsTests(unittest.TestCase):
    def test_dismiss_unavailable_decision_only(self):
        """Dismisses the problem an unavailable Decision shows, and only such a Decision."""
        h = Harness()
        decision = accepted(h)
        with self.assertRaises(UserError) as ctx:
            h.engine.dismiss(decision["id"])
        self.assertEqual(ctx.exception.code, "unknownProblem")
        h.engine.receive(trigger("2", threads=[{"source": "chat", "id": "t2"}]))
        h.pump()
        unavailable = h.decide(h.requests[-1], {"kind": "unavailable", "reason": "model down"})
        dismissed = h.engine.dismiss(unavailable["id"])
        self.assertEqual(dismissed["kind"], "unavailable")
        self.assertTrue(dismissed.get("dismissed"))
        self.assertTrue(h.repo.decisions.get(unavailable["id"]).get("dismissed"))


class RetryAfterFailedDeliveryTests(unittest.TestCase):
    def test_retry_after_delivery_failed_keeps_input(self):
        """Starts a new Assignment for a ticket whose Input keeps failing to reach its worker, which takes the Input."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.report(ticket_id, {"state": "failed", "summary": "tests fail"})
        h.tick(5)
        h.engine.receive(trigger("2"))
        answered = h.repo.tickets.get(ticket_id)
        self.assertEqual(len(answered["undelivered"]), 1)
        aid = running_assignment(answered)["id"]
        h.engine.delivery_failed(ticket_id, aid, "Session gone")
        self.assertEqual(ticket_stage(h.repo.tickets.get(ticket_id)), {"stage": "person", "action": "delivery"})
        retried = h.engine.retry_ticket(ticket_id)
        states = [a["state"] for a in retried["assignments"]]
        self.assertEqual(states, ["ended", "pending"])
        self.assertNotIn("delivery", retried)
        self.assertEqual(len(retried["undelivered"]), 1)

