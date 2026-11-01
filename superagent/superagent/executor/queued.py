from __future__ import annotations

from superagent.core.model import running_assignment


class LaunchDeliveryExecutor:
    """Enqueues launch/delivery effects; handlers drive Engine like DshExecutor without DSH sessions."""

    def __init__(self, host) -> None:
        self.host = host
        host.effects.handle("launch", self._launch, {"limit": lambda: 4})
        host.effects.handle("delivery", self._delivery, {"limit": lambda: 4})

    def start(self, ticket: dict, assignment: dict) -> None:
        self.host.effects.enqueue(
            "launch",
            assignment["id"],
            {"ticketId": ticket["id"], "assignmentId": assignment["id"]},
        )

    def deliver(self, ticket: dict, assignment: dict) -> None:
        self.host.effects.enqueue("delivery", ticket["id"], {"ticketId": ticket["id"]})

    def stop(self, ticket: dict, assignment: dict, reason: str) -> None:
        pass

    def _launch(self, payload: dict, _key: str) -> None:
        ticket_id = payload["ticketId"]
        assignment_id = payload["assignmentId"]
        self.host.engine.assignment_started(ticket_id, assignment_id)

    def _delivery(self, payload: dict, _key: str) -> None:
        ticket_id = payload["ticketId"]
        ticket = self.host.repo.tickets.get(ticket_id)
        if not ticket or ticket.get("ended"):
            return
        assignment = running_assignment(ticket)
        if not assignment:
            return
        for input_id in list(ticket.get("undelivered", [])):
            self.host.engine.inputs_delivered(ticket_id, assignment["id"], [input_id])
