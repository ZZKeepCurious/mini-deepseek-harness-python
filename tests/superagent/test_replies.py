import unittest

from superagent.core.errors import UserError
from superagent.core.ids import key

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import GUEST, trigger


class RepliesTests(unittest.TestCase):
    def test_outbox_ack_reused_via_reply(self):
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        reply = h.engine.reply(
            {"purpose": "acknowledgement", "ticketId": ticket_id, "triggerId": "1", "text": "On it"}
        )
        self.assertEqual(reply, {"id": key("ack", "1"), "status": "pending", "reused": True})
        self.assertEqual([m["text"] for m in h.outbox], ["ok"])

    def test_acknowledgement_once_refuses_others(self):
        """Acknowledges a person's message that reached an open ticket once, and refuses others."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        reply = h.engine.reply(
            {"purpose": "acknowledgement", "ticketId": ticket_id, "triggerId": "1", "text": "On it"}
        )
        self.assertEqual(reply["reused"], True)
        self.assertEqual([m["text"] for m in h.outbox], ["ok"])
        h.engine.receive(trigger("2", actor=GUEST))
        reply2 = h.engine.reply(
            {"purpose": "acknowledgement", "ticketId": ticket_id, "triggerId": "2", "text": "Looking"}
        )
        self.assertEqual(reply2, {"id": key("ack", "2"), "status": "pending", "reused": False})
        h.engine.receive(trigger("3", human=False))
        with self.assertRaises(UserError) as ctx:
            h.engine.reply(
                {"purpose": "acknowledgement", "ticketId": ticket_id, "triggerId": "3", "text": "x"}
            )
        self.assertEqual(ctx.exception.code, "ackNotPerson")
        h.engine.receive(trigger("6", threads=[{"source": "chat", "id": "elsewhere"}]))
        with self.assertRaises(UserError) as ctx:
            h.engine.reply(
                {"purpose": "acknowledgement", "ticketId": ticket_id, "triggerId": "6", "text": "x"}
            )
        self.assertEqual(ctx.exception.code, "notReached")
        h.engine.stop_ticket(ticket_id)
        with self.assertRaises(UserError) as ctx:
            h.engine.reply(
                {"purpose": "acknowledgement", "ticketId": ticket_id, "triggerId": "2", "text": "x"}
            )
        self.assertEqual(ctx.exception.code, "ackEnded")

    def test_answer_result_attention_rules(self):
        """Sends an answer only after the worker reported it, a result only for a ready ticket, and attention only for its current question."""
        h = Harness()
        ticket_id = accepted(h)["ticketId"]
        asked = trigger(
            "2",
            actor=GUEST,
            channel={"source": "chat", "thread": "t1", "address": {"message": "2"}},
        )
        h.engine.receive(asked)
        with self.assertRaises(UserError) as ctx:
            h.engine.reply({"purpose": "answer", "triggerId": "2", "text": "Yes"})
        self.assertEqual(ctx.exception.code, "noAnswer")
        h.engine.report(ticket_id, {"state": "progress", "summary": "Yes", "answers": "2"})
        ans = h.engine.reply({"purpose": "answer", "triggerId": "2", "text": "Yes"})
        self.assertEqual(ans["status"], "pending")
        self.assertFalse(ans["reused"])
        self.assertEqual(h.outbox[-1]["channel"], asked["channel"])
        self.assertEqual(h.outbox[-1]["text"], "Yes")
        self.assertEqual(h.outbox[-1]["kind"], "reply")
        self.assertEqual(h.repo.tickets.get(ticket_id)["answers"], [])
        with self.assertRaises(UserError) as ctx:
            h.engine.reply({"purpose": "result", "ticketId": ticket_id, "text": "Done"})
        self.assertEqual(ctx.exception.code, "notReady")
        h.engine.report(
            ticket_id,
            {"state": "progress", "summary": "asking", "question": "Which color?", "askedBy": "2"},
        )
        with self.assertRaises(UserError) as ctx:
            h.engine.reply(
                {"purpose": "attention", "ticketId": ticket_id, "question": "Which size?", "text": "x"}
            )
        self.assertEqual(ctx.exception.code, "notAttention")
        quiet = accepted(h, "9", "t9")["ticketId"]
        with self.assertRaises(UserError):
            h.engine.reply({"purpose": "attention", "ticketId": quiet, "text": "Anything to send."})
        with self.assertRaises(UserError):
            h.engine.reply({"purpose": "attention", "ticketId": quiet, "question": "", "text": "Anything to send."})
        attention = h.repo.tickets.get(ticket_id)["attention"]
        att_reply = h.engine.reply(
            {"purpose": "attention", "ticketId": ticket_id, "question": "Which color?", "text": "Which color?"}
        )
        self.assertEqual(att_reply["id"], key("attention-reply", attention["id"]))
        self.assertEqual(h.outbox[-1]["channel"], asked["channel"])
        h.engine.report(ticket_id, {"state": "ready", "summary": "Done"})
        result = h.engine.reply({"purpose": "result", "ticketId": ticket_id, "text": "Done"})
        self.assertEqual(result["status"], "pending")
        self.assertEqual(h.outbox[-1]["channel"], trigger("1")["channel"])

