from __future__ import annotations

from superagent.core.assignments import Assignments
from superagent.core.errors import UserError
from superagent.core.evals import Evals, Outcome
from superagent.core.intake import Intake, ReceiveResult
from superagent.core.model import iso_time, live_ticket, running_assignment
from superagent.core.part import EngineOptions
from superagent.core.replies import Replies

Reply = dict
ReplyRequest = dict
from superagent.core.tickets import Tickets

__all__ = [
    "Engine",
    "EngineOptions",
    "Outcome",
    "ReceiveResult",
    "Reply",
    "ReplyRequest",
]


class Engine:
    def __init__(self, options: EngineOptions) -> None:
        self.options = options
        self.repo = options.repo
        self.replies = Replies(options)
        self.tickets = Tickets(options, self.replies)
        self.assignments = Assignments(options, self.tickets, self.replies)
        self.evals = Evals(options, self.tickets)
        self.intake = Intake(options, self.tickets)

    def receive(self, trigger: dict) -> ReceiveResult:
        return self.repo.atomic(lambda: self.intake.receive(trigger))

    def bind_thread(self, ticket_id: str, thread: dict, own: bool) -> dict:
        return self.repo.atomic(lambda: self.tickets.bind(ticket_id, thread, own))

    def thread_closed(self, thread: dict, outcome: str, at: str) -> None:
        self.repo.atomic(lambda: self.tickets.thread_closed(thread, outcome, iso_time(at)))

    def evaluate_due(self) -> None:
        policy = self.options.policy()
        for thread in self.repo.threads.active():
            if thread.get("evaluating"):
                self.repo.atomic(lambda t=thread: self.evals.due(t, policy))
        if policy.paused:
            return
        for ticket in self.repo.tickets.active():
            if not live_ticket(ticket) and ticket.get("undelivered"):
                self.repo.atomic(lambda tid=ticket["id"]: self.tickets.wake(tid))
        for thread in self.repo.threads.active():
            if thread.get("pending") and not thread.get("evaluating"):
                self.repo.atomic(lambda k=thread["key"]: self.evals.start(k, policy))

    def candidate(self, trigger_id: str):
        return self.evals.candidate(trigger_id)

    def decide(self, trigger_id: str, outcome: Outcome) -> dict:
        return self.repo.atomic(lambda: self.evals.decide(trigger_id, outcome))

    def attach(self, ticket_id: str, trigger_id: str, note: str | None = None) -> dict:
        def work():
            disposition = self.repo.triggers.disposition(trigger_id) or {}
            handed = any(h.get("ticketId") == ticket_id for h in disposition.get("handbacks") or [])
            if handed:
                raise UserError("handedBackHere")
            trigger = self.tickets.attachable(ticket_id, trigger_id)
            self.evals.withdraw(trigger)
            self.tickets.attach(ticket_id, trigger, note)
            self.evals.routed(trigger_id, ticket_id)
            self.tickets.wake(ticket_id)
            return self.repo.tickets.get(ticket_id)

        return self.repo.atomic(work)

    def request_status(self, ticket_id: str) -> bool:
        return self.repo.atomic(lambda: self.tickets.request_status(ticket_id))

    def reply(self, request: ReplyRequest) -> Reply:
        return self.repo.atomic(lambda: self.replies.reply(request))

    def candidate_delivered(self, eval_id: str) -> None:
        self.repo.atomic(lambda: self.evals.delivered(eval_id, self.options.policy()))

    def reevaluate(self, trigger_id: str) -> bool:
        return self.repo.atomic(lambda: self.evals.reevaluate(trigger_id))

    def dismiss(self, decision_id: str) -> dict:
        return self.repo.atomic(lambda: self.evals.dismiss(decision_id))

    def unsubscribe(self, thread: str) -> bool:
        return self.repo.atomic(lambda: self.tickets.drop_subscriptions(thread, "person"))

    def stop_ticket(self, ticket_id: str, reason: str | None = None) -> dict:
        return self.repo.atomic(lambda: self.tickets.stop(ticket_id, reason))

    def retry_ticket(self, ticket_id: str) -> dict:
        return self.repo.atomic(lambda: self.tickets.retry(ticket_id))

    def assignment_started(self, ticket_id: str, assignment_id: str) -> None:
        self.repo.atomic(lambda: self.assignments.started(ticket_id, assignment_id))

    def inputs_delivered(self, ticket_id: str, assignment_id: str, input_ids: list[str]) -> None:
        self.repo.atomic(lambda: self.assignments.delivered(ticket_id, assignment_id, input_ids))

    def inputs_read(self, ticket_id: str, input_ids: list[str]) -> None:
        self.repo.atomic(lambda: self.assignments.read(ticket_id, input_ids))

    def turn_ended(self, ticket_id: str, assignment_id: str) -> None:
        self.repo.atomic(lambda: self.assignments.turn_ended(ticket_id, assignment_id))

    def delivery_failed(self, ticket_id: str, assignment_id: str, error: str) -> None:
        self.repo.atomic(lambda: self.assignments.delivery_failed(ticket_id, assignment_id, error))

    def mark_unrelated(self, ticket_id: str, thread: str, reason: str) -> None:
        self.repo.atomic(lambda: self.tickets.mark_unrelated(ticket_id, thread, reason))

    def follow_for_context(self, ticket_id: str, thread: dict) -> bool:
        return self.repo.atomic(lambda: self.tickets.follow_for_context(ticket_id, thread))

    def note(self, ticket_id: str, text: str) -> dict:
        return self.repo.atomic(lambda: self.tickets.note(ticket_id, text))

    def handback(self, ticket_id: str, trigger_id: str, reason: str) -> bool:
        return self.repo.atomic(lambda: self.evals.handback(ticket_id, trigger_id, reason))

    def report(self, ticket_id: str, report: dict) -> dict:
        return self.repo.atomic(lambda: self.assignments.report(ticket_id, report))

    def assignment_interrupted(self, ticket_id: str, assignment_id: str) -> None:
        self.repo.atomic(lambda: self.assignments.interrupted(ticket_id, assignment_id))

    def policy_changed(self, from_version: str, to_version: str, effects: dict | None = None) -> None:
        def work() -> None:
            self.tickets.policy_changed(from_version, to_version, effects)
            self.evals.policy_changed(from_version, to_version)

        self.repo.atomic(work)
