"""每会话文件上传收据（对齐 packages/client/file-upload/src/index.ts）。

上游 `FileUploads`（`ctx.fileUploads`）负责：
  * 上传落盘后为**接收会话**签收 `receiptId`（stage）；
  * prompt 里的 `{type:'file', receiptId}` 经 `bindPrompt` 绑定到 requestId，
    投递失败可回滚、成功 commit，随后在 `user/message` 被观察到或队列 remove
    时退休（retire）；
  * 仅普通会话可上传（子代理 → `subagent/attachment-invalid`）。

载体差异：上游以 `WeakMap<Session, Map<...>>` 直接键会话对象；mini 的 Session
生命周期由 `SessionStore` 管理，这里按 `sessionId` 记录，并在 `session/disposed`
时丢弃；receipt 在进程内有效（上游同）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from ..core.scope import Context, Service

__all__ = [
    "PromptFileBinding",
    "StagedFileUploads",
    "install_file_uploads",
]


@dataclass
class _StagedUpload:
    file: Any
    request_id: str | None = None


class PromptFileBinding:
    """一次 prompt 的 receipt 绑定（对齐上游 PromptFileBindingGuard）。

    `commit()` 保留 requestId（收据在投递/退休前不可复用）；`rollback()` 恢复
    绑定前的状态。两者幂等，首次调用生效。
    """

    def __init__(self, uploads: "StagedFileUploads", entries: list[tuple[_StagedUpload, str | None]]):
        self._uploads = uploads
        self._entries = entries
        self._settled = False

    def commit(self) -> None:
        if self._settled:
            return
        self._settled = True

    def rollback(self) -> None:
        if self._settled:
            return
        self._settled = True
        for upload, previous in self._entries:
            upload.request_id = previous

    def __enter__(self) -> "PromptFileBinding":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.commit()
        else:
            self.rollback()


class StagedFileUploads(Service):
    """`ctx.fileUploads`：会话级上传收据 + prompt 绑定/退休。"""

    provide = "fileUploads"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "fileUploads")
        self._staged: dict[str, dict[str, _StagedUpload]] = {}
        self._disposers: list[Any] = [
            ctx.on("session/event", self._observe_session_event, global_=True),
            ctx.on("session/disposed", self._on_disposed, global_=True),
        ]

    def stage(self, session_id: str, file: Any) -> dict:
        """为接收会话签收一个已落盘文件，返回 `{receiptId, file}`。"""
        receipt_id = str(uuid.uuid4())
        self._staged.setdefault(session_id, {})[receipt_id] = _StagedUpload(file)
        return {"receiptId": receipt_id, "file": file.to_dict()}

    def resolve(self, session_id: str, receipt_id: str) -> Any | None:
        """会话内 receipt → 持久文件引用；未知/外会话 → None。"""
        upload = self._staged.get(session_id, {}).get(receipt_id)
        return upload.file if upload is not None else None

    def bind_prompt(self, session_id: str, receipt_ids: list[str],
                    request_id: str) -> PromptFileBinding:
        """把 prompt 引用的 receipt 绑定到 requestId（缺失即 FILE_NOT_STAGED）。"""
        staged = self._staged.get(session_id, {})
        entries: list[tuple[_StagedUpload, str | None]] = []
        for receipt_id in receipt_ids:
            upload = staged.get(receipt_id)
            if upload is None:
                raise LookupError(receipt_id)
            entries.append((upload, upload.request_id))
        for upload, _previous in entries:
            upload.request_id = request_id
        return PromptFileBinding(self, entries)

    def retire_prompt(self, session_id: str, request_id: str) -> None:
        staged = self._staged.get(session_id)
        if staged is None:
            return
        for receipt_id in [key for key, upload in staged.items()
                           if upload.request_id == request_id]:
            del staged[receipt_id]
        if not staged:
            self._staged.pop(session_id, None)

    def discard(self, session_id: str) -> None:
        self._staged.pop(session_id, None)

    def _observe_session_event(self, payload: dict) -> None:
        event = payload.get("event")
        session = payload.get("session")
        session_id = getattr(session, "session_id", None)
        if session_id is None or not hasattr(event, "get"):
            return
        if event.get("type") != "user/message":
            return
        source = (event.get("data") or {}).get("source") or {}
        if source.get("kind") != "user":
            return
        request_id = source.get("rpcId")
        if isinstance(request_id, str):
            self.retire_prompt(session_id, request_id)

    def _on_disposed(self, payload: dict) -> None:
        session = payload.get("session")
        session_id = getattr(session, "session_id", None)
        if session_id is not None:
            self.discard(session_id)


def install_file_uploads(ctx: Context) -> StagedFileUploads:
    """幂等装配 `ctx.fileUploads`。"""
    existing = ctx.get("fileUploads")
    if existing is not None:
        return existing
    return StagedFileUploads(ctx)
