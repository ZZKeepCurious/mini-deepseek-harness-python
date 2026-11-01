import unittest

from superagent.core.errors import UserError
from superagent.core.model import running_assignment

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import GUEST, trigger


def resumed():
    h = Harness()
    ticket_id = accepted(h)["ticketId"]
    h.engine.report(ticket_id, {"state": "ready", "summary": "done"})
    h.tick(1)
    h.engine.receive(trigger("2"))
    assignment_id = h.repo.tickets.get(ticket_id)["assignments"][-1]["id"]
    h.tick(1)
    h.engine.assignment_started(ticket_id, assignment_id)
    undelivered = h.repo.tickets.get(ticket_id)["undelivered"]
    note, message = undelivered[0], undelivered[1]
    return {
        "h": h,
        "ticket_id": ticket_id,
        "assignment_id": assignment_id,
        "note": note,
        "message": message,
        "pending": lambda: h.repo.threads.get("chat:t1")["pending"],
    }


class ModelSurfaceReviewTests(unittest.TestCase):
    def test_resume_only_on_person_not_bot_attach(self):
        """Resumes an ended ticket only on a person's message, not on a bot's that is attached to it."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.report(ticket_id, {"state": "ready", "summary": "Done"})
        h.tick(1)
        h.engine.receive(
            trigger(
                "2",
                human=False,
                actor={"source": "chat", "id": "ci"},
                threads=[{"source": "chat", "id": "ci"}],
            )
        )
        h.engine.attach(ticket_id, "2")
        self.assertIn("ended", h.repo.tickets.get(ticket_id))
        h.tick(1)
        h.engine.receive(trigger("3"))
        self.assertNotIn("ended", h.repo.tickets.get(ticket_id))


class FollowUpReviewTurnEndsTests(unittest.TestCase):
    def merge(self, h):
        h.engine.thread_closed({"source": "gh", "id": "pr-9"}, "merged", "2026-09-23T00:00:01Z")

    def test_merge_sends_unread_after_ready_to_eval(self):
        """Lets a merge send to Eval only the messages its worker had no turn to read after its ready report."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-9"}, True)
        h.engine.report(ticket_id, {"state": "ready", "summary": "PR ready"})
        h.tick(1)
        h.engine.receive(trigger("2"))
        h.settle()
        h.read(ticket_id)
        h.tick(1)
        h.engine.turn_ended(ticket_id, running_assignment(h.repo.tickets.get(ticket_id))["id"])
        h.tick(1)
        h.engine.receive(trigger("3"))
        h.settle()
        self.merge(h)
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], ["3"])

    def test_merge_keeps_open_while_working_on_message(self):
        """Keeps a ticket open at a merge while its worker works on a message it took up, also after a progress report."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-9"}, True)
        h.engine.report(ticket_id, {"state": "ready", "summary": "PR ready"})
        h.tick(1)
        h.engine.receive(trigger("2"))
        h.settle()
        h.engine.reply(
            {"purpose": "acknowledgement", "ticketId": ticket_id, "triggerId": "2", "text": "On it"}
        )
        h.tick(1)
        h.engine.report(ticket_id, {"state": "progress", "summary": "Answered the other question"})
        self.merge(h)
        self.assertNotIn("ended", h.repo.tickets.get(ticket_id))

    def test_deferred_close_ends_on_drop(self):
        """Ends a ticket whose close it deferred as the close did when its worker then drops the work."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-9"}, True)
        h.engine.report(ticket_id, {"state": "ready", "summary": "PR ready"})
        h.tick(1)
        h.engine.receive(trigger("2"))
        h.settle()
        h.engine.reply(
            {"purpose": "acknowledgement", "ticketId": ticket_id, "triggerId": "2", "text": "On it"}
        )
        self.merge(h)
        h.engine.report(ticket_id, {"state": "dropped", "summary": "Nothing more to do"})
        self.assertEqual(h.repo.tickets.get(ticket_id)["ended"]["outcome"], "merged")

    def test_handback_to_eval_when_stop_ends_keeper(self):
        """Sends a message another ticket handed back to Eval when a stop on it ends the ticket that kept it."""
        h = Harness()
        first = accepted(h)["ticketId"]
        second = accepted(h, "2", "t2")["ticketId"]
        h.engine.bind_thread(second, {"source": "chat", "id": "t1"}, False)
        h.engine.receive(trigger("3"))
        h.settle()
        self.assertFalse(h.engine.handback(second, "3", "It is about the first fix"))
        h.engine.decide("3", {"kind": "stop", "ticketId": first, "reason": "asked to stop"})
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], ["3"])

    def test_live_ticket_gets_reattached_message_again(self):
        """Gives a live ticket's worker again a message it had that a person sent to Eval and the main Session attached back."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.tick(1)
        h.engine.receive(trigger("2"))
        h.settle()
        h.tick(1)
        h.engine.report(ticket_id, {"state": "progress", "summary": "working"})
        h.engine.reevaluate("2")
        h.pump()
        h.engine.attach(ticket_id, "2")
        triggers = [
            i
            for i in h.repo.tickets.get(ticket_id)["inputs"]
            if i.get("kind") == "trigger" and i.get("triggerId") == "2"
        ]
        self.assertEqual(len(triggers), 2)


class FollowUpReviewTurnEndsDeliveriesTests(unittest.TestCase):
    def test_stop_sends_undelivered_after_turn_end(self):
        """Lets a stop send to Eval a message that waited for delivery when a turn ended, and reached the worker after."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.report(ticket_id, {"state": "progress", "summary": "working"})
        h.tick(1)
        h.engine.receive(trigger("2"))

        def ticket():
            return h.repo.tickets.get(ticket_id)

        assignment_id = running_assignment(ticket())["id"]
        input_id = ticket()["undelivered"][0]
        h.tick(1)
        h.engine.turn_ended(ticket_id, assignment_id)
        h.tick(1)
        h.engine.inputs_delivered(ticket_id, assignment_id, [input_id])
        h.tick(1)
        h.engine.stop_ticket(ticket_id, "not needed")
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], ["2"])


class FollowUpReviewDeliveryTimesTests(unittest.TestCase):
    def test_stop_sends_message_after_waiting_report(self):
        """Lets a stop send to Eval a message that came before the worker's waiting report but reached the worker after it."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.tick(1)
        h.engine.receive(trigger("2"))

        def ticket():
            return h.repo.tickets.get(ticket_id)

        assignment_id = running_assignment(ticket())["id"]
        input_id = ticket()["undelivered"][0]
        h.tick(1)
        h.engine.report(ticket_id, {"state": "waiting", "summary": "Waiting for input"})
        h.tick(1)
        h.engine.inputs_delivered(ticket_id, assignment_id, [input_id])
        h.tick(1)
        h.engine.stop_ticket(ticket_id, "not needed")
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], ["2"])


class RoutingReviewTests(unittest.TestCase):
    def test_two_stops_same_moment_two_decisions(self):
        """Records two stops of one follow-up that reached two tickets, made in the same moment, as two Decisions."""
        h = Harness()
        first = accepted(h)["ticketId"]
        second = accepted(h, "2", "t2")["ticketId"]
        h.engine.bind_thread(second, {"source": "chat", "id": "t1"}, False)
        h.engine.receive(trigger("3"))
        h.settle()
        stops = [
            h.engine.decide("3", {"kind": "stop", "ticketId": tid, "reason": "asked to stop"})
            for tid in (first, second)
        ]
        self.assertEqual(len({s["id"] for s in stops}), 2)
        self.assertEqual(
            [h.repo.decisions.get(s["id"])["ticketId"] for s in stops],
            [first, second],
        )
        outcomes = [h.repo.tickets.get(tid)["ended"]["outcome"] for tid in (first, second)]
        self.assertEqual(outcomes, ["stopped", "stopped"])


class WhatAWorkerReadTests(unittest.TestCase):
    def test_progress_then_drop_sends_wakeup_message_to_eval(self):
        """Sends to Eval a message that woke a ticket and reached its worker only after a progress report, at a dropped report."""
        ctx = resumed()
        h, ticket_id, assignment_id, note, message = (
            ctx["h"],
            ctx["ticket_id"],
            ctx["assignment_id"],
            ctx["note"],
            ctx["message"],
        )
        pending = ctx["pending"]
        h.engine.inputs_delivered(ticket_id, assignment_id, [note])
        h.engine.delivery_failed(ticket_id, assignment_id, "Session busy")
        h.tick(1)
        h.engine.report(ticket_id, {"state": "progress", "summary": "Looking"})
        h.tick(1)
        h.engine.inputs_delivered(ticket_id, assignment_id, [message])
        h.tick(1)
        h.engine.report(ticket_id, {"state": "dropped", "summary": "Nothing to do"})
        self.assertEqual(pending(), ["2"])

    def test_read_in_step_counts_as_read_on_drop(self):
        """Counts a message that woke a ticket as read by a later dropped report once its worker's Session took it into a step."""
        ctx = resumed()
        h, ticket_id, assignment_id, note, message = (
            ctx["h"],
            ctx["ticket_id"],
            ctx["assignment_id"],
            ctx["note"],
            ctx["message"],
        )
        pending = ctx["pending"]
        h.engine.inputs_read(ticket_id, [message])
        h.engine.inputs_delivered(ticket_id, assignment_id, [note, message])
        h.tick(1)
        h.engine.report(ticket_id, {"state": "progress", "summary": "Looking"})
        h.tick(1)
        h.engine.report(ticket_id, {"state": "dropped", "summary": "Nothing to do"})
        self.assertEqual(pending(), [])

    def test_merge_sends_message_after_turn_end_no_step(self):
        """Sends to Eval at a merge a message that reached the worker after its last step, which no step took before its turn ended."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-9"}, True)
        h.engine.report(ticket_id, {"state": "ready", "summary": "PR ready"})
        h.tick(1)
        h.engine.receive(trigger("2"))
        h.settle()
        h.tick(1)
        h.engine.turn_ended(ticket_id, running_assignment(h.repo.tickets.get(ticket_id))["id"])
        h.engine.thread_closed({"source": "gh", "id": "pr-9"}, "merged", "2026-09-23T00:00:01Z")
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], ["2"])

    def test_retry_delivery_after_turn_end_read_on_drop(self):
        """Counts a message a retry delivered after a failure and a turn end as read by a dropped report only once a step took it."""
        for taken in (False, True):
            ctx = resumed()
            h, ticket_id, assignment_id, note, message = (
                ctx["h"],
                ctx["ticket_id"],
                ctx["assignment_id"],
                ctx["note"],
                ctx["message"],
            )
            pending = ctx["pending"]
            h.engine.inputs_delivered(ticket_id, assignment_id, [note])
            h.engine.delivery_failed(ticket_id, assignment_id, "Session busy")
            h.tick(1)
            h.engine.turn_ended(ticket_id, assignment_id)
            h.tick(1)
            h.engine.inputs_delivered(ticket_id, assignment_id, [message])
            if taken:
                h.read(ticket_id)
            h.tick(1)
            h.engine.report(ticket_id, {"state": "dropped", "summary": "Nothing to do"})
            self.assertEqual(pending(), [] if taken else ["2"])

    def test_interrupt_read_only_after_report_on_stop(self):
        """Counts a message a step took before a restart cut its turn off as read only once the restarted worker reports."""
        for reported in (False, True):
            h = Harness()
            ticket_id = accepted(h)["ticketId"]
            h.engine.report(ticket_id, {"state": "progress", "summary": "working"})
            h.tick(1)
            h.engine.receive(trigger("2"))
            h.settle()
            h.read(ticket_id)
            h.tick(1)
            h.engine.assignment_interrupted(
                ticket_id, running_assignment(h.repo.tickets.get(ticket_id))["id"]
            )
            h.settle()
            if reported:
                h.tick(1)
                h.engine.report(ticket_id, {"state": "progress", "summary": "Recovered"})
            h.tick(1)
            h.engine.stop_ticket(ticket_id, "not needed")
            self.assertEqual(h.repo.threads.get("chat:t1")["pending"], [] if reported else ["2"])


class ReadClaimsReviewTests(unittest.TestCase):
    def test_retry_gets_undelivered_not_read(self):
        """Gives a retry's Assignment the messages the failed one had delivered but no step of its worker took."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.tick(1)
        h.engine.receive(trigger("2"))
        h.settle()

        def ticket():
            return h.repo.tickets.get(ticket_id)

        input_id = next(
            i["id"] for i in ticket()["inputs"] if i.get("kind") == "trigger" and i.get("triggerId") == "2"
        )
        self.assertEqual(ticket()["undelivered"], [])
        h.tick(1)
        h.engine.report(ticket_id, {"state": "failed", "summary": "The turn broke."})
        h.tick(1)
        h.engine.retry_ticket(ticket_id)
        self.assertEqual(ticket()["undelivered"], [input_id])


class ReadClaimsReviewRetriesTests(unittest.TestCase):
    def test_retry_gets_only_current_assignment_delivered(self):
        """Gives a retry only what the Assignment it replaces had delivered and no step took, not what an earlier end let go of."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]

        def ticket():
            return h.repo.tickets.get(ticket_id)

        def input_of(trigger_id):
            return next(
                i["id"]
                for i in ticket()["inputs"]
                if i.get("kind") == "trigger" and i.get("triggerId") == trigger_id
            )

        h.tick(1)
        h.engine.receive(trigger("2"))
        h.settle()
        h.tick(1)
        h.engine.report(ticket_id, {"state": "dropped", "summary": "nothing to do"})
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], ["2"])
        h.tick(1)
        h.engine.receive(trigger("3"))
        h.settle()
        h.tick(1)
        h.engine.report(ticket_id, {"state": "blocked", "summary": "Which page?"})
        h.tick(1)
        h.engine.retry_ticket(ticket_id)
        self.assertEqual(ticket()["undelivered"], [input_of("3")])


class ReadClaimsReviewRestartsTests(unittest.TestCase):
    def test_restart_and_retry_get_undelivered_unread(self):
        """Gives a restart's Assignment what the replaced one had delivered and no step took, and a retry of it gets it again."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]

        def ticket():
            return h.repo.tickets.get(ticket_id)

        def input_of(trigger_id):
            return next(
                i["id"]
                for i in ticket()["inputs"]
                if i.get("kind") == "trigger" and i.get("triggerId") == trigger_id
            )

        h.tick(1)
        h.engine.receive(trigger("2"))
        h.settle()
        h.tick(1)
        h.engine.assignment_interrupted(ticket_id, running_assignment(ticket())["id"])
        self.assertEqual(ticket()["undelivered"][0], input_of("2"))
        h.settle()
        h.tick(1)
        h.engine.report(ticket_id, {"state": "blocked", "summary": "Which page?"})
        h.tick(1)
        h.engine.retry_ticket(ticket_id)
        self.assertEqual(ticket()["undelivered"], [input_of("2")])

