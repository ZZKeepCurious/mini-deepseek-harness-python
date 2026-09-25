"""Workspace Remote 控制器（对齐 packages/api/workspace-controller）。

承载 `ctx.workspaceController`：`create` / `initializeDefault` / `rename` / `delete` /
`insertBefore` / `insertSessionBefore` / `archiveSession` / `unarchiveSession` /
`pinSession` / `unpinSession` + `follow` 流。变更正确性依赖当前注册表状态，因此命令在
控制器内串行化；预期失败抛带稳定 `workspace/*` 码的 `WorkspaceFault`。

`follow` 契约（对齐上游 feed）：同步附着到持久工作区变更，先发一条完整
`baseline`，随后发有序 `upsert` / `remove` / `order` / `archived` / `pinned` 增量；
重连开新代次并重发 baseline，消费者不依赖断连期间的每条增量。

载体差异（登记）：
  * 上游域变更事件来自 storage-domain `domain/changed`；mini 以自有
    `workspace/changed` ctx 事件承载（payload 形状由本模块投影为 wire 帧）。
  * 上游 `DirectoryPickerController` 仅在组合了选择后端时挂载；mini 无该后端
    （无原生/浏览选择器），故不注册 `directoryPicker` namespace（如实缺席）。
  * 上游 `create`/`rename` 等异步；mini 注册表本体同步（`*_now`），控制器直调；
    归档/置顶/默认初始化等在服务上异步，控制器经常驻事件循环
    （`run_on_resident`）同步驱动，对 WebApi 同步派发面等价。
  * 上游首用目录解析经原生命令查系统 Documents（macOS/win32/linux 三路）；mini
    无原生命令面，取配置 `documentsDirectory` 或 `~/Documents`（载体简化）。
"""
from __future__ import annotations

import os
import threading
from collections import deque
from typing import Any

from ..core.agent_loop.resident_loop import run_on_resident
from ..core.scope import Context, Service
from ..workspace import (
    WorkspaceActiveSessionError,
    WorkspaceArchivedSessionPinError,
    WorkspaceMoveInvalidError,
    WorkspaceOrderInvalidError,
    WorkspaceUnknownSessionError,
    fully_qualified_workspace_path,
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


def _activity_view(activity: list) -> list[dict]:
    """把注册表的活动条目投影为其 Remote 值（feed/commands 的 wire 形状）。"""
    view: list[dict] = []
    for entry in activity:
        item: dict[str, Any] = {"kind": entry.kind}
        if entry.items:
            item["items"] = [
                {"id": item_.id, **({"label": item_.label} if item_.label is not None else {})}
                for item_ in entry.items
            ]
        view.append(item)
    return view


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
    if kind == "pinned":
        return {"type": "pinned", "pinnedSessionIds": list(payload["pinnedSessionIds"])}
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

    def __init__(self, ctx: Context, config: dict | None = None):
        super().__init__(ctx, "workspaceController")
        self._tail = threading.Lock()
        self._documents_directory = (config or {}).get("documentsDirectory")

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

    def initialize_default(self, request: dict) -> dict | None:
        """首用启动时初始化或复用默认工作区（对齐上游 initializeDefault）。

        校验目录名与标题（空白/分隔符/冒号/NUL/首尾空白/尾点拒绝），经注册表的
        `initialize_default` 在注册表与会话历史皆空时创建；不创建会话或消息。
        返回 `{workspace}` 或 None（不符合自动创建条件）。
        """
        directory_name = request.get("directoryName")
        title = request.get("title")
        if (not isinstance(directory_name, str) or directory_name.strip() == ""
                or directory_name != directory_name.strip()
                or directory_name.endswith(".")
                or "/" in directory_name or "\\" in directory_name
                or ":" in directory_name or "\0" in directory_name
                or not isinstance(title, str) or title.strip() == ""):
            raise WorkspaceFault(
                "gateway/bad-request",
                "default Workspace requires a directory name and non-blank title", {})
        with self._tail:
            async def resolve_directory() -> dict:
                return self._resolve_default_directory(directory_name, title)

            workspace = run_on_resident(
                self._registry().initialize_default(resolve_directory))
        return None if workspace is None else {"workspace": workspace_view(workspace)}

    def _resolve_default_directory(self, directory_name: str, title: str) -> dict:
        """解析首用目录：`<documents>/deepseek-harness/<name>`（对齐上游的末段拼接）。

        载体简化：上游经原生命令查系统 Documents；mini 取配置 `documentsDirectory`
        或 `~/Documents`。
        """
        base = self._documents_directory or os.path.join(
            os.path.expanduser("~"), "Documents")
        if not fully_qualified_workspace_path(base):
            raise WorkspaceFault(
                "gateway/bad-request",
                f"Documents directory must be fully qualified: '{base}'", {})
        return {"path": os.path.join(os.path.normpath(base), "deepseek-harness", directory_name),
                "title": title}

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
        """归档一个已知会话（对齐上游 archiveSession）。

        无 `stopActivity` 时运行中的会话以 `workspace/session-active` 连同活动详情
        被拒；带它则先由 provider 停止该工作。
        """
        session_id = request.get("sessionId")
        options = {"stopActivity": True} if request.get("stopActivity") is True else {}
        try:
            run_on_resident(self._registry().archive_session(session_id, options))
        except WorkspaceUnknownSessionError as error:
            raise WorkspaceFault("session/not-found", str(error),
                                 {"sessionId": session_id}) from error
        except WorkspaceActiveSessionError as error:
            raise WorkspaceFault(
                "workspace/session-active", str(error),
                {"sessionId": session_id, "activity": _activity_view(error.activity)}) from error
        return {"archivedSessionIds": self._registry().archivedSessionIds}

    def unarchive_session(self, request: dict) -> dict:
        self._registry().unarchive_session(request.get("sessionId"))
        return {"archivedSessionIds": self._registry().archivedSessionIds}

    def pin_session(self, request: dict) -> dict:
        """把已知未归档会话置顶到未置顶会话之前（对齐上游 pinSession）。"""
        session_id = request.get("sessionId")
        with self._tail:
            try:
                run_on_resident(self._registry().pin_session(session_id))
            except WorkspaceUnknownSessionError as error:
                raise WorkspaceFault("session/not-found", str(error),
                                     {"sessionId": session_id}) from error
            except WorkspaceArchivedSessionPinError as error:
                raise WorkspaceFault("gateway/bad-request", str(error), {}) from error
        return {"pinnedSessionIds": self._registry().pinnedSessionIds}

    def unpin_session(self, request: dict) -> dict:
        """丢弃一个会话的置顶而不改动其保存顺序（对齐上游 unpinSession）。"""
        session_id = request.get("sessionId")
        with self._tail:
            run_on_resident(self._registry().unpin_session(session_id))
        return {"pinnedSessionIds": self._registry().pinnedSessionIds}

    def follow(self) -> WorkspaceFollow:
        return WorkspaceFollow(self)

    # ---------- 读面 ----------

    def baseline(self) -> dict:
        registry = self._registry()
        return {"items": [workspace_view(workspace) for workspace in registry.list()],
                "archivedSessionIds": registry.archivedSessionIds,
                "pinnedSessionIds": registry.pinnedSessionIds}

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


def install_workspace_controller(ctx: Context,
                                 config: dict | None = None) -> WorkspaceController:
    """幂等装配 `ctx.workspaceController`（`documentsDirectory` 覆盖首用目录）。"""
    existing = ctx.get("workspaceController")
    if existing is not None:
        return existing
    return WorkspaceController(ctx, config)
