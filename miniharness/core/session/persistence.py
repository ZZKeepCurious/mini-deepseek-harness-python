"""第 5 章：会话持久化 —— JSONL / SQLite 双后端 + 崩溃恢复。

对应 dsh 真实源码：packages/session/session-persistence。

硬性规定：
  1. append 先复制事件、异步成批写入；flush 是"等待的栅栏"
  2. JSONL 每文件 header 行 + 事件行；SESSION_FORMAT_VERSION 不符整体拒读
     （fail-closed，上游 format.ts / assertVersion）
  3. torn 尾部（写入中途崩溃留下的残行）截断修复，绝不静默丢弃
  4. load 时未知事件类型（未带 ignorable）整体拒绝 —— fail-closed
  5. 崩溃恢复只合成 closers（工具结果 / step/end / turn/end），不截断日志

简化说明（有意保留）：上游默认 zstd 帧压缩与 CRC 校验依赖第三方库，
本实现为明文 JSONL；fsync 与目录级持久化语义以 flush 屏障近似。
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from . import (
    KNOWN_TYPES,
    SESSION_FORMAT_VERSION,
    Session,
    repair_interrupted_turn,
    thaw,
    turn_balance,
)


class SessionPersistence:
    """接缝接口：append / flush / load。"""

    def append(self, session_id: str, event: dict) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        raise NotImplementedError

    def load(self, session_id: str) -> list[dict]:
        raise NotImplementedError


class JsonlPersistence(SessionPersistence):
    """JSONL 后端：每会话一个文件；header 行 + 事件行（上游 format.ts）。"""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._pending: dict[str, list[dict]] = {}
        self._created: set[str] = set()

    def _path(self, session_id: str) -> Path:
        safe = session_id.replace("/", "_").replace("\\", "_")
        return self.root / f"{safe}.jsonl"

    def append(self, session_id, event):
        self._pending.setdefault(session_id, []).append(event)

    def _ensure_header(self, session_id: str) -> None:
        path = self._path(session_id)
        if session_id in self._created or path.exists():
            return
        header = {"version": SESSION_FORMAT_VERSION, "id": session_id}
        with open(path, "w", encoding="utf-8") as f:
            f.write(json.dumps(header, ensure_ascii=False) + "\n")
        self._created.add(session_id)

    def flush(self):
        for sid, events in self._pending.items():
            self._ensure_header(sid)
            with open(self._path(sid), "a", encoding="utf-8") as f:
                for ev in events:
                    f.write(json.dumps(thaw(ev), ensure_ascii=False) + "\n")
        self._pending.clear()

    def load(self, session_id):
        path = self._path(session_id)
        if not path.exists():
            return []
        events = []
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if not lines:
            return []
        header = json.loads(lines[0])
        if header.get("version") != SESSION_FORMAT_VERSION:
            raise RuntimeError(
                f"会话格式版本 {header.get('version')} 与当前 {SESSION_FORMAT_VERSION} 不符，拒绝加载"
            )
        body = lines[1:]
        if body and not body[-1].endswith("\n"):
            # torn 尾部：写入中途崩溃留下的残行，截断修复（上游 torn-tail 恢复）
            try:
                json.loads(body[-1].strip())
            except json.JSONDecodeError:
                body = body[:-1]
                self._truncate(path, lines[:1] + body)
        for line in body:
            line = line.strip()
            if line:
                events.append(json.loads(line))
        return events

    def _truncate(self, path: Path, kept: list[str]) -> None:
        with open(path, "w", encoding="utf-8") as f:
            f.write("".join(kept))


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
                (sid, base + 1 + i, ev["type"], json.dumps(thaw(ev), ensure_ascii=False))
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
    """load → 校验 → 崩溃修复 → 以 seed 回放进内存 Session（重启后继续对话）。

    与上游 prepareCore 顺序一致：fail-closed 校验在前，repair 只合成缺失 closers。
    """
    raw = load_events_checked(persistence.load(session_id))
    repaired = raw + repair_interrupted_turn(raw)
    if repaired:
        session = Session(session_id, seed=repaired, created_at=session.created_at)
    return session


def balanced_after_replay(persistence: SessionPersistence, session_id: str) -> bool:
    """验收辅助：加载修复后日志必须括号平衡。"""
    raw = load_events_checked(persistence.load(session_id))
    return turn_balance(raw + repair_interrupted_turn(raw)) == 0