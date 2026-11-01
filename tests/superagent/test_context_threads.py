import unittest

from superagent.core.errors import UserError
from superagent.core.model import waits_for_person

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import GUEST, trigger


class ContextThreadsAndNotesTests(unittest.TestCase):
    def test_context_thread_fans_to_live_ticket_not_ended_unless_attached(self):
        """Fans a Thread followed for context out to the live ticket, resumes no ended ticket from it, unless it was attached."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        self.assertTrue(h.engine.follow_for_context(ticket_id, {"source": "chat", "id": "issue"}))
        self.assertFalse(h.engine.follow_for_context(ticket_id, {"source": "chat", "id": "issue"}))
        h.engine.receive(trigger("2", threads=[{"source": "chat", "id": "issue"}]))
        h.settle()
        trigger_ids = [
            inp["triggerId"]
            for inp in h.repo.tickets.get(ticket_id)["inputs"]
            if inp.get("kind") == "trigger"
        ]
        self.assertEqual(trigger_ids, ["2"])
        h.engine.report(ticket_id, {"state": "dropped", "summary": "nothing to do"})
        h.engine.receive(trigger("3", threads=[{"source": "chat", "id": "issue"}]))
        self.assertIn("ended", h.repo.tickets.get(ticket_id))
        h.engine.attach(ticket_id, "3")
        h.settle()
        self.assertEqual(h.repo.threads.get("chat:issue")["context"], [])
        h.engine.report(ticket_id, {"state": "dropped", "summary": "nothing to do"})
        self.assertIn("ended", h.repo.tickets.get(ticket_id))
        h.engine.receive(trigger("4", threads=[{"source": "chat", "id": "issue"}]))
        self.assertNotIn("ended", h.repo.tickets.get(ticket_id))

    def test_context_only_message_not_followup_does_not_lift_block(self):
        """Counts a message on a Thread a ticket follows only for context as none of its follow-ups, and lifts no block with it."""
        h = Harness()
        owner = accepted(h)["ticketId"]
        follower = accepted(h, "2", "other")["ticketId"]
        h.engine.follow_for_context(follower, {"source": "chat", "id": "t1"})
        h.engine.receive(trigger("3", actor=GUEST))
        h.pump()
        disp = h.repo.triggers.disposition("3")
        self.assertEqual(disp["fanout"], [owner, follower])
        self.assertEqual(disp["context"], [follower])
        last_inp = h.repo.tickets.get(follower)["inputs"][-1]
        self.assertEqual(last_inp["triggerId"], "3")
        self.assertTrue(last_inp.get("context"))
        h.engine.stop_ticket(owner, "done")
        h.engine.report(follower, {"state": "blocked", "summary": "needs a decision"})
        h.engine.receive(trigger("4"))
        h.pump()
        self.assertEqual([t["id"] for t in h.requests[-1]["triggers"]], ["3", "4"])
        self.assertTrue(waits_for_person(h.repo.tickets.get(follower)))

    def test_eval_when_ticket_cannot_resume_only_context_follower_left(self):
        """Sends a message to Eval when the ticket it reached cannot resume and only a ticket following its Thread for context is left."""
        h = Harness()
        owner = accepted(h)["ticketId"]
        follower = accepted(h, "2", "other")["ticketId"]
        h.engine.follow_for_context(follower, {"source": "chat", "id": "t1"})
        h.engine.report(owner, {"state": "ready", "summary": "done"})
        h.set_version("revoked")
        h.engine.receive(trigger("3"))
        h.set_version("v1")
        h.pump()
        disp = h.repo.triggers.disposition("3")
        self.assertEqual(disp["fanout"], [follower])
        self.assertEqual(disp["context"], [follower])
        self.assertEqual([t["id"] for t in h.requests[-1]["triggers"]], ["3"])

    def test_note_as_input_held_like_automatic_refused_when_ended(self):
        """Gives an open ticket's worker a note as an Input of its own, held like automatic Input, and refuses one for an ended ticket."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        note_inp = h.engine.note(ticket_id, "The other ticket renamed the API")["inputs"][-1]
        self.assertEqual(note_inp["kind"], "note")
        self.assertEqual(note_inp["text"], "The other ticket renamed the API")
        h.settle()
        h.engine.report(ticket_id, {"state": "blocked", "summary": "needs a decision"})
        note = h.engine.note(ticket_id, "Ticket B changed the schema")["inputs"][-1]
        h.settle()
        delivered_ids = [pair[1] for pair in h.delivered]
        self.assertNotIn(note["id"], delivered_ids)
        self.assertIn(note, h.repo.tickets.get(ticket_id)["inputs"])
        h.engine.stop_ticket(ticket_id, "done")
        with self.assertRaises(UserError) as ctx:
            h.engine.note(ticket_id, "Too late")
        self.assertEqual(ctx.exception.code, "ticketEnded")

