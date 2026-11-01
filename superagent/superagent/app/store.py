from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from superagent.core.memory import MemoryRepositories


class Store(MemoryRepositories):
    def __init__(self, path: str) -> None:
        super().__init__()
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS trigger (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS disposition (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS thread (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS ticket (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS decision (id TEXT PRIMARY KEY, thread TEXT NOT NULL, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS effect (
                kind TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                seq INTEGER NOT NULL,
                due REAL NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0,
                attempts INTEGER NOT NULL DEFAULT 0,
                error TEXT NOT NULL DEFAULT '',
                since TEXT NOT NULL DEFAULT '',
                PRIMARY KEY (kind, key)
            );
            CREATE INDEX IF NOT EXISTS effect_due ON effect(kind, due, seq);
            """
        )
        self._depth = 0
        self._after_commit: list = []
        self._effect_pending: list[tuple] = []
        self._load_from_sqlite()

    def _load_from_sqlite(self) -> None:
        for row in self.db.execute("SELECT id, value FROM trigger"):
            self._trigger_rows[row[0]] = json.loads(row[1])
        for row in self.db.execute("SELECT id, value FROM disposition"):
            self._dispositions[row[0]] = json.loads(row[1])
        for row in self.db.execute("SELECT key, value FROM thread"):
            self._thread_rows[row[0]] = json.loads(row[1])
        for row in self.db.execute("SELECT id, value FROM ticket"):
            self._ticket_rows[row[0]] = json.loads(row[1])
        for row in self.db.execute("SELECT id, value, thread FROM decision"):
            self._decision_rows[row[0]] = json.loads(row[1])

    def max_effect_seq(self) -> int:
        row = self.db.execute("SELECT IFNULL(MAX(seq), 0) AS seq FROM effect").fetchone()
        return int(row[0]) if row else 0

    def queue_effect(self, kind: str, key: str, value: str, seq: int, due: float, since: str) -> None:
        self._effect_pending.append((kind, key, value, seq, due, since))

    def _flush_effects(self) -> None:
        for kind, key, value, seq, due, since in self._effect_pending:
            self.upsert_effect(kind, key, value, seq, due, since)
        self._effect_pending = []

    def upsert_effect(self, kind: str, key: str, value: str, seq: int, due: float, since: str) -> None:
        self.db.execute(
            """
            INSERT INTO effect (kind, key, value, seq, due, generation, attempts, error, since)
            VALUES (?, ?, ?, ?, ?, 0, 0, '', ?)
            ON CONFLICT(kind, key) DO UPDATE SET
                value = excluded.value,
                generation = generation + 1,
                attempts = 0,
                error = '',
                due = excluded.due,
                since = excluded.since
            """,
            (kind, key, value, seq, due, since),
        )

    def delete_effect(self, kind: str, key: str) -> None:
        self.db.execute("DELETE FROM effect WHERE kind=? AND key=?", (kind, key))

    def list_effects(self, kind: str, due_before: float, limit: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT kind, key, value, seq, due, generation, attempts FROM effect WHERE kind=? AND due<=? ORDER BY seq LIMIT ?",
            (kind, due_before, limit),
        ).fetchall()
        return [
            {
                "kind": r[0],
                "key": r[1],
                "value": r[2],
                "seq": r[3],
                "due": r[4],
                "generation": r[5],
                "attempts": r[6],
            }
            for r in rows
        ]

    def remove_effect(self, kind: str, key: str, seq: int, generation: int) -> bool:
        cur = self.db.execute(
            "DELETE FROM effect WHERE kind=? AND key=? AND seq=? AND generation=?",
            (kind, key, seq, generation),
        )
        return cur.rowcount > 0

    def reschedule_effect(self, kind: str, key: str, seq: int, generation: int, due: float, error: str = "") -> None:
        self.db.execute(
            "UPDATE effect SET due=?, attempts=attempts+1, error=? WHERE kind=? AND key=? AND seq=? AND generation=?",
            (due, error[:1000], kind, key, seq, generation),
        )

    def after_commit(self, fn) -> None:
        if self._depth > 0:
            self._after_commit.append(fn)

    def _persist(self) -> None:
        self.db.execute("DELETE FROM trigger")
        for id, value in self._trigger_rows.items():
            self.db.execute("INSERT INTO trigger VALUES (?, ?)", (id, json.dumps(value)))
        self.db.execute("DELETE FROM disposition")
        for id, value in self._dispositions.items():
            self.db.execute("INSERT INTO disposition VALUES (?, ?)", (id, json.dumps(value)))
        self.db.execute("DELETE FROM thread")
        for key, value in self._thread_rows.items():
            self.db.execute("INSERT INTO thread VALUES (?, ?)", (key, json.dumps(value)))
        self.db.execute("DELETE FROM ticket")
        for id, value in self._ticket_rows.items():
            self.db.execute("INSERT INTO ticket VALUES (?, ?)", (id, json.dumps(value)))
        self.db.execute("DELETE FROM decision")
        for id, value in self._decision_rows.items():
            self.db.execute("INSERT INTO decision VALUES (?, ?, ?)", (id, value["threadKey"], json.dumps(value)))
        self.db.commit()

    def atomic(self, work):
        self._depth += 1
        if self._depth == 1:
            self._effect_pending = []
            self._after_commit = []
        try:
            result = super().atomic(work)
            if self._depth == 1:
                self._flush_effects()
                self.db.commit()
                self._persist()
                callbacks = self._after_commit
                self._after_commit = []
                for fn in callbacks:
                    fn()
            return result
        except Exception:
            if self._depth == 1:
                self._effect_pending = []
                self._after_commit = []
            raise
        finally:
            self._depth -= 1

    def close(self) -> None:
        self.db.close()

    @property
    def receipts(self):
        store = self

        class Receipts:
            def get(self, id: str):
                row = store.db.execute("SELECT value FROM outbox WHERE id=?", (id,)).fetchone()
                return json.loads(row[0]) if row else None

            def add(self, message: dict) -> bool:
                receipt = {**message, "status": "pending"}
                cur = store.db.execute(
                    "INSERT OR IGNORE INTO outbox (id, value) VALUES (?, ?)",
                    (message["id"], json.dumps(receipt)),
                )
                return cur.rowcount > 0

            def withdraw(self, id: str) -> bool:
                cur = store.db.execute(
                    "DELETE FROM outbox WHERE id=? AND json_extract(value, '$.status')='pending'",
                    (id,),
                )
                return cur.rowcount > 0

            def mark_sent(self, id: str) -> None:
                row = store.db.execute("SELECT value FROM outbox WHERE id=?", (id,)).fetchone()
                if not row:
                    return
                receipt = json.loads(row[0])
                receipt["status"] = "sent"
                store.db.execute("UPDATE outbox SET value=? WHERE id=?", (json.dumps(receipt), id))

        return Receipts()
