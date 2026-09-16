"""Storage hub + backend 的错误词汇（对齐 upstream 两个错误模块）。

上游对照：packages/storage/storage/src/error.ts（StorageError）+
packages/storage/storage-domain/src/error.ts（DomainError）。

契约：code 是稳定判别符（消费者 switch 的目标）；message 是诊断散文。后端
失败（backend-not-found / version-mismatch / …）以 StorageError 原样穿透，
domain 层不重新包装。
"""
from __future__ import annotations

from typing import Any

__all__ = [
    "StorageError",
    "StorageErrorCode",
    "DomainError",
    "DomainErrorCode",
    "InvalidRecordDetail",
]

#: StorageError 的稳定判别码闭集（对齐 upstream StorageErrorCode）。
STORAGE_ERROR_CODES = frozenset({
    "backend-not-found",
    "form-not-mounted",
    "duplicate-backend",
    "duplicate-mount",
    "version-mismatch",
    "malformed-medium",
    "closed",
})

#: DomainError 的稳定判别码闭集（对齐 upstream DomainErrorCode）。
DOMAIN_ERROR_CODES = frozenset({
    "already-open",
    "facet-unsupported",
    "invalid-record",
    "missing-key",
    "closed",
})

StorageErrorCode = str
DomainErrorCode = str


class InvalidRecordDetail(dict):
    """schema 校验失败记录的位置（上游 InvalidRecordDetail：table/key，'' = global）。"""

    table: str
    key: str


class StorageError(Exception):
    """hub 与 backend 抛出的错误。code 为稳定契约，message 为诊断散文。"""

    name = "StorageError"

    def __init__(self, code: StorageErrorCode, message: str, *, cause: Any = None):
        super().__init__(message)
        self.code = code
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause if isinstance(cause, BaseException) else None


class DomainError(Exception):
    """domain 层抛出的错误。code 为稳定契约；detail 恰在 code==='invalid-record' 时存在。"""

    name = "DomainError"

    def __init__(self, code: DomainErrorCode, message: str, *,
                 detail: InvalidRecordDetail | None = None, cause: Any = None):
        super().__init__(message)
        self.code = code
        self.detail = detail
        self.cause = cause
        if cause is not None:
            self.__cause__ = cause if isinstance(cause, BaseException) else None