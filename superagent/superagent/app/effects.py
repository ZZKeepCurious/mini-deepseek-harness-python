from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from typing import Callable

from superagent.app.store import Store


class Later(Exception):
    pass


FAILURES_BEFORE_ERROR = 3


class Effects:
    def __init__(self, store: Store | None = None, board=None, now: Callable[[], float] | None = None) -> None:
        self.store = store
        self._now = now or time.time
        self._handlers: dict[str, dict] = {}
        self._memory_rows: list[dict] = []
        self._seq = store.max_effect_seq() if store else 0
        self.started = False
        self.stopped = False

    def enqueue(self, kind: str, key: str, payload: dict) -> None:
        self._seq += 1
        value = json.dumps(payload)
        due = self._now()
        since = datetime.fromtimestamp(due, tz=timezone.utc).isoformat().replace("+00:00", "Z")
        if self.store:
            self.store.queue_effect(kind, key, value, self._seq, due, since)
            self.store.after_commit(self.kick)
        else:
            self._memory_rows = [r for r in self._memory_rows if not (r["kind"] == kind and r["key"] == key)]
            self._memory_rows.append(
                {
                    "kind": kind,
                    "key": key,
                    "value": value,
                    "seq": self._seq,
                    "due": due,
                    "generation": 0,
                    "attempts": 0,
                }
            )
            self.kick()

    def handle(self, kind: str, run, options: dict | None = None) -> None:
        if kind in self._handlers:
            raise RuntimeError(f"The {kind} effects already have a handler")
        options = options or {}
        self._handlers[kind] = {"run": run, **options}

    def pending(self, kind: str, key: str | None = None) -> bool:
        if self.store:
            if key is None:
                row = self.store.db.execute("SELECT 1 FROM effect WHERE kind=? LIMIT 1", (kind,)).fetchone()
                return row is not None
            row = self.store.db.execute(
                "SELECT 1 FROM effect WHERE kind=? AND key=? LIMIT 1", (kind, key)
            ).fetchone()
            return row is not None
        if key is None:
            return any(r["kind"] == kind for r in self._memory_rows)
        return any(r["kind"] == kind and r["key"] == key for r in self._memory_rows)

    def cancel(self, kind: str, key: str) -> None:
        if self.store:
            self.store.delete_effect(kind, key)
            self.store.after_commit(self.kick)
        else:
            self._memory_rows = [r for r in self._memory_rows if not (r["kind"] == kind and r["key"] == key)]

    def start(self) -> None:
        self.started = True
        self.kick()

    def kick(self) -> None:
        if not self.started or self.stopped:
            return
        self._dispatch()

    def idle(self, kind: str | None = None) -> None:
        for _ in range(256):
            before = self.pending(kind) if kind else any(self.pending(k) for k in self._handlers)
            self._dispatch()
            after = self.pending(kind) if kind else any(self.pending(k) for k in self._handlers)
            if not after or after == before and not self._dispatch_ran():
                break

    def _dispatch_ran(self) -> bool:
        return getattr(self, "_last_dispatch_worked", False)

    def drain(self) -> None:
        self.stopped = True

    def _rows_for(self, kind: str, limit: int) -> list[dict]:
        now = self._now()
        if self.store:
            return self.store.list_effects(kind, now, limit)
        return [r for r in self._memory_rows if r["kind"] == kind and r["due"] <= now][:limit]

    def _dispatch(self) -> None:
        self._last_dispatch_worked = False
        if not self.started or self.stopped:
            return
        now = self._now()
        for kind, handler in self._handlers.items():
            limit_fn = handler.get("limit", lambda: 1)
            limit = limit_fn() if callable(limit_fn) else limit_fn
            if limit <= 0:
                continue
            ready = self._rows_for(kind, limit + 16)[:limit]
            for row in ready:
                self._last_dispatch_worked = True
                payload = json.loads(row["value"])
                try:
                    handler["run"](payload, row["key"])
                except Later:
                    retry = handler.get("retry", [2000, 60_000])
                    delay = retry[0] / 1000
                    if self.store:
                        self.store.reschedule_effect(
                            kind, row["key"], row["seq"], row["generation"], now + delay
                        )
                        self.store.db.commit()
                    else:
                        row["due"] = now + delay
                        row["attempts"] = row.get("attempts", 0) + 1
                    continue
                if self.store:
                    self.store.remove_effect(kind, row["key"], row["seq"], row["generation"])
                    self.store.db.commit()
                else:
                    self._memory_rows = [
                        r
                        for r in self._memory_rows
                        if not (r["kind"] == kind and r["key"] == row["key"] and r["seq"] == row["seq"])
                    ]
