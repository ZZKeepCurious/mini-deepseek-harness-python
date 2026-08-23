"""第 5 章：会话持久化 —— JSONL / SQLite 双后端 + 崩溃恢复。

对应 dsh 真实源码：packages/session/session-persistence。

硬性规定：
  1. append 先复制事件、异步成批写入；flush 是"等待的栅栏"
  2. JSONL 每文件 header 行 + 事件行；SESSION_FORMAT_VERSION 不符整体拒读
     （fail-closed，上游 format.ts / assertVersion）
  3. torn 尾部（写入中途崩溃留下的残行）截断修复，绝不静默丢弃
  4. load 时未知事件类型（未带 ignorable）整体拒绝 —— fail-closed
  5. 崩溃恢复只合成 closers（工具结果 / step/end / turn/end），不截断有效日志；
     修复结果经 commit_repair 持久化（截断 torn 尾 + 追加 closers + fsync，
     对齐上游 commitRepair 两个 fsync 步）

简化说明（有意保留）：上游默认 zstd 帧压缩与 CRC 校验依赖第三方库，
本实现为明文 JSONL（zstd 未实现时标注简化）；JSONL 写路径已做真实 fsync。
"""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote

from . import (
    KNOWN_TYPES,
    SESSION_FORMAT_VERSION,
    Session,
    repair_interrupted_turn,
    thaw,
    turn_balance,
)
from .json import now_ms

# header 基础键（其余键扁平并入，对齐上游 SessionHeader 平面字段：
# version/id/createdAt + cwd?/parentSession?/seedLength?/origin?/
# delegationDepth?/agentPreset? + mini 扩展 label?）
_HEADER_BASE_KEYS = frozenset({"version", "id", "createdAt"})


def _flat_header(session_id: str, meta: dict | None, created_at: int | None) -> dict:
    """构造扁平 header（上游 prepare index.ts:877-887：meta 字段散开进 header）。"""
    header: dict[str, Any] = {
        "version": SESSION_FORMAT_VERSION,
        "id": session_id,
        "createdAt": created_at if created_at is not None else now_ms(),
    }
    if meta:
        header.update(meta)
    return header


def _header_meta(header: dict) -> dict | None:
    """从扁平 header 恢复 meta（剔除基础键；空 → None）。"""
    meta = {k: v for k, v in header.items() if k not in _HEADER_BASE_KEYS}
    return meta or None


# ---------- 上游 format.ts 目录布局（packages/session/session-persistence-jsonl）----------
# 上游布局：root/<encodeSegment(cwd)>/<encodeSegment(id)>/session.jsonl(.zstd)
# mini 不实现 zstd（保留简化），故恒为 .jsonl。
# encodeSegment 对齐上游 format.ts：encodeURIComponent 后把 '*' 转义为 '~'，
# 使任意 cwd/id（含路径分隔符）都折成单段安全目录名。

def encode_segment(value: str) -> str:
    """对齐上游 format.ts `encodeSegment`：URL 安全单段编码。"""
    return quote(value, safe="").replace("*", "~")


def decode_segment(value: str) -> str:
    """对齐上游 format.ts `decodeSegment`：逆变换。"""
    return unquote(value.replace("~", "*"))


def _project_dir(root: Path, cwd: str) -> Path:
    """对齐上游 format.ts `projectDir`：按 cwd 分项目目录（cwd 空串 → 即 root）。"""
    return root / encode_segment(cwd) if cwd else Path(root)


def _session_dir(root: Path, cwd: str, session_id: str) -> Path:
    """对齐上游 format.ts `sessionDir`：项目目录 → 会话目录。"""
    return _project_dir(root, cwd) / encode_segment(session_id)


def _log_suffix(compression: str | None) -> str:
    """对齐上游 format.ts `logSuffix`：明文 .jsonl（mini 不实现 zstd）。"""
    return ".jsonl" if compression is None else ".jsonl.zstd"


def _log_path(root: Path, cwd: str, session_id: str, compression: str | None = None) -> Path:
    """对齐上游 format.ts `logPath`：会话目录下的固定文件名 session.jsonl。"""
    name = f"session{_log_suffix(compression)}"
    return _session_dir(root, cwd, session_id) / name


class SessionPersistence:
    """接缝接口：append / flush / load，以及 A7 追加的 declare / inspect / list_headers。

    cwd 缺省时按上游契约从 header 元数据（meta.cwd）或既有落盘位置反查；无法反查
    的新会话（测试用）退化到项目目录 = root 的单层布局，保证可写可读。
    """

    def append(self, session_id: str, event: dict, cwd: str | None = None) -> None:
        raise NotImplementedError

    def flush(self) -> None:
        raise NotImplementedError

    def load(self, session_id: str, cwd: str | None = None) -> list[dict]:
        raise NotImplementedError

    def declare(self, session_id: str, meta: dict | None = None, created_at: int | None = None,
                cwd: str | None = None) -> None:
        """在首次 append 前写 header 元数据（子会话创建用）；幂等。"""
        raise NotImplementedError

    def inspect(self, session_id: str, cwd: str | None = None) -> dict:
        """返回 {meta, events}：meta 为 header 元数据（无则 None）。"""
        raise NotImplementedError

    def list_headers(self) -> list[dict]:
        """枚举全部会话 header：{id, meta, created_at, cwd}（meta/cwd 可能为 None）。"""
        raise NotImplementedError

    def read_raw(self, session_id: str, cwd: str | None = None) -> str | None:
        """读出会话的逐字原始制品文本（上游逐会话 raw artifact）。

        不支持该形态的后端（如 SQLite）抛 NotImplementedError，由调用方
        判定为 501（does not expose per-session raw artifacts）。
        """
        raise NotImplementedError

    def commit_repair(self, session_id: str, closers: list[dict], cwd: str | None = None) -> None:
        """把崩溃修复的 closers 持久化落盘（对齐上游 PersistenceBackend.commitRepair）。

        上游以 commitRepair(meta, tornMarker, closers) 截断 torn 尾并追加 closers；
        mini 的 torn 尾在 load 读路径已即时截断（含 fsync），故此处只追加 closers。
        """
        raise NotImplementedError


class JsonlPersistence(SessionPersistence):
    """JSONL 后端：每会话一个文件；header 行 + 事件行（上游 format.ts）。

    目录布局对齐上游 session-persistence-jsonl：
        root/<encodeSegment(cwd)>/<encodeSegment(id)>/session.jsonl
    cwd 缺省时按 header.meta.cwd 或既有落盘位置反查；新会话（cwd 反查不到）
    退化到项目目录 = root 的单层布局（root/<encodeSegment(id)>/session.jsonl），
    保证可写可读（测试用，真实会话恒带 cwd）。
    """

    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._pending: dict[str, list[dict]] = {}
        self._created: set[str] = set()
        # session_id -> 用于布局的 cwd（declare/append 时登记，缺省 ""）
        self._cwd: dict[str, str] = {}

    def _durable(self, path: Path, mode: str, lines: list[str]) -> None:
        """写入 + fsync：对齐上游 JSONL 后端的 fsync 持久化语义。"""
        with open(path, mode, encoding="utf-8") as f:
            f.write("".join(lines))
            f.flush()
            os.fsync(f.fileno())

    def _find(self, session_id: str) -> Path | None:
        """在 root 下递归定位会话文件（按 header.id 匹配），找不到返回 None。"""
        for path in self.root.glob("**/session.jsonl"):
            try:
                with open(path, encoding="utf-8") as f:
                    first = f.readline()
                header = json.loads(first) if first.strip() else {}
            except (ValueError, OSError):
                continue
            if isinstance(header, dict) and header.get("id") == session_id:
                return path
        return None

    def _resolve(self, session_id: str, cwd: str | None) -> Path:
        """解析会话文件绝对路径：cwd 给定优先 → 记忆 → 扫描 → 退化单层布局。"""
        if cwd is not None:
            self._cwd[session_id] = cwd
        elif session_id in self._cwd:
            cwd = self._cwd[session_id]
        else:
            found = self._find(session_id)
            if found is not None:
                # 从落盘位置反查 cwd（项目目录名即 encodeSegment(cwd)）
                proj_name = found.parent.parent.name
                cwd = decode_segment(proj_name) if found.parent.parent != self.root else ""
                self._cwd[session_id] = cwd
                return found
            cwd = ""
            self._cwd[session_id] = cwd
        return _log_path(self.root, cwd, session_id)

    def path_of(self, session_id: str, cwd: str | None = None) -> Path | None:
        """对外：返回会话文件绝对路径，不存在返回 None（供 CLI 存在性判断）。"""
        if cwd is None and session_id in self._cwd:
            cwd = self._cwd[session_id]
        if cwd is not None:
            path = _log_path(self.root, cwd, session_id)
            return path if path.exists() else None
        return self._find(session_id)

    def read_raw(self, session_id: str, cwd: str | None = None) -> str | None:
        """读出会话的逐字原始制品文本（header 行 + 事件行，未 thaw）。"""
        path = self.path_of(session_id, cwd)
        if path is None or not path.exists():
            return None
        return path.read_text(encoding="utf-8")

    def append(self, session_id, event, cwd: str | None = None):
        if cwd is not None:
            self._cwd[session_id] = cwd
        self._pending.setdefault(session_id, []).append(event)

    def declare(self, session_id: str, meta: dict | None = None, created_at: int | None = None,
                cwd: str | None = None) -> None:
        cwd = cwd if cwd is not None else (meta or {}).get("cwd") or ""
        self._cwd[session_id] = cwd
        path = _log_path(self.root, cwd, session_id)
        if session_id in self._created or path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        header = _flat_header(session_id, meta, created_at)
        self._durable(path, "w", [json.dumps(header, ensure_ascii=False) + "\n"])
        self._created.add(session_id)

    def _ensure_header(self, session_id: str) -> None:
        path = self._resolve(session_id, None)
        if session_id in self._created or path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        header = _flat_header(session_id, None, None)
        self._durable(path, "w", [json.dumps(header, ensure_ascii=False) + "\n"])
        self._created.add(session_id)

    def flush(self):
        for sid, events in self._pending.items():
            self._ensure_header(sid)
            path = self._resolve(sid, None)
            lines = [json.dumps(thaw(ev), ensure_ascii=False) + "\n" for ev in events]
            self._durable(path, "a", lines)
        self._pending.clear()

    def _read_file(self, path: Path):
        """读回 (header, events)；torn 尾部截断修复（与 load 共享）。"""
        if not path.exists():
            return None, []
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
        if not lines:
            return None, []
        header = json.loads(lines[0])
        if header.get("version") != SESSION_FORMAT_VERSION:
            raise RuntimeError(
                f"会话格式版本 {header.get('version')} 与当前 {SESSION_FORMAT_VERSION} 不符，拒绝加载"
            )
        body = lines[1:]
        if body and not body[-1].endswith("\n"):
            # torn 尾部：无换行的尾记录一律是写入中途崩溃的残行，整体忽略并
            # 截断落盘——内容合法与否不参与判定（上游 format.ts:338
            # "ignoring a final record without a newline as a torn tail"）
            body = body[:-1]
            self._truncate(path, lines[:1] + body)
        events = []
        for line in body:
            line = line.strip()
            if line:
                events.append(json.loads(line))
        return header, events

    def load(self, session_id, cwd: str | None = None):
        path = self._resolve(session_id, cwd)
        if path is None:
            return []
        _, events = self._read_file(path)
        return events

    def inspect(self, session_id, cwd: str | None = None):
        path = self._resolve(session_id, cwd)
        if path is None:
            return {"meta": None, "events": []}
        header, events = self._read_file(path)
        return {"meta": _header_meta(header) if header else None, "events": events}

    def list_headers(self):
        headers = []
        for path in sorted(self.root.glob("**/session.jsonl")):
            try:
                with open(path, encoding="utf-8") as f:
                    first = f.readline()
                header = json.loads(first) if first.strip() else {}
            except (ValueError, OSError):
                continue
            if not isinstance(header, dict) or header.get("version") != SESSION_FORMAT_VERSION:
                continue
            headers.append({
                "id": header.get("id"),
                "meta": _header_meta(header),
                "created_at": header.get("createdAt"),
                "cwd": header.get("cwd"),
            })
        return headers

    def _truncate(self, path: Path, kept: list[str]) -> None:
        self._durable(path, "w", kept)

    def commit_repair(self, session_id: str, closers: list[dict], cwd: str | None = None) -> None:
        if not closers:
            return
        self._ensure_header(session_id)
        path = self._resolve(session_id, cwd)
        path.parent.mkdir(parents=True, exist_ok=True)
        lines = [json.dumps(thaw(ev), ensure_ascii=False) + "\n" for ev in closers]
        self._durable(path, "a", lines)


class SqlitePersistence(SessionPersistence):
    """SQLite 后端：多会话一库；单调 SCHEMA_VERSION，版本不符拒绝加载。"""

    SCHEMA_VERSION = 2

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
        self._conn.execute(
            "CREATE TABLE IF NOT EXISTS sessions ("
            "session_id TEXT PRIMARY KEY, meta TEXT, created_at INTEGER)"
        )
        self._conn.commit()
        self._pending: dict[str, list[dict]] = {}

    def append(self, session_id, event, cwd: str | None = None):
        self._pending.setdefault(session_id, []).append(event)

    def declare(self, session_id: str, meta: dict | None = None, created_at: int | None = None,
                cwd: str | None = None) -> None:
        meta = dict(meta) if meta else {}
        if cwd is not None and "cwd" not in meta:
            meta["cwd"] = cwd
        self._conn.execute(
            "INSERT INTO sessions VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO NOTHING",
            (session_id, json.dumps(meta, ensure_ascii=False) if meta else None, created_at),
        )
        self._conn.commit()

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

    def load(self, session_id, cwd: str | None = None):
        rows = self._conn.execute(
            "SELECT data FROM events WHERE session_id=? ORDER BY seq", (session_id,)
        ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def inspect(self, session_id, cwd: str | None = None):
        row = self._conn.execute(
            "SELECT meta FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        meta = json.loads(row[0]) if row and row[0] else None
        return {"meta": meta, "events": self.load(session_id, cwd)}

    def list_headers(self):
        rows = self._conn.execute(
            "SELECT session_id, meta, created_at FROM sessions ORDER BY session_id"
        ).fetchall()
        out = []
        for r in rows:
            meta = json.loads(r[1]) if r[1] else None
            out.append({
                "id": r[0],
                "meta": meta,
                "created_at": r[2],
                "cwd": (meta or {}).get("cwd"),
            })
        return out

    def commit_repair(self, session_id: str, closers: list[dict], cwd: str | None = None) -> None:
        if not closers:
            return
        rows = [
            (session_id, ev["seq"], ev["type"], json.dumps(thaw(ev), ensure_ascii=False))
            for ev in closers
        ]
        self._conn.executemany(
            "INSERT INTO events (session_id, seq, type, data) VALUES (?, ?, ?, ?)",
            rows,
        )
        self._conn.commit()

    def read_raw(self, session_id: str, cwd: str | None = None) -> str | None:
        """SQLite 后端不暴露逐会话原始制品（事件在行列存储，无 JSONL 形态）。"""
        raise NotImplementedError

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
    修复结果持久化：closers 经 commit_repair 落盘（对齐上游 commitPrepared →
    backend.commitRepair），torn 尾截断在 load 读路径已即时落盘（含 fsync）。
    已平衡的日志返回空 closers，二次加载幂等。
    """
    raw = load_events_checked(persistence.load(session_id))
    closers = repair_interrupted_turn(raw)
    if closers:
        persistence.commit_repair(session_id, closers)
    repaired = raw + closers
    if repaired:
        session = Session(session_id, seed=repaired, created_at=session.created_at)
    return session


def balanced_after_replay(persistence: SessionPersistence, session_id: str) -> bool:
    """验收辅助：加载修复后日志必须括号平衡。"""
    raw = load_events_checked(persistence.load(session_id))
    return turn_balance(raw + repair_interrupted_turn(raw)) == 0