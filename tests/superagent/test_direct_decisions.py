import unittest

from superagent.core.errors import UserError
from superagent.core.ids import key

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import GUEST, trigger


class DirectDecisionsTests(unittest.TestCase):
    def test_direct_accept_refuses_proactive_replay_idempotent(self):
        """Decides an explicit request no Eval holds in a direct Eval, refuses a proactive accept without matched rules, and accepts a Trigger once."""
        h = Harness()
        h.engine.receive(trigger("1"))
        decision = h.engine.decide(
            "1", {"kind": "accept", "title": "Fix", "scope": "small", "reason": "routed"}
        )
        self.assertEqual(decision["kind"], "accept")
        self.assertEqual(decision["triggerIds"], ["1"])
        self.assertEqual(decision["strength"], "explicit")
        t1 = h.repo.threads.get("chat:t1")
        self.assertEqual(t1["pending"], [])
        self.assertNotIn("evaluating", t1)
        again = h.engine.decide(
            "1", {"kind": "accept", "title": "Again", "scope": "small", "reason": "routed"}
        )
        self.assertEqual(again, decision)
        h.engine.receive(trigger("2", actor=GUEST))
        h.pump()
        ticket_id = decision["ticketId"]
        with self.assertRaises(UserError):
            h.engine.decide(
                "2",
                {"kind": "accept", "title": "Fix", "scope": "small", "reason": "rule", "rules": ["r1"]},
            )
        h.engine.handback(ticket_id, "2", "A different fix")
        with self.assertRaises(UserError) as ctx:
            h.engine.decide(
                "2", {"kind": "accept", "title": "Fix", "scope": "small", "reason": "proactive"}
            )
        self.assertEqual(ctx.exception.code, "notExplicit")
        inferred = h.engine.decide(
            "2",
            {"kind": "accept", "title": "Fix", "scope": "small", "reason": "rule", "rules": ["r1"]},
        )
        self.assertEqual(inferred["kind"], "accept")
        self.assertEqual(inferred["strength"], "inferred")
        with self.assertRaises(UserError) as ctx:
            h.engine.decide("missing", {"kind": "ignore", "reason": "x"})
        self.assertEqual(ctx.exception.code, "unknownTrigger")

    def test_inferred_accept_only_admitted_rules(self):
        """Accepts an inferred candidate only on rules the Policy admits, whoever names them."""
        h = Harness({"admitsInferred": lambda rules: "enabled" in rules})
        accepted(h)
        h.engine.receive(trigger("2", actor=GUEST))
        h.engine.reevaluate("2")
        h.pump()
        candidate = h.requests[-1]
        with self.assertRaises(UserError):
            h.decide(
                candidate,
                {"kind": "accept", "title": "Fix", "scope": "small", "reason": "rule", "rules": ["disabled"]},
            )
        result = h.decide(
            candidate,
            {"kind": "accept", "title": "Fix", "scope": "small", "reason": "rule", "rules": ["enabled"]},
        )
        self.assertEqual(result["kind"], "accept")

    def test_reevaluate_replayed_trigger_returns_started_ticket_decision(self):
        """Ends the Eval of a replayed Trigger that started a ticket, and returns that ticket's Decision for it."""
        h = Harness()
        decision = accepted(h)
        h.tick(1)
        self.assertTrue(h.engine.reevaluate("1"))
        h.pump()
        self.assertIsNone(h.engine.candidate("1"))
        last = h.repo.decisions.byThread("chat:t1")[-1]
        self.assertEqual(last["kind"], "ignore")
        self.assertEqual(last["triggerIds"], ["1"])
        self.assertEqual(last["reason"][0]["code"], "startedTicket")
        replay = h.engine.decide(
            "1", {"kind": "accept", "title": "Fix", "scope": "small", "reason": "again"}
        )
        self.assertEqual(replay, decision)
        self.assertEqual(len(h.repo.tickets.list()), 1)
        with self.assertRaises(UserError):
            h.engine.reevaluate("missing")

    def test_replayed_starter_kept_out_of_batch_eval(self):
        """Keeps a replayed Trigger that started a ticket out of the Eval of the Triggers queued with it."""
        h = Harness()
        decision = accepted(h)
        h.engine.receive(trigger("2", actor=GUEST))
        h.engine.reevaluate("2")
        h.engine.reevaluate("1")
        h.pump()
        self.assertIsNone(h.engine.candidate("1"))
        cand2 = h.engine.candidate("2")
        self.assertEqual(cand2["strength"], "inferred")
        self.assertEqual(cand2["triggers"][0]["id"], "2")
        with self.assertRaises(UserError):
            h.engine.decide("2", {"kind": "accept", "title": "Other", "scope": "small", "reason": "routed"})
        self.assertEqual(
            h.engine.decide("1", {"kind": "accept", "title": "Fix", "scope": "small", "reason": "again"}),
            decision,
        )
        self.assertEqual(len(h.repo.tickets.list()), 1)

    def test_accept_beside_replayed_starts_second_ticket(self):
        """Starts a ticket from a request accepted beside a replayed one, with its own origin and acknowledgement."""
        h = Harness()
        first = accepted(h)
        h.tick(1)
        h.engine.receive(trigger("2"))
        h.engine.reevaluate("2")
        h.engine.reevaluate("1")
        h.pump()
        second = h.decide(
            h.requests[-1],
            {
                "kind": "accept",
                "title": "Other",
                "scope": "small",
                "reason": "routed",
                "acknowledgement": "On it",
            },
        )
        self.assertNotEqual(second["ticketId"], first["ticketId"])
        origin = h.repo.tickets.get(second["ticketId"])["origin"]
        self.assertEqual(origin, {"triggerId": "2", "triggerIds": ["2"]})
        ack = next(m for m in h.outbox if m["id"] == key("ack", "2"))
        self.assertEqual(ack["text"], "On it")
        self.assertEqual(ack["kind"], "acknowledgement")
        replay = h.engine.decide(
            "1", {"kind": "accept", "title": "Again", "scope": "small", "reason": "again"}
        )
        self.assertEqual(replay, first)

