from __future__ import annotations

from superagent.core.errors import UserError
from superagent.core.ids import key
from superagent.core.model import latest_assignment, live_ticket, ticket_outcome
from superagent.core.part import EnginePart


class Replies(EnginePart):
    def reply(self, request: dict) -> dict:
        if request["purpose"] == "acknowledgement":
            return self._acknowledge(request)
        return self._answer(request)

    def acknowledge_accepted(self, ticket_id: str, trigger: dict, text: str) -> None:
        if trigger.get("channel") and trigger.get("human"):
            self.options.outbox.send(
                {
                    "id": key("ack", trigger["id"]),
                    "channel": trigger["channel"],
                    "text": text,
                    "kind": "acknowledgement",
                    "ticketId": ticket_id,
                }
            )

    def withdraw_attention(self, attention_id: str) -> None:
        self.options.outbox.withdraw(key("attention-reply", attention_id))

    def _acknowledge(self, request: dict) -> dict:
        ticket = self.require(request["ticketId"])
        if not live_ticket(ticket):
            raise UserError("ackEnded")
        trigger = self.repo.triggers.get(request["triggerId"])
        if not trigger:
            raise UserError("unknownTrigger")
        if not self.reaches(ticket, trigger, False):
            raise UserError("notReached")
        if not trigger.get("channel"):
            raise UserError("ackNoChannel")
        msg_id = key("ack", trigger["id"])
        existing = self.options.outbox.status(msg_id)
        if not existing and not trigger.get("human"):
            raise UserError("ackNotPerson")
        if not existing:
            self.options.outbox.send(
                {
                    "id": msg_id,
                    "channel": trigger["channel"],
                    "text": request["text"],
                    "kind": "acknowledgement",
                    "ticketId": ticket["id"],
                }
            )
        self.take_up(ticket["id"], trigger["id"])
        return {"id": msg_id, "status": self.options.outbox.status(msg_id) or "pending", "reused": existing is not None}

    def _answer(self, request: dict) -> dict:
        trigger = self.repo.triggers.get(request["triggerId"]) if request.get("triggerId") else None
        if request.get("triggerId") and not trigger:
            raise UserError("unknownTrigger")
        ticket = None
        if request.get("ticketId"):
            ticket = self.require(request["ticketId"])
        elif trigger:
            for item in self.repo.tickets.list():
                if self.reaches(item, trigger):
                    ticket = item
                    break
        if request["purpose"] == "result":
            outcome = ticket_outcome(ticket) if ticket else None
            reported = (
                bool(ticket)
                and live_ticket(ticket)
                and (latest_assignment(ticket) or {}).get("report", {}).get("state") == "ready"
            )
            ready = outcome in ("merged", "completed", "dropped") or reported
            if not ticket or not ready:
                raise UserError("notReady")
        attention = ticket.get("attention") if request["purpose"] == "attention" and ticket else None
        if request["purpose"] == "attention" and (not attention or attention.get("question") != request.get("question")):
            raise UserError("notAttention")
        if request["purpose"] == "answer" and not trigger.get("human"):
            raise UserError("notQuestion")
        msg_id = (
            key("attention-reply", attention["id"])
            if attention
            else key("reply", trigger["id"] if trigger else ticket["id"], request["text"])
        )
        existing = self.options.outbox.status(msg_id)
        ready_answer = not ticket or not trigger or trigger["id"] in (ticket.get("answers") or [])
        if request["purpose"] == "answer" and not ready_answer and not existing:
            raise UserError("noAnswer")
        asker = self.repo.triggers.get(attention["triggerId"]) if attention and attention.get("triggerId") else None
        if request["purpose"] == "answer":
            channel = trigger["channel"]
        elif asker:
            channel = asker.get("channel")
        else:
            channel = self._source_channel(ticket) if ticket else None
        if not channel:
            raise UserError("noChannel")
        payload = {"id": msg_id, "channel": channel, "text": request["text"], "kind": "reply"}
        if ticket:
            payload["ticketId"] = ticket["id"]
        self.options.outbox.send(payload)
        if request["purpose"] == "answer" and ticket and trigger and trigger["id"] in (ticket.get("answers") or []):
            ticket["answers"] = [a for a in ticket["answers"] if a != trigger["id"]]
            self.repo.tickets.save(ticket)
        return {"id": msg_id, "status": self.options.outbox.status(msg_id) or "pending", "reused": existing is not None}

    def _source_channel(self, ticket: dict):
        src = self.source(ticket)
        if src and src.get("channel"):
            return src["channel"]
        channels = ticket.get("channels") or []
        return channels[0] if channels else None
