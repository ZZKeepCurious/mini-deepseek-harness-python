import unittest

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import GUEST, trigger


class FollowUpsTests(unittest.TestCase):
    def test_reevaluate_takes_stale_queue_without_decision(self):
        h = Harness()
        accepted(h)
        h.engine.receive(trigger("2", actor=GUEST))
        h.engine.receive(trigger("3", actor=GUEST))
        thread = h.repo.tickets.get(h.repo.tickets.list()[0]["id"])
        t1 = h.repo.threads.get("chat:t1")
        h.repo.threads.save({**t1, "pending": ["2", "3"]})
        h.engine.reevaluate("3")
        h.pump()
        self.assertEqual([t["id"] for t in h.requests[-1]["triggers"]], ["3"])
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], [])
        ids = []
        for d in h.repo.decisions.byThread("chat:t1"):
            ids.extend(d.get("triggerIds", []))
        self.assertEqual(ids, ["1"])

    def test_gate_only_eval_beside_in_progress_eval(self):
        h = Harness()
        decision = accepted(h)
        h.engine.receive(trigger("2", actor=GUEST))
        h.engine.reevaluate("2")
        h.pump()
        evaluating = h.repo.threads.get("chat:t1")["evaluating"]
        h.engine.receive(trigger("3"))
        stop = h.engine.decide("3", {"kind": "stop", "ticketId": decision["ticketId"], "reason": "stop it"})
        self.assertEqual(stop["kind"], "stop")
        self.assertEqual(stop["triggerIds"], ["3"])
        self.assertEqual(h.repo.threads.get("chat:t1")["evaluating"], evaluating)
        ign = h.decide(h.requests[-1], {"kind": "ignore", "reason": "chat"})
        self.assertEqual(ign["kind"], "ignore")
        self.assertEqual(ign["triggerIds"], ["2"])

    def test_thread_free_after_stop_for_later_request(self):
        h = Harness()
        decision = accepted(h)
        h.engine.receive(trigger("2", actor=GUEST))
        h.pump()
        h.engine.stop_ticket(decision["ticketId"], "not needed")
        h.engine.receive(trigger("3"))
        h.pump()
        self.assertEqual([t["id"] for t in h.requests[-1]["triggers"]], ["2", "3"])
