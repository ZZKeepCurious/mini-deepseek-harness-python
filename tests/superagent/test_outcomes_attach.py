import unittest

from superagent.core.errors import UserError
from superagent.core.model import followup_owners, ticket_outcome

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import GUEST, trigger


class OutcomesTests(unittest.TestCase):
    def test_records_how_ticket_ended(self):
        """Records how a ticket ended."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.stop_ticket(ticket_id, "Human asked to stop")
        ended = h.repo.tickets.get(ticket_id)["ended"]
        self.assertEqual(ended["by"], "human")
        self.assertEqual(ended["outcome"], "stopped")
        self.assertEqual(ended["reason"], "Human asked to stop")
        self.assertEqual(ticket_outcome(h.repo.tickets.get(ticket_id)), "stopped")
        self.assertIsNone(ticket_outcome({}))


class AttachTests(unittest.TestCase):
    def test_attach_routing_note_ends_eval_refuses_stopped_revoked(self):
        """Sends the routing note for a message the worker has, ends an Eval that holds only that message, and refuses a ticket a person stopped or the Policy no longer holds."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        h.engine.receive(trigger("2", actor=GUEST))
        h.pump()
        from superagent.core.ids import key

        delivered_keys = [pair[1] for pair in h.delivered]
        self.assertEqual(delivered_keys, [key("input", ticket_id, "trigger", "2")])
        self.assertIsNone(h.engine.candidate("2"))
        with self.assertRaises(UserError):
            h.engine.attach(ticket_id, "2")
        self.assertTrue(h.engine.reevaluate("2"))
        h.pump()
        self.assertIsNotNone(h.engine.candidate("2"))
        attached = h.engine.attach(ticket_id, "2", "The guest asks for the same fix")
        h.settle()
        trigger_inputs = [i for i in attached["inputs"] if i.get("kind") == "trigger"]
        self.assertEqual(len(trigger_inputs), 2)
        self.assertEqual(trigger_inputs[0]["triggerId"], "2")
        self.assertEqual(trigger_inputs[1]["triggerId"], "2")
        self.assertEqual(trigger_inputs[1].get("note"), "The guest asks for the same fix")
        self.assertEqual([p[1] for p in h.delivered], [i["id"] for i in attached["inputs"]])
        self.assertIsNone(h.engine.candidate("2"))
        last_dec = h.repo.decisions.byThread("chat:t1")[-1]
        self.assertEqual(last_dec["kind"], "ignore")
        self.assertEqual(
            last_dec["reason"],
            [{"code": "routed", "values": {"ticket": ticket_id}}],
        )
        h.engine.receive(trigger("5", actor=GUEST))
        h.engine.reevaluate("5")
        note_inp = h.engine.attach(ticket_id, "5", "Same fix again")["inputs"][-1]
        self.assertEqual(note_inp["triggerId"], "5")
        self.assertEqual(note_inp.get("note"), "Same fix again")
        h.settle()
        h.engine.report(ticket_id, {"state": "ready", "summary": "done"})
        h.set_version("revoked")
        with self.assertRaises(UserError) as ctx:
            h.engine.attach(ticket_id, "2")
        self.assertEqual(ctx.exception.code, "mandateRevoked")
        h.set_version("v1")
        stopped = accepted(h, "3", "t3")["ticketId"]
        h.engine.stop_ticket(stopped)
        with self.assertRaises(UserError) as ctx:
            h.engine.attach(stopped, "2")
        self.assertEqual(ctx.exception.code, "ticketStopped")
        with self.assertRaises(UserError) as ctx:
            h.engine.attach("missing", "2")
        self.assertEqual(ctx.exception.code, "unknownTicket")

    def test_attach_ends_batch_eval_reassesses_rest(self):
        """Ends the batch Eval of a message attached to a ticket and assesses the rest again, leaving the message to that ticket."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        other = accepted(h, "5", "t5")["ticketId"]
        elsewhere = {"threads": [{"source": "chat", "id": "x"}]}
        h.engine.receive(trigger("2", **elsewhere))
        h.engine.receive(trigger("3", **elsewhere))
        h.pump()
        h.engine.attach(ticket_id, "3")
        h.pump()
        decisions = h.repo.decisions.byThread("chat:x")
        self.assertEqual(decisions[-1]["kind"], "unavailable")
        self.assertEqual(decisions[-1]["triggerIds"], ["2", "3"])
        cand = h.engine.candidate("2")
        self.assertEqual([t["id"] for t in cand["triggers"]], ["2"])
        self.assertEqual(followup_owners(h.repo.triggers.get("3"), h.repo), [ticket_id])
        with self.assertRaises(UserError):
            h.engine.attach(other, "3")

    def test_user_error_missing_and_not_admissible_metadata(self):
        """Names missing things apart from refusals and fills in the values."""
        self.assertTrue(UserError("unknownTicket").missing)
        self.assertFalse(UserError("ticketEnded").missing)
        err = UserError("notAdmissible", {"reason": "not_a_member"})
        self.assertEqual(err.code, "notAdmissible")
        self.assertIn("not_a_member", str(err))

