from __future__ import annotations

import copy

from superagent.core.model import latest_assignment


def _clone(value):
    return copy.deepcopy(value)


class MemoryRepositories:
    def __init__(self) -> None:
        self._trigger_rows: dict[str, dict] = {}
        self._dispositions: dict[str, dict] = {}
        self._thread_rows: dict[str, dict] = {}
        self._ticket_rows: dict[str, dict] = {}
        self._decision_rows: dict[str, dict] = {}

    def atomic(self, work):
        snapshot = [
            copy.deepcopy(m)
            for m in (
                self._trigger_rows,
                self._dispositions,
                self._thread_rows,
                self._ticket_rows,
                self._decision_rows,
            )
        ]
        try:
            return work()
        except Exception:
            maps = (
                self._trigger_rows,
                self._dispositions,
                self._thread_rows,
                self._ticket_rows,
                self._decision_rows,
            )
            for map_obj, snap in zip(maps, snapshot):
                map_obj.clear()
                map_obj.update(snap)
            raise

    @property
    def triggers(self):
        repo = self

        class Triggers:
            def get(self, id: str):
                row = repo._trigger_rows.get(id)
                return _clone(row) if row else None

            def save(self, trigger: dict) -> None:
                repo._trigger_rows[trigger["id"]] = _clone(trigger)

            def disposition(self, id: str):
                row = repo._dispositions.get(id)
                return _clone(row) if row else None

            def saveDisposition(self, id: str, value: dict) -> None:
                repo._dispositions[id] = _clone(value)

        return Triggers()

    @property
    def threads(self):
        repo = self

        class Threads:
            def get(self, key: str):
                row = repo._thread_rows.get(key)
                return _clone(row) if row else None

            def save(self, thread: dict) -> None:
                repo._thread_rows[thread["key"]] = _clone(thread)

            def list(self):
                return [_clone(t) for t in repo._thread_rows.values()]

            def active(self):
                return [
                    _clone(t)
                    for t in repo._thread_rows.values()
                    if t.get("pending") or t.get("evaluating") is not None
                ]

        return Threads()

    @property
    def tickets(self):
        repo = self

        class Tickets:
            def get(self, id: str):
                row = repo._ticket_rows.get(id)
                return _clone(row) if row else None

            def save(self, ticket: dict) -> None:
                repo._ticket_rows[ticket["id"]] = _clone(ticket)

            def list(self):
                return [_clone(t) for t in reversed(list(repo._ticket_rows.values()))]

            def open(self):
                return [t for t in self.list() if not t.get("ended")]

            def active(self):
                result = []
                for ticket in self.list():
                    latest = latest_assignment(ticket)
                    state = latest.get("state") if latest else None
                    if ticket.get("undelivered") or state in ("pending", "running"):
                        result.append(ticket)
                return result

        return Tickets()

    @property
    def decisions(self):
        repo = self

        class Decisions:
            def get(self, id: str):
                row = repo._decision_rows.get(id)
                return _clone(row) if row else None

            def save(self, decision: dict) -> None:
                repo._decision_rows[decision["id"]] = _clone(decision)

            def byThread(self, key: str):
                return [
                    _clone(d)
                    for d in repo._decision_rows.values()
                    if d.get("threadKey") == key
                ]

        return Decisions()
