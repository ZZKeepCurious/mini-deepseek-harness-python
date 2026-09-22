"""会话查询服务（对齐 packages/session-query/session-query 的检索/读取/追溯契约）。

`SessionQuery`（`ctx.sessionQuery`）：跨会话 `search`、会话内 `search_events`、
`read_event`（原始窗口）、`trace_event`（替换/来源关系）、`lineage`（世系）。
检索经 `SqliteSearchIndex`（FTS5）。

**载体差异（登记）**：上游的 tracing 提供方分层与 observation/lease（预热/冷快照租约）
不承载——mini 现场解析活会话/持久化日志；`availability`/`cursor` 以简单 keyset 承载。
授权/工作区作用域（workspace-access + sessionProjections）不承载：调用方 scope 由工具层
以调用会话约束（mini 无 workspace 实体，M5）。
"""
from __future__ import annotations

from typing import Any

from ..core.scope import Context, Service
from ..core.session import SESSION_FORMAT_VERSION
from .config import (
    SESSION_QUERY_READ_WINDOW_MAX,
    SessionQueryError,
)
from .documents import build_event_records, build_search_documents
from .sqlite import SqliteSearchIndex

__all__ = ["SessionQuery"]

_MAX_LIMIT = 200

#: read_surface 保留的 surface 事件类型（session-reference 投影面）。
_SURFACE_EVENT_TYPES = frozenset({
    "user/message", "assistant/message", "system/message", "tool/result"})


def _header_of(session: Any) -> dict:
    meta = dict(getattr(session, "meta", {}) or {})
    return {
        "id": session.session_id,
        "createdAt": getattr(session, "created_at", None),
        "cwd": meta.get("cwd"),
        "parentSession": meta.get("parentSession"),
        "isSeeded": bool(meta.get("isSeeded")),
    }


class SessionQuery(Service):
    """会话检索/读取/追溯服务。"""

    provide = "sessionQuery"

    def __init__(self, ctx: Context, config: dict | None = None, *, persistence: Any = None,
                 index: SqliteSearchIndex | None = None):
        config = config or {}
        self.read_window_max = config.get("readWindowMax", SESSION_QUERY_READ_WINDOW_MAX)
        if not isinstance(self.read_window_max, int) or self.read_window_max < 1:
            raise SessionQueryError("readWindowMax must be a positive integer",
                                    "SESSION_QUERY_INVALID_CONFIG")
        super().__init__(ctx, "sessionQuery")
        self._persistence = persistence
        self._index = index or SqliteSearchIndex(":memory:")
        self._indexed: dict = {}

    # ---------- 源解析 ----------

    def _store(self):
        return self.ctx.get("sessions")

    def _resolve(self, session_id: str):
        """返回 (events, header, live)；未知会话 → SessionQueryError。"""
        store = self._store()
        session = store.get(session_id) if store is not None else None
        if session is not None:
            return list(session.events), _header_of(session), True
        if self._persistence is not None and hasattr(self._persistence, "load"):
            try:
                events = self._persistence.load(session_id)
            except SessionQueryError:
                raise
            except BaseException as error:
                raise SessionQueryError(
                    f"failed to read persisted session {session_id}: {error}",
                    "SESSION_QUERY_PERSISTENCE_FAILED", error) from error
            header = {"id": session_id}
            if hasattr(self._persistence, "inspect"):
                try:
                    header.update(self._persistence.inspect(session_id) or {})
                except BaseException:  # noqa: BLE001 - 头信息可选
                    pass
            return list(events), header, False
        raise SessionQueryError(f"session {session_id} not found",
                                "SESSION_QUERY_SESSION_NOT_FOUND")

    def _candidates(self) -> dict:
        result: dict = {}
        store = self._store()
        if store is not None:
            for session in store.list():
                result[session.session_id] = (_header_of(session), True)
        if self._persistence is not None and hasattr(self._persistence, "list_headers"):
            try:
                headers = self._persistence.list_headers() or []
            except BaseException:  # noqa: BLE001 - 持久化枚举失败不拖垮检索
                headers = []
            for header in headers:
                if isinstance(header, dict) and header.get("id"):
                    result.setdefault(header["id"], (header, False))
        return result

    def _ensure_indexed(self, session_id: str) -> None:
        events, _header, _live = self._resolve(session_id)
        signature = (len(events), events[-1].get("seq") if events else -1)
        if self._indexed.get(session_id) == signature:
            return
        self._index.index_session(session_id, build_search_documents(session_id, events))
        self._indexed[session_id] = signature

    # ---------- 校验 ----------

    def _validate_query(self, query: Any) -> str:
        if not isinstance(query, str) or query.strip() == "":
            raise SessionQueryError("query must be a non-empty string",
                                    "SESSION_QUERY_INVALID_QUERY")
        if "\0" in query:
            raise SessionQueryError("query must not contain NUL", "SESSION_QUERY_INVALID_QUERY")
        return query

    def _validate_limit(self, limit: Any, default: int = 20) -> int:
        if limit is None:
            return default
        if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1 or limit > _MAX_LIMIT:
            raise SessionQueryError(f"limit must be an integer in [1, {_MAX_LIMIT}]",
                                    "SESSION_QUERY_INVALID_LIMIT")
        return limit

    # ---------- 检索 ----------

    def search(self, request: dict) -> dict:
        query = self._validate_query(request.get("query"))
        limit = self._validate_limit(request.get("limit"))
        offset = int(request.get("cursor") or 0)
        candidates = self._candidates()
        filters = request.get("sessionFilters") or []

        hits = []
        for session_id, (header, live) in candidates.items():
            if not self._matches_session(header, live, filters):
                continue
            try:
                self._ensure_indexed(session_id)
            except SessionQueryError:
                continue
            matches = self._index.search_session(
                session_id, query, filters=request.get("eventFilters"),
                limit=1, offset=0)
            if matches:
                best = matches[0]
                hits.append({**self._record(header, live), "bestMatch": best,
                             "_rank": best["rank"]})
        hits.sort(key=lambda hit: hit["_rank"])
        page = hits[offset:offset + limit]
        next_cursor = offset + limit if offset + limit < len(hits) else None
        return {"items": [{k: v for k, v in hit.items() if k != "_rank"} for hit in page],
                "nextCursor": next_cursor}

    def search_events(self, request: dict) -> dict:
        session_id = request.get("sessionId")
        if not isinstance(session_id, str) or session_id == "":
            raise SessionQueryError("sessionId must be a non-empty string",
                                    "SESSION_QUERY_SESSION_NOT_FOUND")
        query = self._validate_query(request.get("query"))
        limit = self._validate_limit(request.get("limit"))
        offset = int(request.get("cursor") or 0)
        self._ensure_indexed(session_id)
        hits = self._index.search_session(session_id, query, filters=request.get("filters"),
                                          limit=limit, offset=offset)
        next_cursor = offset + limit if len(hits) == limit else None
        return {"items": hits, "nextCursor": next_cursor}

    # ---------- 读取 / 追溯 ----------

    def list_sessions(self) -> list[dict]:
        """列出全部候选会话（活 + 持久化）的 SessionRecord（对齐上游 listSessions）。"""
        return [self._record(header, live) for header, live in self._candidates().values()]

    def read_surface(self, session_id: str) -> dict:
        """读取某会话当前 surface 快照（对齐上游 readSurface）。

        返回 `{session: {id, cwd, version}, capturedThroughSeq, events}`；
        events 只含 surface 事件（user/message、assistant/message、system/message、
        tool/result），供跨会话引用投影。
        """
        events, header, _live = self._resolve(session_id)
        surface = [event for event in events
                   if event.get("type") in _SURFACE_EVENT_TYPES]
        captured = surface[-1].get("seq") if surface else None
        return {
            "session": {"id": header.get("id"), "cwd": header.get("cwd"),
                        "version": SESSION_FORMAT_VERSION},
            "capturedThroughSeq": captured,
            "events": surface,
        }

    def read_event(self, request: dict) -> dict:
        session_id = request.get("sessionId")
        seq = request.get("seq")
        before = request.get("before", 0) or 0
        after = request.get("after", 0) or 0
        if max(before, after) > self.read_window_max:
            raise SessionQueryError(
                f"before/after must be no greater than {self.read_window_max}",
                "SESSION_QUERY_INVALID_WINDOW")
        events, header, live = self._resolve(session_id)
        by_seq = {event.get("seq"): event for event in events}
        target = by_seq.get(seq)
        if target is None:
            raise SessionQueryError(f"event {seq} not found in session {session_id}",
                                    "SESSION_QUERY_EVENT_NOT_FOUND")
        low = max(0, seq - before)
        high = seq + after
        window = [event for event in events if low <= event.get("seq") <= high]
        return {"session": header, "target": target, "events": window,
                "startSeq": window[0].get("seq"), "endSeq": window[-1].get("seq")}

    def trace_event(self, request: dict) -> dict:
        session_id = request.get("sessionId")
        seq = request.get("seq")
        events, header, _live = self._resolve(session_id)
        by_seq = {event.get("seq"): event for event in events}
        if seq not in by_seq:
            raise SessionQueryError(f"event {seq} not found in session {session_id}",
                                    "SESSION_QUERY_EVENT_NOT_FOUND")
        target = by_seq[seq]
        source = list((target.get("data") or {}).get("sourceEventSeqs") or [])
        derived = [event.get("seq") for event in events
                   if seq in ((event.get("data") or {}).get("sourceEventSeqs") or [])]
        replaced: list = []
        replacement_chain: list = []
        op = target.get("surfaceOp")
        if isinstance(op, dict) and op.get("op") == "replace":
            start, end = op.get("startSeq"), op.get("endSeq")
            replaced = [event.get("seq") for event in events
                        if start is not None and end is not None
                        and start <= event.get("seq") <= end
                        and event.get("seq") != seq]
        for event in events:
            op2 = event.get("surfaceOp")
            if (isinstance(op2, dict) and op2.get("op") == "replace"
                    and op2.get("startSeq") is not None and op2.get("endSeq") is not None
                    and op2["startSeq"] <= seq <= op2["endSeq"]
                    and event.get("seq") > seq):
                replacement_chain.append(event.get("seq"))
        return {"target": build_event_records(session_id, [target])[0],
                "replacementChain": replacement_chain, "replacedEventSeqs": replaced,
                "sourceEventSeqs": source, "derivedEventSeqs": derived,
                "session": header}

    def lineage(self, request: dict) -> dict:
        session_id = request.get("sessionId")
        candidates = self._candidates()
        if session_id not in candidates:
            raise SessionQueryError(f"session {session_id} not found",
                                    "SESSION_QUERY_SESSION_NOT_FOUND")
        ancestors: list = []
        current = candidates[session_id][0].get("parentSession")
        complete = True
        unresolved = None
        while current is not None:
            if current not in candidates:
                complete = False
                unresolved = current
                break
            header, live = candidates[current]
            ancestors.append(self._record(header, live))
            current = header.get("parentSession")
        children: dict = {}
        for sid, (header, _live) in candidates.items():
            parent = header.get("parentSession")
            if parent:
                children.setdefault(parent, []).append(sid)

        def nodes(parent_id: str) -> list:
            out = []
            for child in children.get(parent_id, []):
                header, live = candidates[child]
                out.append({"session": self._record(header, live),
                            "descendants": nodes(child)})
            return out

        target_header, target_live = candidates[session_id]
        result = {"target": self._record(target_header, target_live),
                  "ancestors": ancestors, "descendants": nodes(session_id)}
        if complete:
            result["complete"] = True
            result["root"] = (ancestors[-1] if ancestors
                              else self._record(target_header, target_live))
        else:
            result["complete"] = False
            result["unresolvedParentId"] = unresolved
        return result

    # ---------- 内部 ----------

    def _record(self, header: dict, live: bool) -> dict:
        return {"header": header, "live": live, "persisted": True}

    def _matches_session(self, header: dict, live: bool, filters: list) -> bool:
        for clause in filters or []:
            kind = clause.get("kind")
            values = clause.get("values")
            if kind == "id":
                if header.get("id") not in values:
                    return False
            elif kind == "cwd":
                if header.get("cwd") not in values:
                    return False
            elif kind == "parent":
                if header.get("parentSession") not in values:
                    return False
            elif kind == "availability":
                if "live" in values and live:
                    continue
                if "persisted" in values and not live:
                    continue
                return False
            elif kind == "created-at":
                created = header.get("createdAt")
                if clause.get("from") is not None and (created is None or created < clause["from"]):
                    return False
                if clause.get("to") is not None and (created is None or created > clause["to"]):
                    return False
            else:
                raise SessionQueryError(f"unknown session filter {kind!r}",
                                        "SESSION_QUERY_INVALID_FILTER")
        return True
