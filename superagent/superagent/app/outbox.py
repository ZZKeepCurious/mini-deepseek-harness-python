from __future__ import annotations


class InMemoryOutbox:
    def __init__(self) -> None:
        self._messages: dict[str, dict] = {}

    def send(self, message: dict) -> None:
        if message["id"] not in self._messages:
            self._messages[message["id"]] = dict(message)

    def status(self, id: str) -> str | None:
        receipt = self._messages.get(id)
        return receipt.get("status", "pending") if receipt else None

    def withdraw(self, id: str) -> None:
        if self._messages.get(id, {}).get("status", "pending") == "pending":
            self._messages.pop(id, None)

    def list_pending(self) -> list[dict]:
        return [m for m in self._messages.values()]


class Outbox:
    def __init__(self, host) -> None:
        self.host = host
        host.effects.handle("reply", lambda _payload, id: self._deliver(id))

    def send(self, message: dict) -> None:
        store = self.host.store

        def work():
            if store.receipts.add(message):
                self.host.effects.enqueue("reply", message["id"], {})

        store.atomic(work)

    def status(self, id: str) -> str | None:
        receipt = self.host.store.receipts.get(id)
        return receipt.get("status") if receipt else None

    def withdraw(self, id: str) -> None:
        store = self.host.store

        def work():
            if store.receipts.withdraw(id):
                self.host.effects.cancel("reply", id)

        store.atomic(work)

    def _deliver(self, id: str) -> None:
        receipt = self.host.store.receipts.get(id)
        if not receipt or receipt.get("status") != "pending":
            return
        self.host.sources.send(receipt)
