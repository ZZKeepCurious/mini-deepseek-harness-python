import unittest

from superagent.core.errors import UserError
from superagent.core.ids import key

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import trigger


class WorkerReportsTests(unittest.TestCase):
    def test_question_attention_ask_keep_replace_clear_withdraw(self):
        """Asks, keeps, replaces and clears the question for a person, and says which question a report replaced."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        asked = h.engine.report(
            ticket_id,
            {"state": "progress", "summary": "need a call", "question": "Which color?", "askedBy": "1"},
        )
        self.assertEqual(asked["attention"]["question"], "Which color?")
        self.assertEqual(asked["attention"]["triggerId"], "1")
        blocked = h.engine.report(ticket_id, {"state": "blocked", "summary": "still waiting"})
        self.assertEqual(blocked["attention"], asked["attention"])
        self.assertEqual(h.withdrawn, [])
        replaced = h.engine.report(
            ticket_id, {"state": "progress", "summary": "other question", "question": "Which size?"}
        )
        self.assertEqual(replaced["attention"]["question"], "Which size?")
        self.assertNotIn("triggerId", replaced["attention"])
        self.assertEqual(h.withdrawn, [key("attention-reply", asked["attention"]["id"])])
        cleared = h.engine.report(
            ticket_id, {"state": "progress", "summary": "answered", "question": None}
        )
        self.assertNotIn("attention", cleared)
        self.assertEqual(
            h.withdrawn,
            [
                key("attention-reply", asked["attention"]["id"]),
                key("attention-reply", replaced["attention"]["id"]),
            ],
        )

    def test_final_report_ends_ticket_by_state_and_follows(self):
        """Ends the ticket on a final report: dropped drops it, ready completes it unless it follows a Source's Thread that is open."""

        def outcome(state, follows, closed=False):
            h = Harness()
            ticket_id = accepted(h)["ticketId"]
            h.engine.report(ticket_id, {"state": "progress", "summary": "asking", "question": "Merge it?"})
            if follows:
                h.engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-9"}, False)
                h.engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-10"}, True)
            if closed:
                h.engine.thread_closed({"source": "gh", "id": "pr-9"}, "closed", "2026-09-22T00:00:00Z")
                h.engine.thread_closed({"source": "gh", "id": "pr-10"}, "merged", "2026-09-22T00:00:01Z")
            h.engine.report(ticket_id, {"state": state, "summary": "done"})
            return h.repo.tickets.get(ticket_id)

        dropped_follows = outcome("dropped", True)
        self.assertEqual(dropped_follows["ended"]["by"], "executor")
        self.assertEqual(dropped_follows["ended"]["outcome"], "dropped")
        self.assertNotIn("reason", dropped_follows["ended"])
        ready_no_follow = outcome("ready", False)
        self.assertEqual(ready_no_follow["ended"]["outcome"], "completed")
        self.assertNotIn("attention", ready_no_follow)
        ready_follow = outcome("ready", True)
        self.assertEqual(
            ready_follow["follows"],
            [{"thread": "gh:pr-9", "own": False}, {"thread": "gh:pr-10", "own": True}],
        )
        self.assertNotIn("ended", ready_follow)
        self.assertNotIn("attention", ready_follow)
        ready_closed = outcome("ready", True, closed=True)
        self.assertEqual(ready_closed["ended"]["outcome"], "merged")
        failed = outcome("failed", False)
        self.assertEqual(failed["attention"]["question"], "Merge it?")
        self.assertNotIn("ended", failed)

    def test_refuses_invalid_questions_and_answers(self):
        """Refuses a question with a result or an answer, and questions or answers that name no person's message of the ticket."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.receive(trigger("2", human=False, threads=[{"source": "chat", "id": "t1"}]))
        h.engine.receive(trigger("4", threads=[{"source": "chat", "id": "elsewhere"}]))

        def report(extra):
            return lambda: h.engine.report(ticket_id, {"state": "progress", "summary": "x", **extra})

        with self.assertRaises(UserError) as ctx:
            report({"state": "ready", "question": "Q?"})()
        self.assertEqual(ctx.exception.code, "questionWithResult")
        with self.assertRaises(UserError):
            report({"question": "Q?", "answers": "1"})()
        with self.assertRaises(UserError):
            report({"askedBy": "1"})()
        with self.assertRaises(UserError):
            report({"question": "Q?", "askedBy": "2"})()
        with self.assertRaises(UserError):
            report({"answers": "4"})()
        result = report({"answers": "1"})()
        self.assertEqual(result["answers"], ["1"])

    def test_refuses_report_on_ended_ticket(self):
        """Refuses a report on an ended ticket."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.stop_ticket(ticket_id)
        with self.assertRaises(UserError) as ctx:
            h.engine.report(ticket_id, {"state": "progress", "summary": "x"})
        self.assertEqual(ctx.exception.code, "ticketEnded")

