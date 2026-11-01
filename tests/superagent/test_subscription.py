import unittest

from superagent.core.errors import UserError
from superagent.core.ids import key

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import GUEST, trigger


class SubscriptionFanOutTests(unittest.TestCase):
    def test_delivers_person_message_to_live_ticket_not_eval(self):
        h = Harness()
        decision = accepted(h)
        requests = len(h.requests)
        h.engine.receive(trigger("2", actor=GUEST))
        h.pump()
        ticket = h.repo.tickets.get(decision["ticketId"])
        self.assertEqual(len(ticket["inputs"]), 1)
        self.assertEqual(ticket["inputs"][0]["kind"], "trigger")
        self.assertEqual(ticket["inputs"][0]["triggerId"], "2")
        self.assertEqual(ticket["undelivered"], [])
        self.assertEqual(h.delivered, [(ticket["id"], ticket["inputs"][0]["id"])])
        disp = h.repo.triggers.disposition("2")
        self.assertEqual(disp["fanout"], [ticket["id"]])
        self.assertIn("at", disp)
        self.assertEqual(len(h.requests), requests)
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], [])
        self.assertEqual(len(h.repo.decisions.byThread("chat:t1")), 1)
        h.tick(600)
        h.engine.evaluate_due()
        self.assertEqual(len(h.repo.decisions.byThread("chat:t1")), 1)
        h.engine.receive(trigger("3", human=False))
        h.pump()
        last = h.repo.decisions.byThread("chat:t1")[-1]
        self.assertEqual(last["kind"], "ignore")
        self.assertEqual(last["triggerIds"], ["3"])

    def test_keeps_inputs_undelivered_until_assignment_running(self):
        h = Harness()
        h.engine.receive(trigger("1"))
        h.pump()
        decision = h.decide(
            h.requests[0],
            {"kind": "accept", "title": "Fix", "scope": "small", "reason": "requested"},
        )
        h.settle(launch=False)
        h.engine.receive(trigger("2"))
        h.engine.evaluate_due()
        h.settle(launch=False)
        ticket = h.repo.tickets.get(decision["ticketId"])
        self.assertEqual(len(ticket["undelivered"]), 1)
        self.assertEqual(h.executor.deliver_count, 0)
        self.assertEqual(h.executor.start_count, 2)
        h.engine.assignment_started(ticket["id"], ticket["assignments"][0]["id"])
        h.settle()
        self.assertEqual(h.delivered, [(ticket["id"], ticket["undelivered"][0])])

    def test_does_not_hand_bot_message_to_ended_ticket(self):
        h = Harness()
        decision = accepted(h)
        h.engine.report(decision["ticketId"], {"state": "ready", "summary": "done"})
        h.engine.receive(trigger("2", human=False))
        h.pump()
        ticket = h.repo.tickets.get(decision["ticketId"])
        self.assertIn("ended", ticket)
        self.assertEqual(ticket["undelivered"], [])
        self.assertEqual(h.executor.start_count, 1)

    def test_resume_pump_mandate_revoked_and_retry(self):
        h = Harness()
        decision = accepted(h)
        h.engine.report(decision["ticketId"], {"state": "ready", "summary": "done"})
        h.set_version("revoked")
        h.engine.receive(trigger("2", actor=GUEST))
        h.pump()
        self.assertIn("ended", h.repo.tickets.get(decision["ticketId"]))
        h.set_version("v1")
        h.pump()
        self.assertIn("ended", h.repo.tickets.get(decision["ticketId"]))
        h.engine.receive(trigger("3", actor=GUEST))
        h.pump()
        h.pump()
        ticket = h.repo.tickets.get(decision["ticketId"])
        self.assertEqual(len(ticket["assignments"]), 2)
        resumed = [i for i in ticket["inputs"] if i.get("kind") == "resumed"]
        self.assertEqual(len(resumed), 1)

    def test_delivery_reports_only_from_running_assignment(self):
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        assignment = h.repo.tickets.get(ticket_id)["assignments"][0]["id"]
        h.engine.receive(trigger("2"))
        input_id = key("input", ticket_id, "trigger", "2")
        h.engine.delivery_failed(ticket_id, assignment, "Session busy")
        ticket = h.repo.tickets.get(ticket_id)
        self.assertEqual(ticket["delivery"]["error"], "Session busy")
        h.engine.delivery_failed(ticket_id, "another", "other")
        h.engine.inputs_delivered(ticket_id, "another", [input_id])
        ticket = h.repo.tickets.get(ticket_id)
        self.assertEqual(ticket["undelivered"], [input_id])
        self.assertEqual(ticket["delivery"]["error"], "Session busy")
        h.engine.inputs_delivered(ticket_id, assignment, [input_id])
        ticket = h.repo.tickets.get(ticket_id)
        self.assertEqual(ticket["undelivered"], [])
        self.assertNotIn("delivery", ticket)
        h.engine.receive(trigger("3"))
        h.engine.stop_ticket(ticket_id)
        h.engine.delivery_failed(ticket_id, assignment, "late")
        ticket = h.repo.tickets.get(ticket_id)
        self.assertIn("ended", ticket)
        self.assertEqual(ticket["undelivered"], [])
        self.assertNotIn("delivery", ticket)

    def test_unsubscribe_stops_fan_out(self):
        h = Harness()
        decision = accepted(h)
        self.assertTrue(h.engine.unsubscribe("chat:t1"))
        h.engine.receive(trigger("2"))
        h.pump()
        self.assertEqual(h.repo.tickets.get(decision["ticketId"])["inputs"], [])

    def test_mark_unrelated_drops_subscription(self):
        events = []
        h = Harness(emit=lambda e: events.append(e))
        first = accepted(h)["ticketId"]
        second = accepted(h, "2", "t2")["ticketId"]
        h.engine.receive(trigger("3", threads=[{"source": "chat", "id": "t2"}]))
        h.engine.handback(second, "3", "It is about the first fix")
        h.engine.attach(first, "3")
        self.assertEqual(h.repo.threads.get("chat:t2")["subscriptions"], [second, first])
        h.engine.mark_unrelated(first, "chat:t2", "about another fix")
        self.assertEqual(h.repo.threads.get("chat:t2")["subscriptions"], [second])
        unsub = [e for e in events if e["type"] == "thread.unsubscribed"]
        self.assertEqual(
            unsub[0]["data"],
            {"threadKey": "chat:t2", "ticketId": first, "by": "executor", "reason": "about another fix"},
        )
        with self.assertRaises(UserError):
            h.engine.mark_unrelated(first, "chat:t2", "again")
        with self.assertRaises(UserError):
            h.engine.mark_unrelated(first, "chat:t1", "its own")
        h.engine.bind_thread(first, {"source": "gh", "id": "pr-9"}, True)
        with self.assertRaises(UserError):
            h.engine.mark_unrelated(first, "gh:pr-9", "its PR")
