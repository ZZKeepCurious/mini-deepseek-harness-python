import unittest

from superagent.core.ids import key
from superagent.core.model import followup_owners

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import GUEST, trigger


class EvalPolicyBatchesTests(unittest.TestCase):
    def test_decide_under_older_policy_requeues_batch(self):
        """Queues an Eval's batch again when it is decided under an older Policy, such as after an upgrade changed the version."""
        h = Harness()
        h.engine.receive(trigger("1"))
        h.pump()
        candidate = h.requests[-1]
        h.set_version("v2")
        result = h.decide(candidate, {"kind": "accept", "title": "Fix", "scope": "small", "reason": "r"})
        self.assertEqual(result["kind"], "unavailable")
        self.assertEqual(result["reason"], [{"code": "policyChanged"}])
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], ["1"])
        h.pump()
        self.assertEqual(h.requests[-1]["policyVersion"], "v2")
        self.assertEqual(h.requests[-1]["triggers"][0]["id"], "1")

    def test_revoked_mandate_queues_person_message_says_reached_none(self):
        """Queues a person's message for Eval when the only ticket it reached cannot resume under the Policy now, and says it reached none."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.report(ticket_id, {"state": "ready", "summary": "Done"})
        h.settle()
        h.set_version("revoked")
        h.tick(5)
        h.engine.receive(trigger("2"))
        ticket = h.repo.tickets.get(ticket_id)
        self.assertEqual(ticket["undelivered"], [])
        self.assertIn("ended", ticket)
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], ["2"])
        self.assertEqual(followup_owners(trigger("2"), h.repo), [])

    def test_pause_then_revoke_queues_when_unpaused(self):
        """Queues a person's message that waited out a pause for Eval when the ticket it reached cannot resume once the pause lifts."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.report(ticket_id, {"state": "ready", "summary": "Done"})
        h.settle()
        h.set_paused(True)
        h.tick(5)
        h.engine.receive(trigger("2"))
        h.pump()
        self.assertEqual(len(h.repo.tickets.get(ticket_id)["undelivered"]), 1)
        h.set_version("revoked")
        h.set_paused(False)
        h.engine.evaluate_due()
        ticket = h.repo.tickets.get(ticket_id)
        self.assertEqual(ticket["undelivered"], [])
        self.assertIn("ended", ticket)
        pending = h.repo.threads.get("chat:t1")["pending"]
        self.assertTrue("2" in pending or h.engine.candidate("2") is not None)

    def test_origin_and_ack_from_strongest_admissible_trigger(self):
        """Takes a new ticket's origin and acknowledgement from the admissible Trigger that gave its batch its strength."""
        h = Harness()
        h.engine.receive(trigger("1"))
        h.tick(1)
        h.engine.receive(
            trigger(
                "2",
                human=False,
                actor={"source": "chat", "id": "ci-bot"},
                channel={"source": "chat", "thread": "chat:t1", "address": {"message": "bot"}},
            )
        )
        h.pump()
        self.assertEqual([t["id"] for t in h.requests[-1]["triggers"]], ["1", "2"])
        decision = h.decide(
            h.requests[-1],
            {
                "kind": "accept",
                "title": "Fix",
                "scope": "small",
                "reason": "r",
                "acknowledgement": "ok",
            },
        )
        origin = h.repo.tickets.get(decision["ticketId"])["origin"]
        self.assertEqual(origin, {"triggerId": "1", "triggerIds": ["1", "2"]})
        self.assertEqual(
            h.outbox,
            [next(m for m in h.outbox if m["id"] == key("ack", "1"))],
        )
        ack = h.outbox[0]
        self.assertEqual(ack["channel"], trigger("1")["channel"])

