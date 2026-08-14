"""第 5 章：会话持久化 —— JSONL / SQLite 双后端 + 崩溃恢复。

对应 dsh 真实源码：packages/session/session-persistence。

不变量：
  1. append 先复制事件、异步成批写入；flush 是"等待的栅栏"
  2. load 时未知事件类型（未带 ignorable）整体拒绝 —— fail-closed
  3. 崩溃恢复只合成 interrupted，绝不截断日志
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .session import KNOWN_TYPES, Session, repair_interrupted_turn, turn_balance


class SessionPersistence:
    """接缝接口：locate / append / load / flush。"""

    def append(self, session_id: str, event: dict) -> None:
        raise NotImplementedError

    def load(self, session_id: str) -> list[dict]:
        raise NotImplementedError

    def flush(self) -> None:
        raise NotImplementedError


class JsonlPersistence(SessionPersistence):
    """JSONL 后端：每会话一个文件；支持 packed chunk 行（此处为简化逐行）。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._pending: dict[str, list[dict]] = {}

    def _path(self, session_id: str) -> Path:
        safe = session_id.replace("/", "_").replace("\\", "_")
        return self.root / f"{safe}.jsonl"

    def append(self, session_id, event):
        self._pending.setdefault(session_id, []).append(event)

    def flush(self):
        for sid, events in self._pending.items():
            with open(self._path(sid), "a", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        self._pending.clear()

    def load(self, session_id):
        path = self._path(session_id)
        if not path.exists():
            return []
        events = []
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return events


class SqlitePersistence(SessionPersistence):
    """SQLite 后端：多会话一库；单调 SCHEMA_VERSION，版本不符拒绝加载。"""

    SCHEMA_VERSION = 1

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.root / "sessions.sqlite")
        self._conn.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = self._conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        if row is None:
            self._conn.execute("INSERT INTO meta VALUES ('schema_version', ?)", (str(self.SCHEMA_VERSION),))
            self._conn.commit()
        elif int(row[0]) != self.SCHEMA_VERSION:
            self._conn.close()
            raise RuntimeError(
                f"SQLite 库版本 {row[0]} 与当前 {self.SCHEMA_VERSION} 不一致，拒绝加载"
            )
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS events ("
            "session_id TEXT, seq INTEGER, type TEXT, data TEXT, "
            "PRIMARY KEY (session_id, seq))"
        )
        self._conn.commit()
        self._pending: dict[str, list[dict]] = {}

    def append(self, session_id, event):
        self._pending.setdefault(session_id, []).append(event)

    def flush(self):
        for sid, events in self._pending.items():
            base = self._conn.execute(
                "SELECT COALESCE(MAX(seq), -1) FROM events WHERE session_id=?", (sid,)
            ).fetchone()[0]
            rows = [
                (sid, base + 1 + i, ev["type"], json.dumps(ev, ensure_ascii=False))
                for i, ev in enumerate(events)
            ]
            self._conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?)", rows)
        self._conn.commit()
        self._pending.clear()

    def load(self, session_id):
        rows = self._conn.execute(
            "SELECT data FROM events WHERE session_id=? ORDER BY seq", (session_id,)
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def close(self):
        self._conn.close()


# ---------- 加载与恢复 ----------

def load_events_checked(raw_events: list[dict]) -> list[dict]:
    """fail-closed：未知事件类型（未带 ignorable）整体拒绝加载。"""
    for ev in raw_events:
        if ev.get("type") not in KNOWN_TYPES and not ev.get("ignorable"):
            raise RuntimeError(
                f"未知事件类型 {ev.get('type')!r}，拒绝加载（防止静默丢事件改变解读）"
            )
    return raw_events


def repair_and_replay(persistence: SessionPersistence, session_id: str, session: Session) -> Session:
    """load → 校验 → 崩溃修复 → 回放进内存 Session（重启后继续对话）。"""
    raw = load_events_checked(persistence.load(session_id))
    repaired = repair_interrupted_turn(raw)
    for ev in repaired:
        session.append(ev)
    return session


def balanced_after_replay(persistence: SessionPersistence, session_id: str) -> bool:
    """验收辅助：加载修复后日志必须括号平衡。"""
    return turn_balance(repair_interrupted_turn(persistence.load(session_id))) == 0