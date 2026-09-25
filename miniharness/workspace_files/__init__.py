"""Workspace 文件 Remote 服务（对齐 packages/api/workspace-files）。

承载 `ctx.workspaceFiles`：只读文件预览（`read` / `readBytes` / `stat`）、工作区内的
目录列举（`list`）与目标级观察变更流（`changes`）。文件读取遵循组合文件系统的读权限
（可读工作区外路径）；目录列举与目录观察限定在会话工作区根内。本服务**不含任何变更操作**。

`readBytes` 是唯一字节读入口（上游 rc.1 把 `readAll`/`readRelated` 折入）：
`options.range` 给 Byte 窗口，省略则读整文件并在超 `maxFileBytes` 时
`workspace-file/too-large`；`options.baseFile` 提供相对目标时的基文件目录。服务值
`WorkspaceFileBytes.data` 是原生 `bytes`，base64 只在 web wire 层做。

`changes(scope, path)` 是目标级的：经 `ctx.fs.watch` 建立 OS 监听，成功后才给 `ready`，
此后命中目标的失效重 stat 产出当前元数据；目录目标须在工作区内；后端不能 watch 时折
`workspace-file/watch-unsupported`。

载体差异（登记）：
  * 上游 `ctx.fs` 的方法为 Promise；mini 的 fs seam 本体为 async（多数同步体），
    本控制器经常驻事件循环（`run_on_resident`）同步驱动，对 WebApi 同步派发面等价。
  * 上游 lookup `workspaceFileScope` 可回退持久化 `stat` 解析冷会话 header；mini
    无会话持久化 stat 面，scope 由 web 层从 `ctx.sessions` 或 sandboxPolicy 回退根派生。
  * `changes` 的 OS 监听经 watchdog（`fs.watch`）；watch 回调在 emitter 线程，观察队列
    只做入队，重 stat 在 pop 时同步驱动（上游在事件循环内 await stat）。
"""
from __future__ import annotations

import os
import posixpath
import ntpath
import threading
from typing import Any

from ..core.agent_loop.resident_loop import run_on_resident
from ..core.scope import Context, Service
from ..fs import FsError

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
    """一条 `changes` 代次：目标级 OS watch + `fs/observed`（非阻塞 pop）。

    对齐 changes.ts WorkspaceChangeFeed.follow：先解析目标（目录须在工作区内），
    经 `ctx.fs.watch` 建立 OS 监听，成功后才对外给 `ready`；此后每条命中目标的
    失效（watch 回调或受插桩 `fs/observed`）在 pop 时重 stat 目标，产出当前元数据
    （`version` 或 `absent`）。watch 初始化失败折 `workspace-file/watch-unsupported`。
    """

    def __init__(self, controller: "WorkspaceFiles", workspace_root: str, path: str):
        self._fs = controller._fs()
        self._ctx = controller.ctx
        self._workspace_root = workspace_root
        self._path = path
        self._root = None
        self._target = None
        self._watch_close = None
        self._dispose = None
        self._failure: WorkspaceFileFault | None = None
        self._lock = threading.Lock()
        self._pending = 0
        self._closed = False
        self.ready: dict | None = None
        self._setup()

    def _setup(self) -> None:
        try:
            run_on_resident(self._setup_async())
        except WorkspaceFileFault as error:
            self._failure = error
            raise
        except FsError as error:
            fault = _map_fs_error(error, self._path)
            self._failure = fault
            raise fault from error

    async def _setup_async(self) -> None:
        fs = self._fs
        self._root = await fs.resolve(self._workspace_root)
        self._target = await fs.resolve(self._path, {"cwd": self._workspace_root})
        # 目录目标须在工作区内（目录观察仍是工作区限定的）。
        await self._stat_target_async()
        try:
            self._watch_close = await fs.watch(self._target, self._on_watch, None)
        except FsError as error:
            raise WorkspaceFileFault(
                "workspace-file/watch-unsupported", str(error),
                {"path": self._path}) from error
        except Exception as error:  # noqa: BLE001 - 任何 watch 装配失败折稳定码
            raise WorkspaceFileFault(
                "workspace-file/watch-unsupported", str(error),
                {"path": self._path}) from error
        self._dispose = self._ctx.on("fs/observed", self._on_observed)
        self.ready = {"kind": "ready"}

    async def _stat_target_async(self):
        info = await self._fs.stat(self._target)
        if (info is not None and info.type == "directory"
                and not self._fs.contains(self._root, self._target)):
            raise WorkspaceFileFault(
                "workspace-file/outside-workspace",
                "Directory is outside the workspace", {"path": self._path})
        return info

    def _on_observed(self, payload: Any) -> None:
        try:
            target = payload[0]
        except (TypeError, IndexError):
            return
        if self._target is None:
            return
        if self._fs.process_path(target) != self._fs.process_path(self._target):
            return
        self._enqueue()

    def _on_watch(self, error: Any = None) -> None:
        if error is not None:
            self._failure = WorkspaceFileFault(
                "workspace-file/watch-unsupported", str(error), {"path": self._path})
            return
        self._enqueue()

    def _enqueue(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._pending += 1

    def pop(self) -> dict | None:
        if self._failure is not None:
            raise self._failure
        with self._lock:
            if self._closed or self._pending == 0:
                return None
            self._pending -= 1
        try:
            info = run_on_resident(self._stat_target_async())
        except WorkspaceFileFault:
            raise
        except FsError as error:
            raise _map_fs_error(error, self._path) from error
        absolute = self._fs.process_path(self._target)
        if info is not None:
            change = {"absolutePath": absolute, "version": info.version}
        else:
            change = {"absolutePath": absolute, "absent": True}
        return {"kind": "change", "change": change}

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._pending = 0
        if self._dispose is not None:
            self._dispose()
            self._dispose = None
        watch_close = self._watch_close
        self._watch_close = None
        if watch_close is not None:
            run_on_resident(watch_close())


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

    def read_bytes(self, scope: dict, path: str, options: dict | None = None) -> dict:
        """`readBytes`：`options.range` 定点窗口；省略则整文件（`maxFileBytes` cap）。

        `options.baseFile` 提供基文件，`path` 相对其目录解析（上游 index.ts:257-280）。
        """
        options = options or {}
        range_ = options.get("range")
        window = None if range_ is None else self._resolve_window(dict(range_), path)
        resolved = path
        if options.get("baseFile") is not None:
            resolved = self._drive(
                self._relative_path_async(scope, options["baseFile"], path), path)
        if window is None:
            return self._drive(self._read_all_bytes_async(scope, resolved), resolved)
        offset, length = window
        return self._drive(
            self._read_bytes_async(scope, resolved, offset, length), resolved)

    def stat(self, scope: dict, path: str) -> dict:
        target, info = self._drive(self._locate_file_async(scope, path), path)
        return self._stat_of(target, info)

    def list(self, scope: dict, path: str) -> dict:
        return self._drive(self._list_async(scope, path), path)

    def changes(self, scope: dict, path: str) -> WorkspaceChanges:
        root = (scope or {}).get("workspaceRoot")
        if not isinstance(root, str) or not root:
            raise WorkspaceFileFault("gateway/bad-request", "workspace root is required", {})
        if not isinstance(path, str) or path == "":
            raise WorkspaceFileFault("gateway/bad-request", "path is required", {})
        return WorkspaceChanges(self, root, path)

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
        return {**self._stat_of(target, info), "offset": offset, "data": data, "eof": eof}

    async def _read_all_bytes_async(self, scope: dict, path: str) -> dict:
        target, info = await self._locate_file_async(scope, path)
        limit = self.config["maxFileBytes"]
        try:
            data = await self._fs().read_bytes(target, None, limit)
        except FsError as error:
            if error.code == "FS_TOO_LARGE":
                raise WorkspaceFileFault(
                    "workspace-file/too-large",
                    f'"{path}" exceeds the {limit} byte full-file cap',
                    {"path": path, "limit": limit}) from error
            raise _map_fs_error(error, path) from error
        return {**self._stat_of(target, info), "offset": 0, "data": data, "eof": True}

    async def _relative_path_async(self, scope: dict, base_file: str, path: str) -> str:
        """`path` 相对 `base_file` 目录解析（上游 index.ts relativePath）。

        `path` 必须是相对路径（非空、不以 `/` 起、非 URL、无 NUL），否则
        `gateway/bad-request`；基文件本身按常规文件定位。
        """
        relative = (path or "").replace("\\", "/")
        if (relative == "" or relative.startswith("/")
                or _looks_like_url(relative) or _NUL in relative):
            raise WorkspaceFileFault(
                "gateway/bad-request", "path must be relative when baseFile is provided", {})
        target, _info = await self._locate_file_async(scope, base_file)
        absolute = self._fs().process_path(target)
        return (posixpath.join(posixpath.dirname(absolute), relative)
                if absolute.startswith("/")
                else ntpath.join(ntpath.dirname(absolute), relative))

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
