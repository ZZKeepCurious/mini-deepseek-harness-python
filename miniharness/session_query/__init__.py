"""session_query 族：会话检索/读取/追溯服务 + SQLite FTS 索引 + 模型侧工具。

上游：`packages/session-query/{session-query,session-query-sqlite,tool-session-query}`
（`session-log-export` 已由 `web/downloads.py` 承载）。
"""
from .config import (
    SESSION_QUERY_ERROR_CODES,
    SESSION_QUERY_READ_WINDOW_MAX,
    SessionQueryError,
)
from .documents import build_event_records, build_search_documents
from .extraction import extract_event_text
from .service import SessionQuery
from .sqlite import SqliteSearchIndex
from .tool import install_session_query_tools

__all__ = [
    "SESSION_QUERY_ERROR_CODES",
    "SESSION_QUERY_READ_WINDOW_MAX",
    "SessionQuery",
    "SessionQueryError",
    "SqliteSearchIndex",
    "build_event_records",
    "build_search_documents",
    "extract_event_text",
    "install_session_query_tools",
]
