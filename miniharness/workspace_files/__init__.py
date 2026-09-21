"""Workspace 文件 Remote 服务（对齐 packages/api/workspace-files）。

承载 `ctx.workspaceFiles`：只读文件预览（`read` / `readBytes` / `readAll` /
`readRelated` / `stat`）、工作区内的目录列举（`list`）与经 `fs/observed` 的观察
变更流（`changes`）。文件读取遵循组合文件系统的读权限（可读工作区外路径）；
目录列举与变更观察限定在会话工作区根内。本服务**不含任何变更操作**。

载体差异（登记）：
  * 上游 `ctx.fs` 的方法为 Promise；mini 的 fs seam 本体为 async（多数同步体），
    本控制器经常驻事件循环（`run_on_resident`）同步驱动，对 WebApi 同步派发面等价。
  * 上游 lookup `workspaceFileScope` 可回退持久化 `stat` 解析冷会话 header；mini
    无会话持久化 stat 面，scope 由 web 层从 `ctx.sessions` 或 sandboxPolicy 回退根派生。
  * `changes` 只转发 `fs/observed`（受插桩操作）；OS 不监听（上游同款限制）。
"""
from __future__ import annotations

import base64
import os
import posixpath
import ntpath
import threading
from collections import deque
from typing import Any

from ..core.agent_loop.resident_loop import run_on_resident
from ..core.scope import Context, Service
from ..fs import FsError, is_path_under

__all__ = [
    "WORKSPACE_FILE_CONFIG_DEFAULTS",
    "WorkspaceFileFault",
    "WorkspaceFiles",
    "WorkspaceChanges",
    "install_workspace_files",
]

#: Config 缺省（index.ts:185-190 的 Schemastery 缺省物化）。
WORKSPACE_FILE_CONFIG_DEFAULTS = {
    "maxBytes": 2 * 1024 * 1024,
    "maxFileBytes": 32 * 1024 * 1024,
    "maxLines": 5000,
    "maxEntries": 2000,
}

#: 文本永不携带的字节：其出现把一页标记为二进制（index.ts:95）。
_NUL = "\x00"


class WorkspaceFileFault(Exception):
    """带稳定 `workspace-file/*` 码的控制器失败（对齐上游 RemoteError）。"""

    def __init__(self, code: str, message: str, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


def resolve_config(config: dict | None = None) -> dict:
    resolved = dict(WORKSPACE_FILE_CONFIG_DEFAULTS)
    for key, value in dict(config or {}).items():
        if value is not None:
            resolved[key] = value
    for key, minimum in (("maxBytes", 1), ("maxFileBytes", 1),
                         ("maxLines", 1), ("maxEntries", 1)):
        value = resolved[key]
        if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
            raise ValueError(f"workspace-files: {key} must be an integer >= {minimum}")
    return resolved


class WorkspaceChanges:
    """一条 `changes` 代次：ready + 工作区内观察（线程安全非阻塞 pop）。"""

    def __init__(self, controller: "WorkspaceFiles", workspace_root: str):
        self.ready = {"kind": "ready"}
        self._root_key = os.path.realpath(workspace_root)
        self._fs = controller._fs()
        self._queue: deque[dict] = deque()
        self._lock = threading.Lock()
        self._closed = False
        self._dispose = controller.ctx.on("fs/observed", self._on_observed)

    def _on_observed(self, payload: Any) -> None:
        try:
            target, observation, _actor = payload
        except (TypeError, ValueError):
            return
        key = self._fs.process_path(target)
        if not is_path_under(key, self._root_key):
            return
        if getattr(observation, "kind", None) == "present":
            change = {"absolutePath": key, "version": observation.version}
        else:
            change = {"absolutePath": key, "absent": True}
        with self._lock:
            if self._closed:
                return
            self._queue.append({"kind": "change", "change": change})

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


class WorkspaceFiles(Service):
    """组合文件系统之上的 Host 文件读与工作区目录观察（index.ts WorkspaceFiles）。"""

    provide = "workspaceFiles"

    def __init__(self, ctx: Context, config: dict | None = None):
        self.config = resolve_config(config)
        super().__init__(ctx, "workspaceFiles")

    # ---------- 远程方法（同步门面；内部经常驻循环驱动 fs） ----------

    def read(self, scope: dict, path: str, range_: dict | None = None) -> dict:
        offset, limit = self._resolve_page(range_ or {})
        return self._drive(self._read_async(scope, path, offset, limit), path)

    def read_bytes(self, scope: dict, path: str, range_: dict | None = None) -> dict:
        offset, length = self._resolve_window(range_ or {}, path)
        return self._drive(self._read_bytes_async(scope, path, offset, length), path)

    def read_all(self, scope: dict, path: str) -> dict:
        return self._drive(self._read_all_async(scope, path), path)

    def read_related(self, scope: dict, path: str, relative_path: str) -> dict:
        relative = (relative_path or "").replace("\\", "/")
        if (relative == "" or relative.startswith("/")
                or _looks_like_url(relative) or _NUL in relative):
            raise WorkspaceFileFault("gateway/bad-request",
                                     "relativePath must be a relative filesystem path", {})
        target = self._drive(self._locate_file_async(scope, path), path)[0]
        absolute = self._fs().process_path(target)
        joined = (posixpath.join(posixpath.dirname(absolute), relative)
                  if absolute.startswith("/")
                  else ntpath.join(ntpath.dirname(absolute), relative))
        return self.read_all(scope, joined)

    def stat(self, scope: dict, path: str) -> dict:
        target, info = self._drive(self._locate_file_async(scope, path), path)
        return self._stat_of(target, info)

    def list(self, scope: dict, path: str) -> dict:
        return self._drive(self._list_async(scope, path), path)

    def changes(self, scope: dict) -> WorkspaceChanges:
        root = (scope or {}).get("workspaceRoot")
        if not isinstance(root, str) or not root:
            raise WorkspaceFileFault("gateway/bad-request", "workspace root is required", {})
        return WorkspaceChanges(self, root)

    # ---------- fs 异步面（在常驻循环上执行） ----------

    async def _inspect_async(self, scope: dict, path: str):
        if not isinstance(path, str) or path == "":
            raise WorkspaceFileFault("gateway/bad-request", "path is required", {})
        fs = self._fs()
        workspace_root = scope["workspaceRoot"]
        root = await fs.resolve(workspace_root)
        entry = await fs.lstat(path, {"cwd": workspace_root})
        if entry is None:
            raise WorkspaceFileFault("workspace-file/not-found",
                                     f'no entry at "{path}"', {"path": path})
        return fs, root, workspace_root, entry

    async def _locate_file_async(self, scope: dict, path: str):
        fs, _root, workspace_root, entry = await self._inspect_async(scope, path)
        if entry.type != "file":
            raise WorkspaceFileFault("workspace-file/not-regular-file",
                                     f'"{path}" is a {entry.type}',
                                     {"path": path, "kind": entry.type})
        target = await fs.resolve(path, {"cwd": workspace_root})
        info = await fs.stat(target)
        if info is None:
            raise WorkspaceFileFault("workspace-file/not-found",
                                     f'no entry at "{path}"', {"path": path})
        if info.type != "file":
            raise WorkspaceFileFault("workspace-file/not-regular-file",
                                     f'"{path}" is a {info.type}',
                                     {"path": path, "kind": info.type})
        return target, info

    async def _read_async(self, scope: dict, path: str, offset: int, limit: int) -> dict:
        target, info = await self._locate_file_async(scope, path)
        try:
            page = await self._cut_page(target, offset, limit, path)
        except FsError as error:
            if error.code == "FS_NOT_TEXT":
                raise WorkspaceFileFault("workspace-file/not-text",
                                         f'"{path}" is not UTF-8 text',
                                         {"path": path}) from error
            raise _map_fs_error(error, path) from error
        if _NUL in page["text"]:
            raise WorkspaceFileFault("workspace-file/not-text",
                                     f'"{path}" contains NUL bytes', {"path": path})
        return {**self._stat_of(target, info), "offset": offset,
                "text": page["text"], "lines": page["lines"], "eof": page["eof"]}

    async def _read_bytes_async(self, scope: dict, path: str, offset: int, length: int) -> dict:
        target, info = await self._locate_file_async(scope, path)
        try:
            data = await self._fs().read_byte_range(target, offset, length)
        except FsError as error:
            raise _map_fs_error(error, path) from error
        eof = (len(data) < length if info.size is None
               else offset + len(data) >= info.size)
        return {**self._stat_of(target, info), "offset": offset,
                "data": base64.b64encode(data).decode("ascii"), "eof": eof}

    async def _read_all_async(self, scope: dict, path: str) -> dict:
        target, info = await self._locate_file_async(scope, path)
        limit = self.config["maxFileBytes"]
        if info.size is not None and info.size > limit:
            raise WorkspaceFileFault(
                "workspace-file/too-large",
                f'"{path}" exceeds the {limit} byte full-file cap',
                {"path": path, "limit": limit})
        try:
            data = await self._fs().read_byte_range(target, 0, limit + 1)
        except FsError as error:
            raise _map_fs_error(error, path) from error
        if len(data) > limit:
            raise WorkspaceFileFault(
                "workspace-file/too-large",
                f'"{path}" exceeds the {limit} byte full-file cap',
                {"path": path, "limit": limit})
        return {**self._stat_of(target, info), "offset": 0,
                "data": base64.b64encode(data).decode("ascii"), "eof": True}

    async def _list_async(self, scope: dict, path: str) -> dict:
        fs, root, workspace_root, entry = await self._inspect_async(scope, path)
        if entry.type != "directory":
            raise WorkspaceFileFault("workspace-file/not-directory",
                                     f'"{path}" is a {entry.type}',
                                     {"path": path, "kind": entry.type})
        target = await fs.resolve(path, {"cwd": workspace_root})
        if not fs.contains(root, target):
            raise WorkspaceFileFault("workspace-file/outside-workspace",
                                     f'"{path}" is outside the workspace', {"path": path})
        children = await fs.list_dir(target)
        relative = _workspace_path_of(fs.process_path(root), fs.process_path(target))
        cap = self.config["maxEntries"]
        entries = [{"name": child.name, "type": child.type,
                    **({} if child.size is None else {"size": child.size})}
                   for child in children[:cap]]
        return {"path": relative, "entries": entries, "truncated": len(children) > cap}

    async def _cut_page(self, target, offset: int, limit: int, path: str) -> dict:
        last = offset + limit - 1
        lines: list[str] = []
        current = ""
        size = 0
        line_number = 1
        max_bytes = self.config["maxBytes"]

        def admit(chunk_size: int) -> None:
            nonlocal size
            size += chunk_size
            if size > max_bytes:
                raise WorkspaceFileFault(
                    "workspace-file/too-large",
                    f'lines {offset}-{last} of "{path}" exceed the {max_bytes} byte cap',
                    {"path": path, "limit": max_bytes})

        def complete() -> None:
            nonlocal current
            if lines:
                admit(1)
            lines.append(current)
            current = ""

        async for chunk in self._fs().stream_text(target):
            position = 0
            while position < len(chunk):
                if line_number > last:
                    return {"text": "\n".join(lines), "lines": len(lines), "eof": False}
                newline = chunk.find("\n", position)
                segment = chunk[position:] if newline == -1 else chunk[position:newline]
                if line_number >= offset:
                    admit(len(segment.encode("utf-8")))
                    current += segment
                if newline == -1:
                    break
                if line_number >= offset:
                    complete()
                line_number += 1
                position = newline + 1
        if len(current) > 0:
            complete()
        return {"text": "\n".join(lines), "lines": len(lines), "eof": True}

    # ---------- 内部 ----------

    def _drive(self, coro, path: str):
        """在常驻循环上同步驱动一段 fs 协程，并把 FsError 折成 wire 故障。"""
        try:
            return run_on_resident(coro)
        except WorkspaceFileFault:
            raise
        except FsError as error:
            raise _map_fs_error(error, path) from error

    def _fs(self):
        fs = self.ctx.get("fs")
        if fs is None:
            raise WorkspaceFileFault(
                "gateway/internal",
                "filesystem service is absent: this deployment does not mount a ctx.fs backend",
                {})
        return fs

    def _stat_of(self, target, info) -> dict:
        result = {"absolutePath": self._fs().process_path(target), "version": info.version}
        if info.size is not None:
            result["bytes"] = info.size
        return result

    def _resolve_page(self, range_: dict) -> tuple[int, int]:
        offset = _integer_at_least(range_.get("offset", 1), 1, "offset")
        limit = _integer_at_least(range_.get("limit", self.config["maxLines"]), 1, "limit")
        if limit > self.config["maxLines"]:
            raise WorkspaceFileFault("gateway/bad-request",
                                     f'limit must be at most {self.config["maxLines"]}', {})
        return offset, limit

    def _resolve_window(self, range_: dict, path: str) -> tuple[int, int]:
        offset = _integer_at_least(range_.get("offset", 0), 0, "offset")
        length = _integer_at_least(range_.get("length", self.config["maxBytes"]), 1, "length")
        if offset + length > 2 ** 53 - 1:
            raise WorkspaceFileFault("gateway/bad-request",
                                     "offset plus length must stay a safe integer", {})
        if length > self.config["maxBytes"]:
            raise WorkspaceFileFault(
                "workspace-file/too-large",
                f'{length} bytes of "{path}" exceed the {self.config["maxBytes"]} byte cap',
                {"path": path, "limit": self.config["maxBytes"]})
        return offset, length


def _integer_at_least(value: Any, minimum: int, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise WorkspaceFileFault(
            "gateway/bad-request",
            f"{name} must be a safe integer of at least {minimum}", {})
    return value


def _looks_like_url(value: str) -> bool:
    import re
    return re.match(r"^[a-z][a-z\d+.-]*:", value, re.IGNORECASE) is not None


def _workspace_path_of(root_path: str, target_path: str) -> str:
    relative = os.path.relpath(target_path, root_path)
    return "" if relative == "." else relative.replace("\\", "/")


def _map_fs_error(error: FsError, path: str) -> WorkspaceFileFault:
    code = getattr(error, "code", None)
    if code == "FS_NOT_FOUND":
        return WorkspaceFileFault("workspace-file/not-found",
                                  f'no entry at "{path}"', {"path": path})
    if code == "FS_NOT_TEXT":
        return WorkspaceFileFault("workspace-file/not-text",
                                  f'"{path}" is not UTF-8 text', {"path": path})
    if code == "FS_TOO_LARGE":
        return WorkspaceFileFault("workspace-file/too-large", str(error), {"path": path})
    return WorkspaceFileFault("gateway/internal", str(error), {})


def install_workspace_files(ctx: Context, config: dict | None = None) -> WorkspaceFiles:
    """幂等装配 `ctx.workspaceFiles`。"""
    existing = ctx.get("workspaceFiles")
    if existing is not None:
        return existing
    return WorkspaceFiles(ctx, config)
