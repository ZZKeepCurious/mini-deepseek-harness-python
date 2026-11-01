import unittest

from superagent.core.errors import UserError
from superagent.core.ids import key
from superagent.core.model import followup_owners

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import GUEST, MEMBER, trigger

ACCEPT = {"kind": "accept", "title": "Y", "scope": "small", "reason": "requested"}


class FollowUpOwnersTests(unittest.TestCase):
    def test_main_session_takes_followup_after_stop_on_ticket(self):
        """Lets the main Session take a follow-up as new work once it stopped the ticket on it."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.receive(trigger("2"))
        h.settle()
        with self.assertRaises(UserError) as ctx:
            h.engine.decide("2", ACCEPT)
        self.assertEqual(ctx.exception.code, "workerDecides")
        stop = h.engine.decide("2", {"kind": "stop", "ticketId": ticket_id, "reason": "asked to stop"})
        self.assertEqual(stop["kind"], "stop")
        accept = h.engine.decide("2", ACCEPT)
        self.assertEqual(accept["kind"], "accept")
        self.assertEqual(accept["triggerIds"], ["2"])
        kinds = [
            d["kind"]
            for d in h.repo.decisions.byThread("chat:t1")
            if "2" in d.get("triggerIds", [])
        ]
        self.assertEqual(kinds, ["stop", "accept"])

    def test_followup_to_eval_when_ticket_ends_by_report_or_stop(self):
        """Sends a follow-up its worker never received to Eval when the ticket ends, by a report or a stop."""
        for end in ("report", "stop"):
            h = Harness()
            ticket_id = accepted(h)["ticketId"]
            h.engine.receive(trigger("2"))
            self.assertEqual(len(h.repo.tickets.get(ticket_id)["undelivered"]), 1)
            if end == "report":
                h.engine.report(ticket_id, {"state": "dropped", "summary": "nothing to do"})
            else:
                h.engine.stop_ticket(ticket_id, "not needed")
            self.assertEqual(h.repo.threads.get("chat:t1")["pending"], ["2"])
            h.pump()
            self.assertEqual([t["id"] for t in h.requests[-1]["triggers"]], ["2"])

    def test_followup_to_eval_after_end_unless_acknowledged(self):
        """Sends a follow-up its worker got since its last report to Eval when the ticket ends, unless it acknowledged it."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.report(ticket_id, {"state": "progress", "summary": "working"})
        h.tick(1)
        h.engine.receive(trigger("2"))
        h.engine.receive(trigger("3"))
        h.settle()
        h.engine.reply(
            {"purpose": "acknowledgement", "ticketId": ticket_id, "triggerId": "2", "text": "On it"}
        )
        h.engine.report(ticket_id, {"state": "dropped", "summary": "nothing to do"})
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], ["3"])
        self.assertEqual(h.repo.triggers.disposition("2")["fanout"], [ticket_id])
        self.assertEqual(h.repo.triggers.disposition("3")["fanout"], [])

    def test_merge_keeps_ticket_open_while_worker_on_person_message(self):
        """Keeps a ticket open after its pull request merged while its worker is on a person's message it took up after its last report."""
        for taken in (True, False):
            h = Harness()
            ticket_id = accepted(h)["ticketId"]
            h.engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-9"}, True)
            h.engine.report(ticket_id, {"state": "ready", "summary": "PR ready"})
            h.tick(1)
            h.engine.receive(trigger("2"))
            h.settle()
            if taken:
                h.engine.reply(
                    {"purpose": "acknowledgement", "ticketId": ticket_id, "triggerId": "2", "text": "On it"}
                )
            h.engine.thread_closed({"source": "gh", "id": "pr-9"}, "merged", "2026-09-23T00:00:01Z")
            ticket = h.repo.tickets.get(ticket_id)
            pending = h.repo.threads.get("chat:t1")["pending"]
            if taken:
                self.assertNotIn("ended", ticket)
                self.assertEqual(pending, [])
            else:
                self.assertEqual(ticket["ended"]["outcome"], "merged")
                self.assertEqual(pending, ["2"])
            if taken:
                h.engine.report(ticket_id, {"state": "ready", "summary": "Done"})
            self.assertEqual(h.repo.tickets.get(ticket_id)["ended"]["outcome"], "merged")

    def test_attach_gives_followup_back_to_ended_ticket(self):
        """Gives a follow-up its ended ticket let go to that ticket again when the main Session attaches it there."""
        for resumed in (False, True):
            h = Harness()
            ticket_id = accepted(h)["ticketId"]
            h.engine.report(ticket_id, {"state": "progress", "summary": "working"})
            h.tick(1)
            h.engine.receive(trigger("2"))
            h.settle()
            h.engine.report(ticket_id, {"state": "dropped", "summary": "nothing to do"})
            if resumed:
                h.tick(1)
                h.engine.receive(trigger("3"))
            h.pump()
            h.tick(1)
            h.engine.attach(ticket_id, "2")
            h.settle()
            copies = [
                inp
                for inp in h.repo.tickets.get(ticket_id)["inputs"]
                if inp.get("kind") == "trigger" and inp.get("triggerId") == "2"
            ]
            self.assertEqual(h.delivered[-1], (ticket_id, copies[1]["id"]))
            self.assertNotIn("ended", h.repo.tickets.get(ticket_id))
            self.assertEqual(followup_owners(trigger("2"), h.repo), [ticket_id])

    def test_attach_wakes_ended_ticket_after_context_or_report_read(self):
        """Wakes an ended ticket for a message attached to it that its worker read only for context or before its last report."""
        for read in ("context", "report"):
            h = Harness()
            ticket_id = accepted(h)["ticketId"]
            if read == "context":
                h.engine.follow_for_context(ticket_id, {"source": "chat", "id": "issue"})
            thread_id = "issue" if read == "context" else "t1"
            h.tick(1)
            h.engine.receive(trigger("2", threads=[{"source": "chat", "id": thread_id}]))
            h.settle()
            h.tick(1)
            h.engine.report(ticket_id, {"state": "progress", "summary": "read it"})
            h.engine.report(ticket_id, {"state": "dropped", "summary": "nothing to do"})
            if read == "report":
                h.engine.reevaluate("2")
            h.pump()
            h.tick(1)
            h.engine.attach(ticket_id, "2")
            h.settle()
            self.assertNotIn("ended", h.repo.tickets.get(ticket_id))
            self.assertEqual(followup_owners(h.repo.triggers.get("2"), h.repo), [ticket_id])

    def test_final_report_answer_counts_as_taken_up(self):
        """Counts a question the final report answers as taken up, so it does not come back as a candidate beside its answer."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.tick(1)
        h.engine.receive(trigger("2"))
        h.settle()
        h.engine.report(ticket_id, {"state": "ready", "summary": "It reads the config.", "answers": "2"})
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], [])
        disp = h.repo.triggers.disposition("2")
        self.assertEqual(disp["fanout"], [ticket_id])
        self.assertEqual(disp["acked"], [ticket_id])

    def test_attach_after_unavailable_eval_requeues_on_end(self):
        """Sends to Eval a message attached after its Eval was unavailable, once its ticket ends without taking it up."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.receive(trigger("2", threads=[{"source": "chat", "id": "x"}]))
        h.pump()
        h.decide(h.requests[-1], {"kind": "unavailable", "reason": "model down"})
        h.tick(1)
        h.engine.attach(ticket_id, "2")
        h.settle()
        h.engine.report(ticket_id, {"state": "dropped", "summary": "nothing to do"})
        self.assertEqual(h.repo.threads.get("chat:x")["pending"], ["2"])

    def test_queued_ack_without_disposition_counts_as_taken_up(self):
        """Counts an acknowledgement an earlier version queued without recording it on the message as taking the message up."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.report(ticket_id, {"state": "progress", "summary": "working"})
        h.tick(1)
        h.engine.receive(trigger("2"))
        h.settle()
        h.outbox.append(
            {
                "id": key("ack", "2"),
                "channel": trigger("2")["channel"],
                "text": "On it",
                "kind": "acknowledgement",
                "ticketId": ticket_id,
            }
        )
        h.engine.report(ticket_id, {"state": "dropped", "summary": "nothing to do"})
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], [])
        self.assertEqual(h.repo.triggers.disposition("2")["fanout"], [ticket_id])

    def test_resumed_message_taken_up_by_ending_report_not_later_one(self):
        """Counts a message that resumed a ticket as taken up by the report that ends it again, unlike one that came later."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.report(ticket_id, {"state": "ready", "summary": "done"})
        h.tick(1)
        h.engine.receive(trigger("2"))
        h.tick(1)
        h.engine.assignment_started(
            ticket_id, h.repo.tickets.get(ticket_id)["assignments"][-1]["id"]
        )
        h.tick(1)
        h.settle()
        h.read(ticket_id)
        h.tick(1)
        h.engine.receive(trigger("3"))
        h.settle()
        h.engine.report(ticket_id, {"state": "dropped", "summary": "nothing to do"})
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], ["3"])

    def test_refused_stop_sends_followup_to_eval_not_stop_message(self):
        """Sends a follow-up its ticket let go to Eval after a stop on it was refused, but not the one a stop ended it on."""
        for actor in (GUEST, MEMBER):
            h = Harness()
            ticket_id = accepted(h)["ticketId"]
            h.engine.receive(trigger("2", actor=actor))
            h.engine.decide("2", {"kind": "stop", "ticketId": ticket_id, "reason": "asked to stop"})
            if actor == GUEST:
                h.engine.report(ticket_id, {"state": "dropped", "summary": "nothing to do"})
            expected_pending = ["2"] if actor == GUEST else []
            self.assertEqual(h.repo.threads.get("chat:t1")["pending"], expected_pending)

    def test_stop_message_to_eval_when_other_ticket_ends(self):
        """Sends the message a stop ended a ticket on to Eval once another ticket it reached ends without taking it up."""
        h = Harness()
        stopped = accepted(h)["ticketId"]
        other = accepted(h, "5", "t5")["ticketId"]
        h.engine.bind_thread(other, {"source": "chat", "id": "t1"}, False)
        h.tick(1)
        h.engine.receive(trigger("2"))
        h.settle()
        h.engine.decide("2", {"kind": "stop", "ticketId": stopped, "reason": "asked to stop"})
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], [])
        h.engine.report(other, {"state": "dropped", "summary": "nothing to do"})
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], ["2"])

    def test_mark_unrelated_hands_followups_to_eval(self):
        """Hands the follow-ups on a thread a worker found unrelated back to Eval, with its reason."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        elsewhere = {"threads": [{"source": "chat", "id": "x"}]}
        h.engine.receive(trigger("2", **elsewhere))
        h.pump()
        h.engine.attach(ticket_id, "2")
        h.settle()
        h.engine.receive(trigger("3", **elsewhere))
        h.settle()
        with self.assertRaises(UserError):
            h.engine.decide("3", ACCEPT)
        h.engine.mark_unrelated(ticket_id, "chat:x", "about something else")
        self.assertEqual(h.repo.threads.get("chat:x")["pending"], ["2", "3"])
        handbacks = h.repo.triggers.disposition("3")["handbacks"]
        self.assertEqual(handbacks[0]["ticketId"], ticket_id)
        self.assertEqual(handbacks[0]["reason"], "about something else")
        h.pump()
        self.assertEqual([t["id"] for t in h.requests[-1]["triggers"]], ["2", "3"])

    def test_two_tickets_handback_keeps_with_non_handback_ticket(self):
        """Keeps a follow-up two tickets reached with the one that did not hand it back, and queues it once neither decides it."""
        h = Harness()
        first = accepted(h)["ticketId"]
        h.tick(1)
        h.engine.receive(trigger("2"))
        h.settle()
        h.engine.handback(first, "2", "another change")
        h.pump()
        second = h.decide(h.requests[-1], ACCEPT)["ticketId"]
        h.settle()
        h.tick(1)
        h.engine.receive(trigger("3"))
        h.settle()
        self.assertEqual(h.repo.triggers.disposition("3")["fanout"], [first, second])
        self.assertFalse(h.engine.handback(first, "3", "for the other ticket"))
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], [])
        with self.assertRaises(UserError):
            h.engine.decide("3", ACCEPT)
        with self.assertRaises(UserError):
            h.engine.decide("3", {"kind": "ignore", "reason": "chat"})
        self.assertTrue(h.engine.handback(second, "3", "neither ticket"))
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], ["3"])

    def test_legacy_eval_can_decide_followup(self):
        """Lets an Eval an earlier version started on a follow-up decide it."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.receive(trigger("2"))
        h.settle()
        thread = h.repo.threads.get("chat:t1")
        h.repo.threads.save(
            {
                **thread,
                "evaluating": {
                    "id": "e-legacy",
                    "threadKey": "chat:t1",
                    "triggerIds": ["2"],
                    "strength": "explicit",
                    "actor": MEMBER,
                    "basis": {"via": "listed"},
                    "policyVersion": "v1",
                    "startedAt": "2026-09-23T00:00:00Z",
                    "deadline": "2026-09-24T00:00:00Z",
                },
            }
        )
        self.assertEqual(followup_owners(trigger("2"), h.repo), [])
        decision = h.engine.decide(
            "2", {"kind": "ignore", "reason": "chat", "evalId": "e-legacy"}
        )
        self.assertEqual(decision["kind"], "ignore")
        self.assertEqual(decision["triggerIds"], ["2"])
        self.assertNotIn("ended", h.repo.tickets.get(ticket_id))

