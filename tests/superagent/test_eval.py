import unittest

from superagent.core.errors import UserError
from superagent.core.ids import key

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import MEMBER, trigger


class EvalTests(unittest.TestCase):
    def test_ignores_at_the_gate_without_an_assessor_when_nothing_is_admissible(self):
        h = Harness()
        h.engine.receive(trigger("1", human=False))
        h.pump()
        self.assertEqual(len(h.assessor.requests), 0)
        decisions = h.repo.decisions.byThread("chat:t1")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["kind"], "ignore")
        self.assertEqual(decisions[0]["triggerIds"], ["1"])
        self.assertIsNotNone(h.repo.triggers.disposition("1").get("decisionId"))

    def test_releases_the_assessors_state_for_every_eval_that_ends(self):
        h = Harness()
        h.engine.receive(trigger("1"))
        h.pump()
        h.decide(h.requests[0], {"kind": "ignore", "reason": "chatter"})
        self.assertEqual(h.assessor.released, [h.requests[0]["evalId"]])

    def test_serializes_evals_per_thread_and_debounces_triggers(self):
        h = Harness()
        h.engine.receive(trigger("1"))
        h.pump()
        h.engine.receive(trigger("2"))
        h.engine.receive(trigger("3"))
        h.pump()
        self.assertEqual(len(h.requests), 1)
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], ["2", "3"])
        h.decide(h.requests[0], {"kind": "ignore", "reason": "chatter"})
        h.pump()
        self.assertEqual([t["id"] for t in h.requests[1]["triggers"]], ["2", "3"])
        self.assertEqual(h.requests[1]["strength"], "explicit")

    def test_accept_creates_ticket_mandate_subscription_ack_and_assignment(self):
        h = Harness()
        decision = accepted(h)
        ticket = h.repo.tickets.get(decision["ticketId"])
        self.assertEqual(ticket["title"], "Fix")
        self.assertEqual(
            ticket["mandate"],
            {
                "strength": "explicit",
                "policyVersion": "v1",
                "basis": {"via": "listed"},
                "actor": MEMBER,
                "scope": "small",
            },
        )
        self.assertNotIn("ended", ticket)
        self.assertEqual(ticket["assignments"][0]["state"], "running")
        self.assertIsNotNone(ticket["assignments"][0].get("startedAt"))
        self.assertEqual(h.repo.threads.get("chat:t1")["subscriptions"], [ticket["id"]])
        self.assertEqual(
            h.outbox,
            [
                {
                    "id": key("ack", "1"),
                    "channel": trigger("1")["channel"],
                    "text": "ok",
                    "kind": "acknowledgement",
                    "ticketId": ticket["id"],
                }
            ],
        )
        self.assertEqual(len(h.executor.launches), 0)

    def test_refuses_stale_eval_and_decides_the_later_one(self):
        h = Harness()
        h.engine.receive(trigger("1"))
        h.pump()
        first = h.requests[0]
        h.set_version("v2")
        h.engine.policy_changed("v1", "v2")
        h.pump()
        second = h.requests[1]
        with self.assertRaises(UserError):
            h.decide(first, {"kind": "ignore", "reason": "chatter"})
        decision = h.decide(second, {"kind": "ignore", "reason": "chatter"})
        self.assertEqual(decision["kind"], "ignore")
        self.assertEqual(decision["evalId"], second["evalId"])

    def test_records_accept_atomically_or_none(self):
        def prepare_ticket(_ticket, _outcome):
            raise RuntimeError("No directory for the ticket")

        h = Harness(prepare_ticket=prepare_ticket)
        h.engine.receive(trigger("1"))
        h.pump()
        eval_id = h.requests[0]["evalId"]
        with self.assertRaises(RuntimeError):
            h.decide(
                h.requests[0],
                {
                    "kind": "accept",
                    "title": "Fix",
                    "scope": "small",
                    "reason": "requested",
                    "acknowledgement": "ok",
                },
            )
        self.assertEqual(h.repo.decisions.byThread("chat:t1"), [])
        self.assertEqual(h.repo.tickets.list(), [])
        thread = h.repo.threads.get("chat:t1")
        self.assertEqual(thread["evaluating"]["id"], eval_id)
        self.assertEqual(thread["subscriptions"], [])
        self.assertEqual(h.outbox, [])

    def test_records_unavailable_when_policy_version_changed_during_assessment(self):
        h = Harness()
        h.engine.receive(trigger("1"))
        h.pump()
        h.set_version("v2")
        decision = h.decide(
            h.requests[0],
            {"kind": "accept", "title": "x", "scope": "x", "reason": "x"},
        )
        self.assertEqual(decision["kind"], "unavailable")
        self.assertEqual(h.repo.tickets.list(), [])

    def test_starts_every_pending_eval_in_one_step(self):
        h = Harness()
        for id, thread in [("1", "A"), ("2", "B")]:
            h.engine.receive(trigger(id, threads=[{"source": "chat", "id": thread}]))
        h.engine.evaluate_due()
        h.engine.evaluate_due()
        keys = [c["thread"]["key"] for c in h.requests]
        self.assertEqual(keys, ["chat:A", "chat:B"])

    def test_eval_deadline_requeue_then_unavailable(self):
        h = Harness()
        h.engine.receive(trigger("1"))
        h.pump()
        h.tick(600)
        h.pump()
        self.assertEqual(h.engine.candidate("1")["evalId"], h.requests[0]["evalId"])

        def deliver():
            h.engine.candidate_delivered(h.requests[-1]["evalId"])

        deliver()
        h.tick(59)
        h.pump()
        self.assertEqual(len(h.requests), 1)
        h.tick(2)
        h.pump()
        self.assertEqual(h.repo.decisions.byThread("chat:t1"), [])
        self.assertIn(h.requests[0]["evalId"], h.assessor.released)
        self.assertEqual([[t["id"] for t in item["triggers"]] for item in h.requests], [["1"], ["1"]])
        deliver()
        h.tick(61)
        h.pump()
        self.assertEqual(len(h.requests), 3)
        deliver()
        h.tick(61)
        h.pump()
        decisions = h.repo.decisions.byThread("chat:t1")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0]["kind"], "unavailable")
        self.assertEqual(decisions[0]["reason"], [{"code": "timeout"}])
        thread = h.repo.threads.get("chat:t1")
        self.assertEqual(thread.get("pending"), [])
        self.assertNotIn("evaluating", thread)
        self.assertNotIn("expired", thread)
