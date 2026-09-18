"""durable DeepSeek attachment→file-id 索引。

对应 dsh 真实源码：packages/llm/llm-deepseek/src/common/upload-index.ts。

上游用 `withFileLock` + `writeFileAtomic`（dsh-atomic-write）；mini 用 filelock
（跨进程写锁，credentials-local 同款）+ 临时文件 `os.replace` 原子发布。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass

from filelock import FileLock, Timeout

from ...core.home_paths import resolve_dsh_home
from .file_id import DeepSeekFileId, DeepSeekFileScope

__all__ = [
    "DeepSeekUploadIndex",
    "DeepSeekUploadRecord",
    "UploadIndexCommit",
    "deep_seek_file_scope",
]

#: 写锁等待上限（对齐上游 atomic-write 的 30s 纪律）。
UPLOAD_INDEX_LOCK_WAIT_SECONDS = 30.0

_FORMAT_VERSION = 3


@dataclass(frozen=True)
class DeepSeekUploadRecord:
    """一条 durable 远程上传映射；Unix 时间毫秒。"""

    scope: DeepSeekFileScope
    attachmentId: str
    variantId: str
    fileId: DeepSeekFileId
    bytes: int
    createdAt: int
    expiresAt: int


@dataclass(frozen=True)
class UploadIndexCommit:
    """另一进程已发布可复用上传时的候选提交结果（上游 UploadIndexCommit）。"""

    record: DeepSeekUploadRecord
    accepted: bool


class InvalidUploadIndexError(Exception):
    """索引不是可识别的 files-v3 文档（读作空，绝不搞垮整个索引）。"""


def deep_seek_file_scope(baseURL: str, apiKey: str) -> DeepSeekFileScope:
    """派生一个不持久化/不记录 API key 的稳定索引命名空间摘要（上游同名函数）。"""
    digest = hashlib.sha256()
    digest.update(baseURL.rstrip("/").encode("utf-8"))
    digest.update(b"\0")
    digest.update(apiKey.encode("utf-8"))
    return DeepSeekFileScope(digest.hexdigest())


_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SCOPE_HEX_RE = re.compile(r"^[0-9a-f]{64}$")


def _safe_non_negative(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _parse_record(value) -> DeepSeekUploadRecord:
    if not isinstance(value, dict):
        raise InvalidUploadIndexError(
            "llm-deepseek: upload index contains a non-object record")
    scope = value.get("scope")
    attachment_id = value.get("attachmentId")
    variant_id = value.get("variantId")
    file_id = value.get("fileId")
    if (not isinstance(scope, str) or not _SCOPE_HEX_RE.match(scope)
            or not isinstance(attachment_id, str) or not _SHA256_RE.match(attachment_id)
            or not isinstance(variant_id, str) or not _SHA256_RE.match(variant_id)
            or not isinstance(file_id, str) or len(file_id) == 0
            or not _safe_non_negative(value.get("bytes"))
            or not _safe_non_negative(value.get("createdAt"))
            or not _safe_non_negative(value.get("expiresAt"))):
        raise InvalidUploadIndexError(
            "llm-deepseek: upload index contains an invalid record")
    return DeepSeekUploadRecord(
        scope=DeepSeekFileScope(scope),
        attachmentId=attachment_id,
        variantId=variant_id,
        fileId=DeepSeekFileId(file_id),
        bytes=value["bytes"],
        createdAt=value["createdAt"],
        expiresAt=value["expiresAt"],
    )


def _parse_index(text: str) -> dict:
    try:
        value = json.loads(text)
    except ValueError as error:
        raise InvalidUploadIndexError(
            "llm-deepseek: upload index is not valid JSON") from error
    if not isinstance(value, dict):
        raise InvalidUploadIndexError("llm-deepseek: upload index is not an object")
    if value.get("formatVersion") != _FORMAT_VERSION or not isinstance(value.get("records"), list):
        raise InvalidUploadIndexError(
            "llm-deepseek: unsupported upload index format")
    records = [_parse_record(record) for record in value["records"]]
    keys = set()
    for record in records:
        key = f"{record.scope}\0{record.variantId}"
        if key in keys:
            raise InvalidUploadIndexError(
                "llm-deepseek: upload index contains duplicate mappings")
        keys.add(key)
    return {"formatVersion": _FORMAT_VERSION, "records": records}


def _reusable(record: DeepSeekUploadRecord, now: int, refresh_margin_ms: int) -> bool:
    return record.expiresAt - now > refresh_margin_ms


def _record_to_wire(record: DeepSeekUploadRecord) -> dict:
    return {
        "scope": str(record.scope),
        "attachmentId": record.attachmentId,
        "variantId": record.variantId,
        "fileId": str(record.fileId),
        "bytes": record.bytes,
        "createdAt": record.createdAt,
        "expiresAt": record.expiresAt,
    }


class DeepSeekUploadIndex:
    """本 DSH home 内每个 DeepSeek 会话共享的原子本地索引。"""

    def __init__(self, path: str | None = None) -> None:
        self.path = path if path is not None else os.path.join(
            resolve_dsh_home(), "llm-deepseek", "files-v3.json")

    def _load(self) -> dict:
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                return _parse_index(handle.read())
        except FileNotFoundError:
            return {"formatVersion": _FORMAT_VERSION, "records": []}
        except InvalidUploadIndexError:
            return {"formatVersion": _FORMAT_VERSION, "records": []}

    def _save(self, index: dict) -> None:
        directory = os.path.dirname(self.path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        temporary = f"{self.path}.{uuid.uuid4().hex}.tmp"
        try:
            fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {"formatVersion": _FORMAT_VERSION,
                     "records": [_record_to_wire(r) for r in index["records"]]},
                    handle, indent=2)
                handle.write("\n")
            os.replace(temporary, self.path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _lock(self) -> FileLock:
        os.makedirs(os.path.dirname(self.path), mode=0o700, exist_ok=True)
        return FileLock(f"{self.path}.lock", timeout=UPLOAD_INDEX_LOCK_WAIT_SECONDS)

    def get(self, scope: DeepSeekFileScope, variantId: str,
            now: int, refresh_margin_ms: int) -> DeepSeekUploadRecord | None:
        """读取一条可复用映射（上游 get）。"""
        record = next((candidate for candidate in self._load()["records"]
                       if candidate.scope == scope and candidate.variantId == variantId), None)
        if record is not None and _reusable(record, now, refresh_margin_ms):
            return record
        return None

    def commit(self, candidate: DeepSeekUploadRecord, now: int,
               refresh_margin_ms: int) -> UploadIndexCommit:
        """发布一次完成的上传，除非另一进程已发布可复用映射（上游 commit）。"""
        with self._lock():
            index = self._load()
            existing = next((record for record in index["records"]
                             if record.scope == candidate.scope
                             and record.variantId == candidate.variantId
                             and _reusable(record, now, refresh_margin_ms)), None)
            if existing is not None:
                return UploadIndexCommit(existing, False)
            records = [record for record in index["records"]
                       if _reusable(record, now, refresh_margin_ms)
                       and not (record.scope == candidate.scope
                                and record.variantId == candidate.variantId)]
            records.append(candidate)
            self._save({"formatVersion": _FORMAT_VERSION, "records": records})
            return UploadIndexCommit(candidate, True)

    def remove(self, scope: DeepSeekFileScope, variantId: str, fileId: DeepSeekFileId) -> None:
        """移除一条精确映射，不删除并发安装的后继（上游 remove）。"""
        with self._lock():
            index = self._load()
            records = [record for record in index["records"] if not (
                record.scope == scope and record.variantId == variantId
                and record.fileId == fileId)]
            if len(records) != len(index["records"]):
                self._save({"formatVersion": _FORMAT_VERSION, "records": records})

    def clear(self, scope: DeepSeekFileScope) -> None:
        """移除一个远程命名空间的全部本地映射（上游 clear）。"""
        with self._lock():
            index = self._load()
            records = [record for record in index["records"] if record.scope != scope]
            if len(records) != len(index["records"]):
                self._save({"formatVersion": _FORMAT_VERSION, "records": records})
