"""session-query 配置与错误码闭集（对齐 packages/session-query/session-query/src/config.ts）。"""
from __future__ import annotations

SESSION_QUERY_READ_WINDOW_MAX = 50
SESSION_QUERY_DEFAULT_PERSISTED_INSPECT_CONCURRENCY = 4
SESSION_QUERY_DEFAULT_PREPARED_SESSION_CACHE_SIZE = 5

SESSION_QUERY_ERROR_CODES = frozenset({
    "SESSION_QUERY_ABORTED",
    "SESSION_QUERY_CORRUPT_SESSION",
    "SESSION_QUERY_EVENT_NOT_FOUND",
    "SESSION_QUERY_INDEX_FAILED",
    "SESSION_QUERY_INVALID_CONFIG",
    "SESSION_QUERY_INVALID_CURSOR",
    "SESSION_QUERY_INVALID_FILTER",
    "SESSION_QUERY_INVALID_LIMIT",
    "SESSION_QUERY_INVALID_QUERY",
    "SESSION_QUERY_INVALID_LINEAGE",
    "SESSION_QUERY_INVALID_SURFACE",
    "SESSION_QUERY_INVALID_WINDOW",
    "SESSION_QUERY_PERSISTENCE_FAILED",
    "SESSION_QUERY_SEARCH_DISABLED",
    "SESSION_QUERY_SESSION_NOT_FOUND",
    "SESSION_QUERY_STALE_CURSOR",
    "SESSION_QUERY_SOURCE_CONFLICT",
})

__all__ = [
    "SESSION_QUERY_DEFAULT_PERSISTED_INSPECT_CONCURRENCY",
    "SESSION_QUERY_DEFAULT_PREPARED_SESSION_CACHE_SIZE",
    "SESSION_QUERY_ERROR_CODES",
    "SESSION_QUERY_READ_WINDOW_MAX",
    "SessionQueryError",
]


class SessionQueryError(Exception):
    """类型化 session-query 失败：`code` 为闭集成员。"""

    def __init__(self, message: str, code: str, cause: BaseException | None = None):
        super().__init__(message)
        self.code = code
        if cause is not None:
            self.__cause__ = cause
