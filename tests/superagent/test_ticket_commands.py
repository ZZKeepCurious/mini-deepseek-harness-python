import unittest

from superagent.core.errors import UserError
from superagent.core.model import running_assignment

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import trigger


class TicketCommandsTests(unittest.TestCase):
    def test_pr_close_and_ready_report_same_end_rule(self):
        """Ends a ticket by the same rule on a close and on a ready report: once no PR it opened is open, as the latest of them ended."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-5"}, False)
        h.engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-7"}, True)
        h.engine.thread_closed({"source": "gh", "id": "pr-7"}, "merged", "2026-09-23T00:00:00Z")
        self.assertEqual(h.repo.tickets.get(ticket_id)["ended"]["outcome"], "merged")
        h.tick(10)
        h.engine.receive(trigger("2", threads=[{"source": "gh", "id": "pr-5"}]))
        h.settle()
        self.assertNotIn("ended", h.repo.tickets.get(ticket_id))
        h.engine.report(ticket_id, {"state": "ready", "summary": "Answered"})
        self.assertEqual(h.repo.tickets.get(ticket_id)["ended"]["outcome"], "merged")
        h.engine.thread_closed({"source": "gh", "id": "pr-5"}, "closed", "2026-09-23T00:00:30Z")
        self.assertEqual(h.repo.tickets.get(ticket_id)["ended"]["outcome"], "merged")
        other = accepted(h, "3", "t3")["ticketId"]
        h.engine.bind_thread(other, {"source": "gh", "id": "pr-8"}, False)
        h.engine.report(other, {"state": "ready", "summary": "Pushed to their branch"})
        self.assertNotIn("ended", h.repo.tickets.get(other))
        h.engine.thread_closed({"source": "gh", "id": "pr-8"}, "closed", "2026-09-23T00:01:00Z")
        self.assertEqual(h.repo.tickets.get(other)["ended"]["outcome"], "closed")

    def test_unsubscribe_drops_follow(self):
        """Stops following a Thread whose Subscription a person removes."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-7"}, True)
        self.assertTrue(h.engine.unsubscribe("gh:pr-7"))
        self.assertNotIn("follows", h.repo.tickets.get(ticket_id))
        h.engine.report(ticket_id, {"state": "ready", "summary": "Done"})
        self.assertEqual(h.repo.tickets.get(ticket_id)["ended"]["outcome"], "completed")

    def test_follows_source_threads_ends_when_own_closed(self):
        """Follows a Source's Threads, and ends once no Thread of its own is open, unless resumed work has not finished."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]

        def close(id, outcome, at):
            open_ids = [t["id"] for t in h.repo.tickets.open()]
            h.engine.thread_closed({"source": "gh", "id": id}, outcome, at)
            return [tid for tid in open_ids if h.repo.tickets.get(tid).get("ended")]

        follow = h.engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-9"}, False)
        self.assertEqual(follow["follows"], [{"thread": "gh:pr-9", "own": False}])
        h.engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-10"}, True)
        self.assertEqual(h.repo.threads.get("gh:pr-9")["subscriptions"], [ticket_id])
        self.assertEqual(close("pr-9", "closed", "2026-09-23T00:00:00Z"), [])
        ticket = h.repo.tickets.get(ticket_id)
        self.assertEqual(
            ticket["follows"],
            [
                {"thread": "gh:pr-9", "own": False, "closed": {"at": "2026-09-23T00:00:00.000Z", "outcome": "closed"}},
                {"thread": "gh:pr-10", "own": True},
            ],
        )
        self.assertNotIn("ended", ticket)
        self.assertEqual(close("pr-10", "merged", "2026-09-23T00:00:01Z"), [ticket_id])
        ended = h.repo.tickets.get(ticket_id)
        self.assertEqual(ended["ended"]["by"], "executor")
        self.assertEqual(ended["ended"]["outcome"], "merged")
        self.assertEqual(ended["ended"]["at"], "2026-09-23T00:00:01.000Z")
        self.assertEqual(close("pr-10", "merged", "2026-09-23T00:00:01Z"), [])
        with self.assertRaises(UserError):
            h.engine.bind_thread(ticket_id, {"source": "gh", "id": "pr-11"}, True)
        other = accepted(h, "2", "t2")["ticketId"]
        h.engine.bind_thread(other, {"source": "gh", "id": "pr-12"}, True)
        h.engine.report(other, {"state": "failed", "summary": "tests fail"})
        h.tick(10)
        h.engine.retry_ticket(other)
        self.assertEqual(close("pr-12", "closed", "2026-09-23T00:00:05Z"), [])
        self.assertNotIn("ended", h.repo.tickets.get(other))
        h.engine.report(other, {"state": "ready", "summary": "done"})
        self.assertEqual(h.repo.tickets.get(other)["ended"]["outcome"], "closed")

    def test_retry_failed_and_request_status_waiting(self):
        """Retries a ticket that reported failed with a new Assignment, and asks only a waiting worker for its status."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        with self.assertRaises(UserError):
            h.engine.retry_ticket(ticket_id)
        self.assertFalse(h.engine.request_status(ticket_id))
        self.assertEqual(h.repo.tickets.get(ticket_id)["inputs"], [])
        h.engine.report(ticket_id, {"state": "waiting", "summary": "idle"})
        self.assertTrue(h.engine.request_status(ticket_id))
        status_inputs = h.repo.tickets.get(ticket_id)["inputs"]
        self.assertTrue(any(i.get("kind") == "text" and "status-only" in i.get("text", "") for i in status_inputs))
        h.engine.report(ticket_id, {"state": "failed", "summary": "broke"})
        retried = h.engine.retry_ticket(ticket_id)
        self.assertEqual(retried["assignments"][0]["state"], "ended")
        self.assertEqual(retried["assignments"][0]["endedReason"], "retried")
        self.assertEqual(retried["assignments"][1]["state"], "pending")
        self.assertEqual(h.executor.stop_count, 1)
        self.assertEqual(h.executor.launches[-1][0]["id"], ticket_id)
        self.assertEqual(h.executor.launches[-1][1], retried["assignments"][1])

