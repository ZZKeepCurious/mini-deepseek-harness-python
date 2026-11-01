from __future__ import annotations


class FakeExecutor:
    def __init__(self) -> None:
        self.launches: list[tuple[dict, dict]] = []
        self.deliveries: list[str] = []
        self.stops: list[tuple[dict, dict, str]] = []
        self.start_count = 0
        self.deliver_count = 0
        self.stop_count = 0

    def start(self, ticket: dict, assignment: dict) -> None:
        self.start_count += 1
        self.launches.append((ticket, assignment))

    def deliver(self, ticket: dict, assignment: dict) -> None:
        self.deliver_count += 1
        self.deliveries.append(ticket["id"])

    def stop(self, ticket: dict, assignment: dict, reason: str) -> None:
        self.stop_count += 1
        self.stops.append((ticket, assignment, reason))
