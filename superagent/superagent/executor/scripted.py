from __future__ import annotations

from superagent.core.model import running_assignment


class ScriptedExecutor:
    """Runs a fixed script of Engine callbacks after launch/delivery (C3 stand-in for DshExecutor)."""

    def __init__(self, engine, steps: list[dict] | None = None) -> None:
        self.engine = engine
        self.steps = list(steps or [])
        self.launches: list[tuple[dict, dict]] = []
        self.start_count = 0
        self.deliver_count = 0

    def start(self, ticket: dict, assignment: dict) -> None:
        self.start_count += 1
        self.launches.append((ticket, assignment))
        self.engine.assignment_started(ticket["id"], assignment["id"])
        self._run_script(ticket)

    def deliver(self, ticket: dict, assignment: dict) -> None:
        self.deliver_count += 1
        running = running_assignment(ticket) or assignment
        for input_id in list(ticket.get("undelivered", [])):
            self.engine.inputs_delivered(ticket["id"], running["id"], [input_id])

    def stop(self, ticket: dict, assignment: dict, reason: str) -> None:
        pass

    def _run_script(self, ticket: dict) -> None:
        ticket_id = ticket["id"]
        for step in self.steps:
            kind = step["kind"]
            if kind == "report":
                self.engine.report(ticket_id, step["report"])
            elif kind == "turn_ended":
                assignment = running_assignment(self.engine.repo.tickets.get(ticket_id))
                if assignment:
                    self.engine.turn_ended(ticket_id, assignment["id"])
