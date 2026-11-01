from __future__ import annotations

from superagent.core.errors import UserError
from superagent.core.ids import key
from superagent.core.model import latest_assignment, live_ticket, running_assignment
from superagent.core.part import EnginePart, EngineOptions
from superagent.core.tickets import automatic

RECOVERY_NOTE = (
    "The host restarted during this ticket. Inspect the current worktree, git history and GitHub state before continuing. "
    "Do not repeat an already completed external operation. Finish the original assignment and report its status."
)


class Assignments(EnginePart):
    def __init__(self, options: EngineOptions, tickets, replies) -> None:
        super().__init__(options)
        self.tickets = tickets
        self.replies = replies

    def started(self, ticket_id: str, assignment_id: str) -> None:
        ticket = self.require(ticket_id)
        assignment = next((a for a in ticket.get("assignments", []) if a["id"] == assignment_id), None)
        if not assignment or assignment.get("state") == "ended":
            return
        assignment["state"] = "running"
        assignment.setdefault("startedAt", self.now())
        ticket["updatedAt"] = self.now()
        self.repo.tickets.save(ticket)
        if live_ticket(ticket) and ticket.get("undelivered"):
            running = running_assignment(ticket)
            if running:
                self.options.executor.deliver(ticket, running)
        self.emit("assignment.started", {"ticketId": ticket_id, "assignmentId": assignment_id})

    def delivered(self, ticket_id: str, assignment_id: str, input_ids: list[str]) -> None:
        ticket = self._running(ticket_id, assignment_id)
        if not ticket:
            return
        ticket["undelivered"] = [i for i in ticket.get("undelivered", []) if i not in input_ids]
        ticket["updatedAt"] = self.now()
        running = running_assignment(ticket)
        if running:
            running["deliveredAt"] = ticket["updatedAt"]
        for inp in ticket.get("inputs", []):
            if inp.get("kind") == "trigger" and inp["id"] in input_ids:
                if inp.get("readAt") is None:
                    inp["readAt"] = None
                inp["deliveredTo"] = assignment_id
        ticket.pop("delivery", None)
        self.repo.tickets.save(ticket)

    def read(self, ticket_id: str, input_ids: list[str]) -> None:
        ticket = self.require(ticket_id)
        if not live_ticket(ticket):
            return
        for inp in ticket.get("inputs", []):
            if inp.get("kind") == "trigger" and inp["id"] in input_ids and not inp.get("readAt"):
                inp["readAt"] = self.now()
        self.repo.tickets.save(ticket)

    def turn_ended(self, ticket_id: str, assignment_id: str) -> None:
        ticket = self._running(ticket_id, assignment_id)
        if not ticket:
            return
        running = running_assignment(ticket)
        if running:
            running["idleAt"] = self.now()
        self.repo.tickets.save(ticket)

    def delivery_failed(self, ticket_id: str, assignment_id: str, error: str) -> None:
        ticket = self._running(ticket_id, assignment_id)
        if not ticket:
            return
        ticket["delivery"] = {"error": error[:300], "at": self.now()}
        self.repo.tickets.save(ticket)

    def interrupted(self, ticket_id: str, assignment_id: str) -> None:
        ticket = self._running(ticket_id, assignment_id)
        running = running_assignment(ticket) if ticket else None
        if not ticket or not running:
            return
        report = running.get("report")
        busy = bool(running.get("deliveredAt") and report and running["deliveredAt"] >= report.get("at", ""))
        settled = bool(report and report.get("state") in ("waiting", "ready", "blocked", "failed") and not busy)
        if not settled:
            self.tickets.restart(ticket, running, "hostRestarted", RECOVERY_NOTE)
        elif ticket.get("undelivered"):
            self.options.executor.deliver(ticket, running)

    def report(self, ticket_id: str, report: dict) -> dict:
        ticket = self.require(ticket_id)
        if not live_ticket(ticket):
            raise UserError("ticketEnded")
        if report.get("question") and (report.get("state") == "ready" or report.get("answers")):
            raise UserError("questionWithResult")
        if report.get("askedBy") and not report.get("question"):
            raise UserError("askerWithoutQuestion")

        def person(trigger_id: str, need_channel: bool) -> bool:
            trigger = self.repo.triggers.get(trigger_id)
            if not trigger or not trigger.get("human"):
                return False
            if need_channel and not trigger.get("channel"):
                return False
            return self.reaches(ticket, trigger, False)

        if report.get("askedBy") and not person(report["askedBy"], True):
            raise UserError("notAsker")
        if report.get("answers") and not person(report["answers"], False):
            raise UserError("notAnswerable")
        assignment = running_assignment(ticket) or latest_assignment(ticket)
        if not assignment:
            raise UserError("unknownAssignment")
        if assignment.get("state") == "ended":
            raise UserError("assignmentEnded")
        at = self.now()
        previous = ticket.get("attention")
        asked = previous
        if report.get("question") and report.get("question") != (previous or {}).get("question"):
            asked = {
                "id": key("attention", ticket_id, report["question"], at),
                "question": report["question"],
            }
            if report.get("askedBy"):
                asked["triggerId"] = report["askedBy"]
        if report.get("state") == "ready" or ("question" in report and report["question"] is None):
            attention = None
        else:
            attention = asked
        if attention:
            ticket["attention"] = attention
        else:
            ticket.pop("attention", None)
        if previous and (previous.get("id") != (attention or {}).get("id")):
            self.replies.withdraw_attention(previous["id"])
        assignment["report"] = {"state": report["state"], "summary": report["summary"], "at": at}
        ticket["updatedAt"] = at
        if report.get("answers"):
            answers = list(ticket.get("answers") or [])
            if report["answers"] not in answers:
                answers.append(report["answers"])
            ticket["answers"] = answers
            self.take_up(ticket_id, report["answers"])
        if report.get("state") in ("blocked", "failed"):
            ticket["undelivered"] = [
                i
                for i in ticket.get("undelivered", [])
                if not automatic(next((x for x in ticket["inputs"] if x["id"] == i), None))
            ]
        self.repo.tickets.save(ticket)
        ready_outcome = "completed" if not ticket.get("follows") else (self.tickets.follows_done(ticket) or {}).get("outcome")
        if report.get("state") == "dropped":
            outcome = (self.tickets.follows_done(ticket) or {}).get("outcome") or "dropped"
        elif report.get("state") == "ready":
            outcome = ready_outcome
        else:
            outcome = None
        if outcome:
            self.tickets.end(ticket_id, "executor", outcome, at)
        return self.require(ticket_id)

    def _running(self, ticket_id: str, assignment_id: str):
        ticket = self.require(ticket_id)
        running = running_assignment(ticket)
        if live_ticket(ticket) and running and running["id"] == assignment_id:
            return ticket
        return None
