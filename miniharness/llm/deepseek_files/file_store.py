"""DeepSeek Files API 上传复用、失效与配额恢复。

对应 dsh 真实源码：packages/llm/llm-deepseek/src/common/file-store.ts。

载体差异：上游用 AbortController/共享 Promise；mini 用 asyncio 共享 task +
等待者计数（全等待者取消即取消共享上传），对齐「并发调用共享一次上传、各自
独立等待、无等待者时停止传输」的语义。
"""
from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

from ..protocol import INVALID_REQUEST, INVALID_RESPONSE, StreamAborted, LlmFailure
from ..deepseek_messages import messages_api_root
from .file_id import DeepSeekFileId
from .files_api import DeepSeekFilesClient, is_files_quota_error
from .upload_index import (
    DeepSeekUploadIndex,
    DeepSeekUploadRecord,
    deep_seek_file_scope,
)

__all__ = [
    "MAX_IMAGE_BYTES",
    "DeepSeekFileConnection",
    "DeepSeekFilePolicy",
    "DeepSeekFileReference",
    "DeepSeekFileStore",
]

#: 每张请求图（含 file-id 引用）共享的 Files-store 上限。
MAX_IMAGE_BYTES = 32 * 1024 * 1024
_OWNED_FILE_PREFIX = "dsh-"
_EXTENSIONS = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/webp": "webp",
    "image/gif": "gif",
}


@dataclass(frozen=True)
class DeepSeekFilePolicy:
    """从插件配置解析出的文件存储策略（上游 DeepSeekFilePolicy）。"""

    expiresAfterSeconds: int
    refreshMarginSeconds: int
    quotaCleanupBatch: int


@dataclass(frozen=True)
class DeepSeekFileConnection:
    """文件操作所需的连接事实（上游 DeepSeekFileConnection）。"""

    baseURL: str
    apiKey: str
    #: 使用 DSH 账户头（x-dsh-auth-token）；普通 API key 为 False。
    accountCredential: bool = False


@dataclass(frozen=True)
class DeepSeekFileReference:
    """一次 file-id 解析的结果（上游 DeepSeekFileReference）。"""

    record: DeepSeekUploadRecord
    uploaded: bool


def _file_scope(connection: DeepSeekFileConnection):
    # Files 资源的父 URL 标识上传命名空间（上游 fileScope → messagesApiRoot）。
    return deep_seek_file_scope(messages_api_root(connection.baseURL), connection.apiKey)


def _extension(media_type: str) -> str:
    extension = _EXTENSIONS.get(media_type)
    if extension is None:
        raise LlmFailure(INVALID_REQUEST,
                         f"DeepSeek does not support image media type {media_type!r}.")
    return extension


def _filename(version) -> str:
    attachment_id = str(version.attachment.attachmentId)
    variant_id = str(version.variantId)
    attachment = attachment_id[len("sha256:"):len("sha256:") + 16]
    variant = variant_id[len("sha256:"):len("sha256:") + 8]
    return f"{_OWNED_FILE_PREFIX}{attachment}-{variant}.{_extension(version.mediaType)}"


class _SharedUpload:
    def __init__(self) -> None:
        self.task: "asyncio.Task | None" = None
        self.waiters = 0
        self.settled = False


class DeepSeekFileStore:
    """DeepSeek 路由的用户级 durable file-id 复用。"""

    def __init__(self, *, index: DeepSeekUploadIndex | None = None,
                 now=None, transport=None) -> None:
        self._index = index if index is not None else DeepSeekUploadIndex()
        self._now = now if now is not None else (lambda: int(time.time() * 1000))
        self._transport = transport
        self._inflight: dict[str, _SharedUpload] = {}

    def _client(self, connection: DeepSeekFileConnection) -> DeepSeekFilesClient:
        return DeepSeekFilesClient(
            baseURL=connection.baseURL, apiKey=connection.apiKey,
            accountCredential=connection.accountCredential, transport=self._transport)

    async def ensure_uploaded(self, version, connection: DeepSeekFileConnection,
                              policy: DeepSeekFilePolicy, signal=None) -> DeepSeekFileReference:
        """解析或上传一张确定性请求图；并发调用共享一次上传（上游 ensureUploaded）。"""
        if signal is not None and getattr(signal, "aborted", False):
            raise StreamAborted("DeepSeek file upload cancelled")
        scope = _file_scope(connection)
        key = f"{scope}\0{version.variantId}"
        active = self._inflight.get(key)
        if active is not None and (active.task is not None and active.task.cancelled()):
            self._inflight.pop(key, None)
            active = None
        if active is None:
            active = _SharedUpload()
            active.task = asyncio.ensure_future(
                self._ensure_uploaded_once(version, connection, policy))
            self._inflight[key] = active

            def _settle(_task, shared=active, key=key):
                shared.settled = True
                if self._inflight.get(key) is shared:
                    self._inflight.pop(key, None)

            active.task.add_done_callback(_settle)
        return await self._wait(active, signal)

    async def _wait(self, shared: _SharedUpload, signal) -> DeepSeekFileReference:
        shared.waiters += 1
        try:
            if signal is None:
                return await asyncio.shield(shared.task)
            while True:
                if getattr(signal, "aborted", False):
                    raise StreamAborted("DeepSeek file upload cancelled")
                done, _pending = await asyncio.wait({shared.task}, timeout=0.05)
                if shared.task in done:
                    return shared.task.result()
        finally:
            shared.waiters -= 1
            if shared.waiters == 0 and not shared.settled and shared.task is not None:
                if not shared.task.done():
                    shared.task.cancel()

    async def _ensure_uploaded_once(self, version, connection: DeepSeekFileConnection,
                                    policy: DeepSeekFilePolicy) -> DeepSeekFileReference:
        if version.bytes > MAX_IMAGE_BYTES:
            raise LlmFailure(
                INVALID_REQUEST,
                "DeepSeek image exceeds the 32 MiB per-image limit.")
        scope = _file_scope(connection)
        now = self._now()
        margin_ms = policy.refreshMarginSeconds * 1_000
        cached = self._index.get(scope, str(version.variantId), now, margin_ms)
        if cached is not None:
            return DeepSeekFileReference(cached, False)

        client = self._client(connection)

        async def upload() -> DeepSeekUploadRecord:
            remote = await client.upload(
                data=version.data, mediaType=version.mediaType,
                filename=_filename(version),
                expiresAfterSeconds=policy.expiresAfterSeconds)
            if remote.bytes != len(version.data):
                raise LlmFailure(
                    INVALID_RESPONSE,
                    "DeepSeek Files API upload response does not match the submitted image.")
            return DeepSeekUploadRecord(
                scope=scope,
                attachmentId=str(version.attachment.attachmentId),
                variantId=str(version.variantId),
                fileId=remote.id,
                bytes=remote.bytes,
                createdAt=remote.createdAt * 1_000,
                expiresAt=remote.expiresAt * 1_000,
            )

        try:
            candidate = await upload()
        except LlmFailure as error:
            if not is_files_quota_error(error):
                raise
            deleted = await self.reclaim_oldest_owned(
                connection, policy.quotaCleanupBatch, None)
            if deleted == 0:
                raise
            candidate = await upload()

        committed = self._index.commit(candidate, self._now(), margin_ms)
        if not committed.accepted:
            try:
                await client.delete(candidate.fileId)
            except LlmFailure:
                # The winning mapping is durable. A failed duplicate cleanup affects
                # quota only and is retried by recovery.
                pass
        return DeepSeekFileReference(committed.record, committed.accepted)

    async def invalidate(self, version, file_id: DeepSeekFileId,
                         connection: DeepSeekFileConnection) -> None:
        """一条模型请求拒绝其远程 id 后使该精确本地映射失效（上游 invalidate）。"""
        self._index.remove(_file_scope(connection), str(version.variantId), file_id)

    async def release(self, version, connection: DeepSeekFileConnection,
                      policy: DeepSeekFilePolicy, signal=None) -> bool:
        """删除某附件的已索引远程文件并移除本地映射（上游 release）。"""
        scope = _file_scope(connection)
        record = self._index.get(scope, str(version.variantId), self._now(),
                                 policy.refreshMarginSeconds * 1_000)
        if record is None:
            return False
        await self._client(connection).delete(record.fileId, signal)
        self._index.remove(scope, str(version.variantId), record.fileId)
        return True

    async def reclaim_oldest_owned(self, connection: DeepSeekFileConnection,
                                   count: int, signal=None) -> int:
        """删除文件名标识 harness 自有的最旧 provider 文件（上游 reclaimOldestOwned）。"""
        client = self._client(connection)
        after: DeepSeekFileId | None = None
        owned: list[tuple[DeepSeekFileId, int]] = []
        while True:
            page = await client.list(after=after, limit=1_000, signal=signal)
            for file in page.data:
                if not file.filename.startswith(_OWNED_FILE_PREFIX):
                    continue
                owned.append((file.id, file.createdAt))
            # Messages 无升序查询；跨页保留最旧候选。
            owned.sort(key=lambda entry: entry[1])
            owned = owned[:count]
            if not page.hasMore or page.lastId is None or page.lastId == after:
                break
            after = page.lastId
        for file_id, _created in owned:
            await client.delete(file_id, signal)
        return len(owned)

    async def release_all(self, connection: DeepSeekFileConnection, signal=None) -> int:
        """删除当前 API-key 命名空间内全部 harness 自有的远程文件并清空索引（上游 releaseAll）。"""
        total = 0
        while True:
            deleted = await self.reclaim_oldest_owned(connection, 1_000, signal)
            total += deleted
            if deleted < 1_000:
                break
        self._index.clear(_file_scope(connection))
        return total
