import unittest

from superagent.core.errors import UserError
from superagent.core.ids import key

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import GUEST, MEMBER, trigger


class GapsTests(unittest.TestCase):
    def test_mixed_batch_strength_from_strongest_admissible(self):
        """Takes a mixed batch's strength, actor and basis from its strongest admissible Trigger, the latest of equal strength."""

        def gate(t, thread, tickets):
            if t["actor"]["id"] == "alice":
                return {"admissible": True, "strength": "explicit", "basis": {"via": "listed"}}
            return {"admissible": True, "strength": "inferred", "basis": {"via": "followup"}}

        for order in (["guest", "alice"], ["alice", "guest"]):
            h = Harness({"gate": gate})
            for index, who in enumerate(order):
                h.tick(1)
                h.engine.receive(trigger(str(10 + index), actor=MEMBER if who == "alice" else GUEST))
            h.pump()
            alice = str(10 + order.index("alice"))
            ev = h.repo.threads.get("chat:t1")["evaluating"]
            self.assertEqual(ev["strength"], "explicit")
            self.assertEqual(ev["actor"], MEMBER)
            self.assertEqual(ev["basis"], {"via": "listed"})
            self.assertEqual(ev["primary"], alice)

        def named_gate(t, thread, tickets):
            return {
                "admissible": True,
                "strength": "explicit",
                "basis": {"via": "listed", "sender": t["actor"]["id"]},
            }

        named = Harness({"gate": named_gate})
        named.engine.receive(trigger("1", actor={"source": "chat", "id": "bob"}))
        named.tick(1)
        named.engine.receive(trigger("2", actor={"source": "chat", "id": "carol"}))
        named.pump()
        ev = named.repo.threads.get("chat:t1")["evaluating"]
        self.assertEqual(ev["actor"], {"source": "chat", "id": "carol"})
        self.assertEqual(ev["basis"]["sender"], "carol")
        self.assertEqual(ev["primary"], "2")

    def test_late_start_of_replaced_assignment_no_op(self):
        """Changes nothing and delivers nothing on a late start of an Assignment a retry replaced."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        first = h.repo.tickets.get(ticket_id)["assignments"][0]["id"]
        h.engine.report(ticket_id, {"state": "failed", "summary": "broke"})
        h.engine.retry_ticket(ticket_id)
        before = h.repo.tickets.get(ticket_id)
        delivered_len = len(h.delivered)
        h.engine.assignment_started(ticket_id, first)
        h.settle(launch=False)
        self.assertEqual(h.repo.tickets.get(ticket_id)["assignments"], before["assignments"])
        self.assertEqual(len(h.delivered), delivered_len)

    def test_stop_unknown_or_ended_ticket_recorded_ignore(self):
        """Records a stop that names an unknown or ended ticket as ignored."""
        h = Harness()
        ended = accepted(h)["ticketId"]
        h.engine.report(ended, {"state": "ready", "summary": "Done"})
        for tid, ticket_id in (("2", "missing"), ("3", ended)):
            h.tick(1)
            h.engine.receive(trigger(tid, threads=[{"source": "chat", "id": f"t{tid}"}]))
            h.pump()
            decision = h.decide(
                h.requests[-1], {"kind": "stop", "ticketId": ticket_id, "reason": "stop it"}
            )
            self.assertEqual(decision["kind"], "ignore")
            self.assertEqual(decision["reason"][0]["code"], "stopUnknown")
        self.assertEqual(h.repo.tickets.get(ended)["ended"]["by"], "executor")
        self.assertEqual(h.repo.tickets.get(ended)["ended"]["outcome"], "completed")

    def test_refuses_direct_decision_when_not_admissible(self):
        """Refuses a direct decision on a Trigger the gate does not admit, and leaves the queue as it was."""
        h = Harness()
        h.engine.receive(trigger("1", human=False))
        pending = list(h.repo.threads.get("chat:t1")["pending"])
        with self.assertRaises(UserError) as ctx:
            h.engine.decide("1", {"kind": "ignore", "reason": "noise"})
        self.assertEqual(ctx.exception.code, "notAdmissible")
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], pending)
        self.assertEqual(h.repo.decisions.byThread("chat:t1"), [])

    def test_refuses_retry_after_person_stopped_failed(self):
        """Refuses to retry a ticket a person stopped after its failed report."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.report(ticket_id, {"state": "failed", "summary": "broke"})
        h.engine.stop_ticket(ticket_id)
        with self.assertRaises(UserError):
            h.engine.retry_ticket(ticket_id)

    def test_answer_and_result_once_rules(self):
        """Answers a question once, sends a result only for finished work, and the same result once."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.tick(1)
        h.engine.receive(trigger("2"))
        h.settle()
        h.engine.report(ticket_id, {"state": "progress", "summary": "It reads the config.", "answers": "2"})
        ans = h.engine.reply({"purpose": "answer", "triggerId": "2", "text": "It reads the config."})
        self.assertFalse(ans["reused"])
        with self.assertRaises(UserError):
            h.engine.reply({"purpose": "answer", "triggerId": "2", "text": "Something else."})
        with self.assertRaises(UserError):
            h.engine.reply({"purpose": "result", "ticketId": ticket_id, "text": "Done"})
        h.engine.report(ticket_id, {"state": "ready", "summary": "Done"})
        sent = len(h.outbox)
        r1 = h.engine.reply({"purpose": "result", "ticketId": ticket_id, "text": "Done"})
        self.assertFalse(r1["reused"])
        r2 = h.engine.reply({"purpose": "result", "ticketId": ticket_id, "text": "Done"})
        self.assertTrue(r2["reused"])
        self.assertEqual(len(h.outbox), sent + 1)
        closed = accepted(h, "5", "t5")["ticketId"]
        h.engine.bind_thread(closed, {"source": "gh", "id": "pr-1"}, True)
        h.engine.thread_closed({"source": "gh", "id": "pr-1"}, "closed", "2026-09-23T00:01:00Z")
        with self.assertRaises(UserError):
            h.engine.reply({"purpose": "result", "ticketId": closed, "text": "x"})
        stopped = accepted(h, "6", "t6")["ticketId"]
        h.engine.stop_ticket(stopped)
        with self.assertRaises(UserError):
            h.engine.reply({"purpose": "result", "ticketId": stopped, "text": "x"})

    def test_eval_deadline_on_repeat_delivery_reevaluate_order(self):
        """Keeps an Eval's deadline on a repeated delivery, refuses to queue a held Trigger again, and puts expired Triggers first."""
        h = Harness()
        h.engine.receive(trigger("1"))
        h.pump()
        eval_id = h.requests[0]["evalId"]
        h.engine.candidate_delivered(eval_id)
        deadline = h.repo.threads.get("chat:t1")["evaluating"]["deadline"]
        h.tick(30)
        h.engine.candidate_delivered(eval_id)
        self.assertEqual(h.repo.threads.get("chat:t1")["evaluating"]["deadline"], deadline)
        self.assertFalse(h.engine.reevaluate("1"))
        h.tick(1)
        h.engine.receive(trigger("2", actor=GUEST))
        h.tick(30)
        h.engine.evaluate_due()
        self.assertEqual(h.repo.threads.get("chat:t1")["evaluating"]["triggerIds"], ["1", "2"])
        self.assertEqual(h.repo.decisions.byThread("chat:t1"), [])


class FinalReviewTests(unittest.TestCase):
    def test_legacy_eval_started_ticket_records_ignore_not_unavailable(self):
        """Ignores an Eval an earlier version left holding only a Trigger that started a ticket, instead of recording it unavailable."""
        h = Harness()
        decision = accepted(h)
        thread = h.repo.threads.get("chat:t1")
        h.repo.threads.save(
            {
                **thread,
                "evaluating": {
                    "id": "old-eval",
                    "threadKey": "chat:t1",
                    "triggerIds": ["1"],
                    "primary": "1",
                    "strength": "explicit",
                    "actor": MEMBER,
                    "basis": {"via": "listed"},
                    "policyVersion": "v1",
                    "startedAt": "",
                    "deadline": "2999-01-01T00:00:00.000Z",
                },
            }
        )
        replay = h.engine.decide(
            "1", {"kind": "accept", "title": "Again", "scope": "small", "reason": "replayed"}
        )
        self.assertEqual(replay, decision)
        last = h.repo.decisions.byThread("chat:t1")[-1]
        self.assertEqual(last["kind"], "ignore")
        self.assertEqual(last["reason"][0]["code"], "startedTicket")
        self.assertFalse(any(d["kind"] == "unavailable" for d in h.repo.decisions.byThread("chat:t1")))
        self.assertNotIn("evaluating", h.repo.threads.get("chat:t1"))

    def test_clears_expired_count_when_gate_ignores(self):
        """Clears the count of expired Evals when the gate ignores what a Thread queued."""
        h = Harness()
        h.engine.receive(trigger("1", human=False))
        t1 = h.repo.threads.get("chat:t1")
        h.repo.threads.save({**t1, "expired": 1})
        h.pump()
        decisions = h.repo.decisions.byThread("chat:t1")
        self.assertEqual(decisions[-1]["kind"], "ignore")
        self.assertNotIn("expired", h.repo.threads.get("chat:t1"))


class FinalReviewPolicyTransitionsTests(unittest.TestCase):
    def test_repeated_policy_change_inputs(self):
        """Tells a worker of each Policy change that bears on its ticket, also of one that repeats an earlier transition."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        effect = {"requesterAllowed": False}
        for from_v, to_v in (("A", "B"), ("B", "A"), ("A", "B")):
            h.tick(1)
            h.engine.policy_changed(from_v, to_v, {ticket_id: effect})
        pc = [i for i in h.repo.tickets.get(ticket_id)["inputs"] if i.get("kind") == "policy-changed"]
        self.assertEqual(len(pc), 3)


class FinalReviewContextThreadsTests(unittest.TestCase):
    def test_context_thread_no_ack_or_question(self):
        """Lets a worker acknowledge or ask no one on a Thread it follows only for context."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.follow_for_context(ticket_id, {"source": "chat", "id": "side"})
        h.tick(1)
        h.engine.receive(
            trigger(
                "5",
                threads=[{"source": "chat", "id": "side"}],
                channel={"source": "chat", "thread": "side", "address": {"message": "5"}},
            )
        )
        with self.assertRaises(UserError) as ctx:
            h.engine.reply(
                {"purpose": "acknowledgement", "ticketId": ticket_id, "triggerId": "5", "text": "On it"}
            )
        self.assertEqual(ctx.exception.code, "notReached")
        with self.assertRaises(UserError) as ctx:
            h.engine.report(
                ticket_id,
                {"state": "progress", "summary": "A question", "question": "Which page?", "askedBy": "5"},
            )
        self.assertEqual(ctx.exception.code, "notAsker")


class FinalReviewCloseTimesTests(unittest.TestCase):
    def test_canonical_close_time_updates_ticket(self):
        """Records a Source's close time in the canonical form, and keeps the ticket's updatedAt moving forward."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-1"}, True)
        h.tick(30)
        before = h.repo.tickets.get(ticket_id)["updatedAt"]
        h.engine.thread_closed({"source": "gh", "id": "pr-1"}, "merged", "2026-09-23T00:00:05Z")
        ended = h.repo.tickets.get(ticket_id)
        self.assertEqual(ended["ended"]["at"], "2026-09-23T00:00:05.000Z")
        self.assertEqual(ended["ended"]["outcome"], "merged")
        self.assertEqual(
            ended["follows"],
            [
                {
                    "thread": "gh:pr-1",
                    "own": True,
                    "closed": {"at": "2026-09-23T00:00:05.000Z", "outcome": "merged"},
                }
            ],
        )
        self.assertGreaterEqual(ended["updatedAt"], before)


class IgnoresFromCardTests(unittest.TestCase):
    def test_inferred_ignore_from_card_marked(self):
        """Marks the ignore of an inferred candidate that no assessment judged, unlike an assessed one or an explicit candidate's."""

        def said(text):
            return {"code": "said", "values": {"text": text}}

        h = Harness()
        accepted(h)
        h.engine.receive(trigger("2", actor=GUEST))
        h.engine.reevaluate("2")
        h.pump()
        self.assertEqual(h.requests[-1]["strength"], "inferred")
        ign = h.engine.decide("2", {"kind": "ignore", "reason": "Plain chat."})
        self.assertEqual(ign["reason"][0]["code"], "ignoredFromCard")
        self.assertEqual(ign["reason"][1], said("Plain chat."))
        h.engine.receive(trigger("3", actor=GUEST))
        h.engine.reevaluate("3")
        h.pump()
        assessed = h.decide(
            h.requests[-1],
            {"kind": "ignore", "reason": "Plain chat.", "audit": {"rule_assessments": []}},
        )
        self.assertEqual(assessed["reason"], [said("Plain chat.")])
        self.assertEqual(assessed.get("audit"), {"rule_assessments": []})
        h.engine.receive(trigger("4", threads=[{"source": "chat", "id": "t9"}]))
        h.pump()
        explicit_ign = h.engine.decide("4", {"kind": "ignore", "reason": "Thanks."})
        self.assertEqual(explicit_ign["reason"], [said("Thanks.")])
        h.engine.receive(trigger("5", actor=GUEST))
        h.engine.reevaluate("5")
        h.pump()
        with self.assertRaises(UserError):
            h.engine.decide("5", {"kind": "accept", "title": "Fix", "scope": "small", "reason": "chat"})

