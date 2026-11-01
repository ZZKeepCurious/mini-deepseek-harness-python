import unittest

from superagent.core.ids import key
from superagent.core.model import waits_for_person
from superagent.core.stage import ticket_stage

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import GUEST, trigger


class AutomaticInputTests(unittest.TestCase):
    def test_holds_automatic_input_until_person_message_when_blocked(self):
        """Holds back automatic Input from a worker that reported blocked or failed until a person's message arrives."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]

        def bot(id):
            return trigger(id, human=False)

        h.engine.receive(bot("2"))
        h.engine.report(ticket_id, {"state": "blocked", "summary": "needs a decision"})
        h.engine.receive(bot("3"))
        h.settle()
        ticket = h.repo.tickets.get(ticket_id)
        self.assertEqual(ticket["undelivered"], [])
        self.assertEqual(len(ticket["inputs"]), 2)
        self.assertEqual(ticket["inputs"][1]["triggerId"], "3")
        self.assertEqual(h.delivered, [])
        h.tick(1)
        h.engine.receive(trigger("4", actor=GUEST))
        h.engine.receive(bot("5"))
        h.settle()
        delivered_keys = [pair[1] for pair in h.delivered]
        self.assertEqual(
            delivered_keys,
            [key("input", ticket_id, "trigger", "4"), key("input", ticket_id, "trigger", "5")],
        )

    def test_stops_waiting_for_person_on_message_report_or_retry(self):
        """Stops waiting for a person on a person's message, a later report that is neither blocked nor failed, or a retry."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]

        def ticket():
            return h.repo.tickets.get(ticket_id)

        h.engine.report(ticket_id, {"state": "blocked", "summary": "needs a decision"})
        self.assertTrue(waits_for_person(ticket()))
        self.assertEqual(ticket_stage(ticket()), {"stage": "person", "action": "blocked"})
        h.tick(1)
        h.engine.receive(trigger("2", actor=GUEST))
        h.settle()
        self.assertFalse(waits_for_person(ticket()))
        self.assertEqual(ticket_stage(ticket()), {"stage": "working"})
        h.tick(1)
        h.engine.report(ticket_id, {"state": "failed", "summary": "broke"})
        self.assertTrue(waits_for_person(ticket()))
        h.tick(1)
        h.engine.report(ticket_id, {"state": "progress", "summary": "found a way"})
        self.assertFalse(waits_for_person(ticket()))
        h.tick(1)
        h.engine.report(ticket_id, {"state": "blocked", "summary": "needs access"})
        h.engine.retry_ticket(ticket_id)
        self.assertFalse(waits_for_person(ticket()))

    def test_failed_launch_retries_on_person_not_policy_change(self):
        """Launches a failed launch again for a person's message, but not for a Policy change."""
        h = Harness()
        h.engine.receive(trigger("1"))
        h.pump()
        ticket_id = h.decide(
            h.requests[-1],
            {"kind": "accept", "title": "Fix", "scope": "small", "reason": "requested"},
        )["ticketId"]
        h.engine.report(ticket_id, {"state": "failed", "summary": "The prepare command failed"})
        h.executor.start_count = 0
        h.set_version("v2")
        h.engine.policy_changed("v1", "v2")
        self.assertEqual(h.executor.start_count, 0)
        h.tick(1)
        h.engine.receive(trigger("2", actor=GUEST))
        self.assertEqual(h.executor.start_count, 1)
        self.assertEqual(h.executor.launches[-1][0]["id"], ticket_id)
        self.assertEqual(h.executor.launches[-1][1]["state"], "pending")

