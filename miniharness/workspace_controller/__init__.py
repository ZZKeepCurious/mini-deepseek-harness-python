"""Workspace Remote 控制器（对齐 packages/api/workspace-controller）。

承载 `ctx.workspaceController`：`create` / `rename` / `delete` / `insertBefore` /
`insertSessionBefore` / `archiveSession` / `unarchiveSession` + `follow` 流。变更
正确性依赖当前注册表状态，因此命令在控制器内串行化；预期失败抛带稳定
`workspace/*` 码的 `WorkspaceFault`。

`follow` 契约（对齐上游 feed）：同步附着到持久工作区变更，先发一条完整
`baseline`，随后发有序 `upsert` / `remove` / `order` / `archived` 增量；重连开
新代次并重发 baseline，消费者不依赖断连期间的每条增量。

载体差异（登记）：
  * 上游域变更事件来自 storage-domain `domain/changed`；mini 以自有
    `workspace/changed` ctx 事件承载（payload 形状由本模块投影为 wire 帧）。
  * 上游 `DirectoryPickerController` 仅在组合了选择后端时挂载；mini 无该后端
    （无原生/浏览选择器），故不注册 `directoryPicker` namespace（如实缺席）。
  * 上游 `create`/`rename` 等异步；mini 注册表本体同步（`*_now`），控制器直调。
"""
from __future__ import annotations

import threading
from collections import deque
from typing import Any

from ..core.scope import Context, Service
from ..workspace import (
    WorkspaceMoveInvalidError,
    WorkspaceOrderInvalidError,
    WorkspaceUnknownSessionError,
)

__all__ = [
    "WorkspaceController",
    "WorkspaceFault",
    "WorkspaceFollow",
    "install_workspace_controller",
    "workspace_view",
]


class WorkspaceFault(Exception):
    """带稳定 `workspace/*` 码的控制器失败（对齐上游 RemoteError）。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def workspace_view(workspace: Any) -> dict:
    """把一个权威工作区实体投影为其 Remote 值（feed.ts workspaceView）。"""
    return {
        "workspaceId": workspace.id,
        "path": workspace.path,
        "title": workspace.title,
        "sessionIds": list(workspace.sessionIds),
        "createdAt": workspace.createdAt,
        "updatedAt": workspace.updatedAt,
    }


def _frame_of(payload: dict) -> dict | None:
    kind = payload.get("kind")
    if kind == "upsert":
        return {"type": "upsert", "workspace": workspace_view(payload["workspace"])}
    if kind == "remove":
        return {"type": "remove", "workspaceId": payload["workspaceId"]}
    if kind == "order":
        return {"type": "order", "workspaceIds": list(payload["workspaceIds"])}
    if kind == "archived":
        return {"type": "archived", "archivedSessionIds": list(payload["archivedSessionIds"])}
    return None


class WorkspaceFollow:
    """一条 follow 代次：baseline + 有序增量（线程安全的非阻塞 pop）。"""

    def __init__(self, controller: "WorkspaceController"):
        self.baseline = {"type": "baseline", "value": controller.baseline()}
        self._queue: deque[dict] = deque()
        self._lock = threading.Lock()
        self._closed = False
        self._dispose = controller.ctx.on("workspace/changed", self._on_change)

    def _on_change(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        frame = _frame_of(payload)
        if frame is None:
            return
        with self._lock:
            if self._closed:
                return
            self._queue.append(frame)

    def pop(self) -> dict | None:
        with self._lock:
            return self._queue.popleft() if self._queue else None

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._queue.clear()
        if self._dispose is not None:
            self._dispose()
            self._dispose = None


class WorkspaceController(Service):
    """Host 工作区业务 API 与 Remote namespace 属主（index.ts WorkspaceController）。"""

    provide = "workspaceController"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "workspaceController")
        self._tail = threading.Lock()

    # ---------- 远程方法 ----------

    def create(self, request: dict) -> dict:
        path = request.get("path")
        if not isinstance(path, str) or not path:
            raise WorkspaceFault("workspace/invalid-path",
                                 f'cannot create a Workspace at "{path}": path is required',
                                 {"path": path})
        with self._tail:
            try:
                existing = self._registry().resolve_by_path(path)
                if existing is not None:
                    return {"workspace": workspace_view(existing), "created": False}
                workspace = self._registry().create_now(path)
                return {"workspace": workspace_view(workspace), "created": True}
            except WorkspaceFault:
                raise
            except Exception as error:  # noqa: BLE001 - 折 workspace/invalid-path
                raise WorkspaceFault(
                    "workspace/invalid-path",
                    f'cannot create a Workspace at "{path}": {error}',
                    {"path": path}) from error

    def rename(self, request: dict) -> dict:
        workspace_id = request.get("workspaceId")
        raw_title = request.get("title")
        title = raw_title.strip() if isinstance(raw_title, str) else ""
        if title == "":
            raise WorkspaceFault("gateway/bad-request",
                                 "Workspace rename requires a non-blank title", {})
        with self._tail:
            workspace = self._require_workspace(workspace_id)
            if title != workspace.title:
                registry = self._registry()
                if any(candidate.id != workspace.id and candidate.title == title
                       for candidate in registry.list()):
                    raise WorkspaceFault("workspace/name-conflict",
                                         f"Workspace name '{title}' is already in use",
                                         {"name": title})
                workspace.set_title_now(title)
            return {"workspace": workspace_view(workspace)}

    def delete(self, request: dict) -> dict:
        workspace_id = request.get("workspaceId")
        with self._tail:
            if not self._registry().delete(workspace_id):
                raise self._not_found(workspace_id)
            return {"deleted": True}

    def insert_before(self, request: dict) -> dict:
        try:
            workspace_ids = self._registry().insert_before(
                request.get("workspaceId"), request.get("beforeWorkspaceId"))
        except WorkspaceOrderInvalidError as error:
            raise self._not_found(error.workspace_id) from error
        return {"workspaceIds": list(workspace_ids)}

    def insert_session_before(self, request: dict) -> dict:
        workspace = self._require_workspace(request.get("workspaceId"))
        try:
            workspace.insert_session_before_now(
                request.get("sessionId"), request.get("beforeSessionId"))
        except WorkspaceMoveInvalidError as error:
            details = {"workspaceId": request.get("workspaceId"),
                       "sessionId": request.get("sessionId")}
            before = request.get("beforeSessionId")
            if before is not None:
                details["beforeSessionId"] = before
            raise WorkspaceFault("workspace/move-invalid", str(error), details) from error
        return {"workspace": workspace_view(workspace)}

    def archive_session(self, request: dict) -> dict:
        session_id = request.get("sessionId")
        try:
            self._registry().archive_session(session_id)
        except WorkspaceUnknownSessionError as error:
            raise WorkspaceFault("session/not-found", str(error),
                                 {"sessionId": session_id}) from error
        return {"archivedSessionIds": self._registry().archivedSessionIds}

    def unarchive_session(self, request: dict) -> dict:
        self._registry().unarchive_session(request.get("sessionId"))
        return {"archivedSessionIds": self._registry().archivedSessionIds}

    def follow(self) -> WorkspaceFollow:
        return WorkspaceFollow(self)

    # ---------- 读面 ----------

    def baseline(self) -> dict:
        registry = self._registry()
        return {"items": [workspace_view(workspace) for workspace in registry.list()],
                "archivedSessionIds": registry.archivedSessionIds}

    # ---------- 内部 ----------

    def _registry(self):
        registry = self.ctx.get("workspaces")
        if registry is None:
            raise WorkspaceFault(
                "gateway/internal",
                "workspace service is absent: this deployment does not mount a workspace registry",
                {})
        return registry

    def _require_workspace(self, workspace_id: Any):
        workspace = self._registry().get(workspace_id)
        if workspace is None:
            raise self._not_found(workspace_id)
        return workspace

    @staticmethod
    def _not_found(workspace_id: Any) -> WorkspaceFault:
        return WorkspaceFault("workspace/not-found",
                              f'Workspace "{workspace_id}" not found',
                              {"workspaceId": workspace_id})


def install_workspace_controller(ctx: Context) -> WorkspaceController:
    """幂等装配 `ctx.workspaceController`。"""
    existing = ctx.get("workspaceController")
    if existing is not None:
        return existing
    return WorkspaceController(ctx)
