"""工作区域（对齐 packages/workspace/workspace）。

- `paths`：全限定路径校验、默认标题、realpath 规范化（唯一性 canon）；
- `Workspace`：稳定 uuid 记录 + 目录路径 + 标题 + 有序会话账户；标题替换、会话 attach/
  insertBefore/detach、`status()` 实时目录检查；
- `WorkspaceService`（`ctx.workspaces`）：create/list/get/remove + 持久化注册表。

载体差异（登记）：上游经 `ctx.storage.domain`（storage-domain 表 + 双写恢复标记 + 单写链）
持久化，并把会话 header-validated 账户过滤；mini 以 JSON 注册表 + `os.replace` 原子发布承载
（无 pendingMutation 恢复标记、无归档集、无 typert RPC 投影）。
"""
from __future__ import annotations

import json
import os
import pathlib
import uuid
from typing import Any

from ..core.scope import Context, Service

__all__ = [
    "Workspace",
    "WorkspaceService",
    "default_workspace_title",
    "fully_qualified_workspace_path",
    "install_workspaces",
    "realpath_normalize",
]


def fully_qualified_workspace_path(path: str) -> bool:
    """路径是否命名一个固定 Host 位置（不依赖进程 cwd/当前盘）。"""
    if os.name != "nt":
        return os.path.isabs(path)
    drive, _ = os.path.splitdrive(path)
    return bool(drive) and os.path.isabs(path)


def default_workspace_title(path: str) -> str:
    """从 canonical 路径派生非空默认标题：末段，否则根拼写。"""
    base = os.path.basename(path)
    if base:
        return base
    drive, tail = os.path.splitdrive(path)
    return (drive + tail) or path


def realpath_normalize(path: str) -> str:
    """realpath 规范化（尾斜杠/`..`/符号链接全解析）；相对路径或不存在 → 抛错。"""
    if not fully_qualified_workspace_path(path):
        raise TypeError(f"Workspace path is not fully qualified: '{path}'")
    return os.path.realpath(path)


def _now_iso() -> str:
    import datetime
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


class Workspace:
    """一个工作区记录（对齐上游 Workspace 消费者接口）。"""

    def __init__(self, service: "WorkspaceService", record: dict):
        self._service = service
        self.id = record["id"]
        self.path = record["path"]
        self._record = record

    @property
    def title(self) -> str:
        return self._record["title"]

    @property
    def createdAt(self) -> str:
        return self._record["createdAt"]

    @property
    def updatedAt(self) -> str:
        return self._record["updatedAt"]

    @property
    def sessionIds(self) -> list:
        return list(self._record["sessionIds"])

    async def set_title(self, title: str) -> None:
        self._record["title"] = title
        self._service._mutate(self._record)

    async def attach_session(self, session_id: str) -> None:
        if session_id in self._record["sessionIds"]:
            return
        cwd = self._service._session_cwd(session_id)
        if cwd is None:
            raise ValueError(f"unknown session: {session_id}")
        canonical = realpath_normalize(cwd)
        if canonical != self.path:
            raise ValueError(
                f"session {session_id} cwd does not match workspace {self.path}")
        self._record["sessionIds"].insert(0, session_id)
        self._service._mutate(self._record)

    async def insert_session_before(self, session_id: str, before_session_id: str | None = None) -> None:
        ids = self._record["sessionIds"]
        if session_id not in ids:
            raise ValueError(f"session {session_id} is not accounted in this workspace")
        if before_session_id is not None and before_session_id not in ids:
            raise ValueError(f"anchor {before_session_id} is not accounted in this workspace")
        ids.remove(session_id)
        if before_session_id is None:
            ids.append(session_id)
        else:
            ids.insert(ids.index(before_session_id), session_id)
        self._service._mutate(self._record)

    async def detach_session(self, session_id: str) -> None:
        ids = self._record["sessionIds"]
        if session_id not in ids:
            return
        ids.remove(session_id)
        self._service._mutate(self._record)

    def status(self) -> str:
        return "ok" if os.path.isdir(self.path) else "missing-dir"


class WorkspaceService(Service):
    """工作区注册表（`ctx.workspaces`）。"""

    provide = "workspaces"

    def __init__(self, ctx: Context, *, root: str | None = None):
        super().__init__(ctx, "workspaces")
        base = root or os.path.join(os.path.expanduser("~"), ".miniharness")
        self._file = os.path.join(base, "workspaces.json")
        self._records: dict = {}
        self._load()

    # ---------- 持久化 ----------

    def _load(self) -> None:
        try:
            raw = pathlib.Path(self._file).read_text(encoding="utf-8")
        except FileNotFoundError:
            return
        data = json.loads(raw)
        self._records = {record["id"]: record for record in data.get("workspaces", [])}

    def _flush(self) -> None:
        os.makedirs(os.path.dirname(self._file), exist_ok=True)
        payload = json.dumps({"workspaces": list(self._records.values())},
                             ensure_ascii=False, indent=2)
        tmp = self._file + ".tmp"
        pathlib.Path(tmp).write_text(payload, encoding="utf-8")
        os.replace(tmp, self._file)

    def _mutate(self, record: dict) -> None:
        record["updatedAt"] = _now_iso()
        self._flush()

    def _session_cwd(self, session_id: str) -> str | None:
        store = self.ctx.get("sessions")
        session = store.get(session_id) if store is not None else None
        if session is None:
            return None
        return (getattr(session, "meta", {}) or {}).get("cwd")

    # ---------- 公共面 ----------

    async def create(self, path: str, title: str | None = None) -> Workspace:
        canonical = realpath_normalize(path)
        if not os.path.isdir(canonical):
            raise FileNotFoundError(canonical)
        for record in self._records.values():
            if record["path"] == canonical:
                raise ValueError(f"workspace already exists for {canonical}")
        record = {
            "id": uuid.uuid4().hex,
            "path": canonical,
            "title": title if title is not None else default_workspace_title(canonical),
            "sessionIds": [],
            "createdAt": _now_iso(),
            "updatedAt": _now_iso(),
        }
        self._records[record["id"]] = record
        self._flush()
        return Workspace(self, record)

    def list(self) -> list:
        ordered = sorted(self._records.values(), key=lambda r: r["createdAt"])
        return [Workspace(self, record) for record in ordered]

    def get(self, workspace_id: str) -> Workspace | None:
        record = self._records.get(workspace_id)
        return Workspace(self, record) if record is not None else None

    async def remove(self, workspace_id: str) -> None:
        if self._records.pop(workspace_id, None) is None:
            raise KeyError(workspace_id)
        self._flush()


def install_workspaces(ctx: Context, *, root: str | None = None) -> WorkspaceService:
    """幂等装配工作区注册表（`ctx.workspaces`）。"""
    existing = ctx.get("workspaces")
    if existing is not None:
        return existing
    return WorkspaceService(ctx, root=root)
