"""SQLite FTS5 检索索引（对齐 packages/session-query/session-query-sqlite）。

上游构建 durable FTS 索引并提供检索；mini 以 sqlite3 FTS5 承载等价检索：文档 =
`build_search_documents` 的语义文本，按 bm25 排序、`snippet()` 截取命中摘要。查询按
**数据**处理（词项引号包裹 AND），绝不作为 FTS 语法执行。
"""
from __future__ import annotations

import re
import sqlite3

from .config import SessionQueryError

__all__ = ["SqliteSearchIndex", "match_query"]

_TOKEN = re.compile(r"\w+", re.UNICODE)


def match_query(query: str) -> str:
    """把用户查询当数据：抽取词项并以引号 AND 连接（空 → 空串）。"""
    tokens = _TOKEN.findall(query or "")
    return " AND ".join(f'"{token}"' for token in tokens)


class SqliteSearchIndex:
    """按 session 分组的 FTS5 语义文档索引。"""

    def __init__(self, path: str = ":memory:"):
        self._conn = sqlite3.connect(path)
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5("
            "session_id UNINDEXED, seq UNINDEXED, type UNINDEXED, time UNINDEXED, "
            "surface UNINDEXED, text)")

    def close(self) -> None:
        self._conn.close()

    def index_session(self, session_id: str, documents: list) -> None:
        try:
            with self._conn:
                self._conn.execute("DELETE FROM docs WHERE session_id = ?", (session_id,))
                self._conn.executemany(
                    "INSERT INTO docs (session_id, seq, type, time, surface, text) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    [(session_id, doc["seq"], doc["type"], doc["time"], doc["surface"],
                      doc["text"]) for doc in documents])
        except sqlite3.Error as error:
            raise SessionQueryError(f"session query index failed: {error}",
                                    "SESSION_QUERY_INDEX_FAILED", error) from error

    def remove_session(self, session_id: str) -> None:
        with self._conn:
            self._conn.execute("DELETE FROM docs WHERE session_id = ?", (session_id,))

    def _select(self, query: str, session_id: str | None, filters: list,
                limit: int, offset: int) -> list:
        match = match_query(query)
        if match == "":
            return []
        sql = ("SELECT session_id, seq, type, time, surface, "
               "snippet(docs, 5, '', '', '…', 12) AS snippet, bm25(docs) AS rank "
               "FROM docs WHERE docs MATCH ?")
        params: list = [match]
        if session_id is not None:
            sql += " AND session_id = ?"
            params.append(session_id)
        for clause in filters or []:
            kind = clause.get("kind")
            if kind == "type":
                values = list(clause.get("values") or [])
                sql += f" AND type IN ({','.join('?' * len(values))})"
                params.extend(values)
            elif kind == "surface":
                values = list(clause.get("values") or [])
                sql += f" AND surface IN ({','.join('?' * len(values))})"
                params.extend(values)
            elif kind == "seq":
                if clause.get("from") is not None:
                    sql += " AND seq >= ?"
                    params.append(clause["from"])
                if clause.get("to") is not None:
                    sql += " AND seq <= ?"
                    params.append(clause["to"])
            elif kind == "time":
                if clause.get("from") is not None:
                    sql += " AND time >= ?"
                    params.append(clause["from"])
                if clause.get("to") is not None:
                    sql += " AND time <= ?"
                    params.append(clause["to"])
            elif kind == "text":
                # 语义文本扫描（非 FTS 词项）：子串匹配
                sql += " AND text LIKE ?"
                params.append(f"%{clause.get('text', '')}%")
        sql += " ORDER BY rank LIMIT ? OFFSET ?"
        params.extend([limit, offset])
        rows = self._conn.execute(sql, params).fetchall()
        return [{"sessionId": row[0], "seq": row[1], "type": row[2], "time": row[3],
                 "surface": row[4], "snippet": row[5], "rank": row[6]} for row in rows]

    def search(self, query: str, *, session_ids: list | None = None, filters: list | None = None,
               limit: int = 20, offset: int = 0) -> list:
        if session_ids is None:
            return self._select(query, None, filters, limit, offset)
        out: list = []
        for session_id in session_ids:
            out.extend(self._select(query, session_id, filters, limit, offset))
        out.sort(key=lambda hit: hit["rank"])
        return out[:limit]

    def search_session(self, session_id: str, query: str, *, filters: list | None = None,
                       limit: int = 20, offset: int = 0) -> list:
        return self._select(query, session_id, filters, limit, offset)
