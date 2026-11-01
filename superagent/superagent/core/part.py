from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from superagent.core.errors import UserError
from superagent.core.model import followup_owners, reached, thread_key


@dataclass
class EngineOptions:
    repo: Any
    policy: Callable[[], Any]
    scope: Any
    assessor: Any
    executor: Any
    outbox: Any
    prepareTicket: Callable[[dict, dict], dict] | None = None
    now: Callable[[], str] | None = None
    emit: Callable[[dict], None] | None = None


class EnginePart:
    def __init__(self, options: EngineOptions) -> None:
        self.options = options
        self.repo = options.repo

    def now(self) -> str:
        if self.options.now:
            return self.options.now()
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    def policy(self):
        return self.options.policy()

    def owners(self, trigger: dict) -> list[str]:
        return followup_owners(trigger, self.repo)

    def left_to_workers(self, trigger: dict) -> None:
        owners = self.owners(trigger)
        if owners:
            raise UserError("workerDecides", {"tickets": ", ".join(owners)})

    def hand_back(self, ticket_id: str, trigger: dict, reason: str) -> bool:
        decided = ticket_id in self.owners(trigger)
        disposition = self.repo.triggers.disposition(trigger["id"]) or {"fanout": [], "at": self.now()}
        handbacks = list(disposition.get("handbacks") or [])
        handbacks.append({"ticketId": ticket_id, "reason": reason, "at": self.now()})
        self.repo.triggers.saveDisposition(trigger["id"], {**disposition, "handbacks": handbacks})
        return decided and not self.owners(trigger) and self.queue_for_eval(trigger)

    def take_up(self, ticket_id: str, trigger_id: str) -> None:
        disposition = self.repo.triggers.disposition(trigger_id)
        if disposition and ticket_id not in (disposition.get("acked") or []):
            acked = list(disposition.get("acked") or [])
            acked.append(ticket_id)
            self.repo.triggers.saveDisposition(trigger_id, {**disposition, "acked": acked})

    def queue_for_eval(self, trigger: dict) -> bool:
        primary = self.thread(trigger["threads"][0])
        if trigger["id"] in primary.get("pending", []):
            return False
        evaluating = primary.get("evaluating")
        if evaluating and trigger["id"] in evaluating.get("triggerIds", []):
            return False
        primary.setdefault("pending", []).append(trigger["id"])
        self.repo.threads.save(primary)
        return True

    def emit(self, type: str, data: dict) -> None:
        if self.options.emit:
            self.options.emit({"type": type, "at": self.now(), "data": data})

    def require(self, ticket_id: str) -> dict:
        ticket = self.repo.tickets.get(ticket_id)
        if not ticket:
            raise UserError("unknownTicket")
        return ticket

    def thread(self, ref: dict) -> dict:
        k = thread_key(ref)
        existing = self.repo.threads.get(k)
        if existing:
            return existing
        return {
            "key": k,
            "ref": {"source": ref["source"], "id": ref["id"]},
            "createdAt": self.now(),
            "subscriptions": [],
            "correlated": [],
            "pending": [],
            "meta": {},
        }

    def tickets_on_thread(self, thread: dict) -> list[dict]:
        result = []
        for tid in thread.get("subscriptions", []):
            ticket = self.repo.tickets.get(tid)
            if ticket:
                result.append(ticket)
        return result

    def source(self, ticket: dict):
        return self.repo.triggers.get(ticket.get("origin", {}).get("triggerId", ""))

    def reaches(self, ticket: dict, trigger: dict, context: bool = True) -> bool:
        return reached(ticket, trigger, self.repo.threads.get, context)
