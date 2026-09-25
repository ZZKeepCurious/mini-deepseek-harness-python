"""本地文件系统后端（对齐 packages/fs/fs-local/src/{index,fsio}.ts）。

- realpath 派生目标身份：别名共享陈旧守卫；写穿符号链接时更新其目标而不替换链接；
- 文本读取返回严格 UTF-8，NUL 采样拒绝二进制，行窗口属消费者；
- 变更经目标目录内的私有 staging 目录原子发布（0700/0600），createIfAbsent 用硬链接
  的 no-replace 语义；每 targetKey 串行化，使 read→guard→write 窗口不交错。
"""
from __future__ import annotations

import asyncio
import codecs
import os
import stat as stat_mod
import threading
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable

from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

from ..core.scope import Context
from .service import FileSystem
from .types import (
    FsDirEntry,
    FsEditOutcome,
    FsEditRequest,
    FsError,
    FsInfo,
    FsPathInfo,
    FsTarget,
    FsWriteIntent,
    FsWriteOutcome,
)

__all__ = ["DEFAULT_DIFF_BASIS_MAX_BYTES", "LocalFileSystem", "install_local_fs"]

BINARY_SAMPLE_BYTES = 8192
DEFAULT_DIFF_BASIS_MAX_BYTES = 10 * 1024 * 1024
MAX_DIFF_BASIS_BYTES = 2**31 - 1

_LineEndings = str  # 'LF' | 'CRLF'


def _is_enoent(error: OSError) -> bool:
    return error.errno in (2, 20)  # ENOENT, ENOTDIR


def _throw_if_aborted(signal: Any, verb: str) -> None:
    if signal is not None and getattr(signal, "is_set", lambda: False)():
        raise FsError(f"{verb} aborted", "FS_ABORTED")


def _version_of(info: os.stat_result) -> str:
    return (f"{info.st_dev}:{info.st_ino}:{info.st_size}:"
            f"{info.st_mtime_ns}:{info.st_ctime_ns}")


def _type_of(info: os.stat_result) -> str:
    if stat_mod.S_ISREG(info.st_mode):
        return "file"
    if stat_mod.S_ISDIR(info.st_mode):
        return "directory"
    return "other"


def _normalize_line_endings(content: str) -> str:
    return content.replace("\r\n", "\n")


def _detect_line_endings(raw: str) -> _LineEndings:
    sample = raw[:4096]
    crlf = sample.count("\r\n")
    lf = sample.count("\n") - crlf
    return "CRLF" if crlf > lf else "LF"


def _restore_line_endings(content: str, line_endings: _LineEndings) -> str:
    if line_endings == "LF":
        return content
    return _normalize_line_endings(content).replace("\n", "\r\n")


def _count_occurrences(content: str, needle: str) -> int:
    count = 0
    index = 0
    while True:
        found = content.find(needle, index)
        if found == -1:
            return count
        count += 1
        index = found + len(needle)


def apply_literal_edit(content: str, old_string: str, new_string: str,
                       replace_all: bool, display_path: str) -> str:
    """对 LF 归一化内容做字面替换（对齐 fsio.applyLiteralEdit，措辞逐字）。"""
    old_norm = _normalize_line_endings(old_string)
    if old_norm == "":
        raise FsError("old_string must be a non-empty string", "FS_EDIT_NOT_FOUND")
    new_norm = _normalize_line_endings(new_string)
    replacements = _count_occurrences(content, old_norm)
    if replacements == 0:
        raise FsError(f'old_string was not found in "{display_path}"', "FS_EDIT_NOT_FOUND")
    if not replace_all and replacements > 1:
        raise FsError(
            f'old_string matched {replacements} times in "{display_path}"; '
            "provide a more specific old_string or set replace_all to true",
            "FS_AMBIGUOUS_EDIT")
    return content.replace(old_norm, new_norm)


def _decode_utf8(raw: bytes, verb: str, display_path: str) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise FsError(f'cannot {verb} "{display_path}": invalid UTF-8 text',
                      "FS_NOT_TEXT", error) from error


def local_display_path(cwd: str, path: str) -> str:
    """锚定路径：绝对 cwd/绝对 path 直用，否则 join 后 abspath（保留物理拼写）。"""
    absolute_cwd = cwd if os.path.isabs(cwd) else os.path.join(os.getcwd(), cwd)
    raw = path if os.path.isabs(path) else os.path.join(absolute_cwd, path)
    return os.path.abspath(raw)


def _resolve_local_target(cwd: str, path: str) -> tuple[str, str]:
    """解析为 (displayPath, realpath targetKey)；缺失时 realpath 最近存在祖先并续接后缀。"""
    if path.strip() == "":
        raise FsError("file_path must be a non-empty string", "FS_NOT_FOUND")
    display_path = local_display_path(cwd, path)
    try:
        return display_path, os.path.realpath(display_path)
    except OSError as error:
        if not _is_enoent(error):
            raise
    missing = [os.path.basename(display_path)]
    ancestor = os.path.dirname(display_path)
    while True:
        try:
            real_ancestor = os.path.realpath(ancestor)
            return display_path, os.path.join(real_ancestor, *missing)
        except OSError as error:
            if not _is_enoent(error):
                raise
            parent = os.path.dirname(ancestor)
            if parent == ancestor:
                return display_path, display_path
            missing.insert(0, os.path.basename(ancestor))
            ancestor = parent


def _probe(absolute_path: str, follow: bool = True) -> dict | None:
    try:
        info = os.stat(absolute_path) if follow else os.lstat(absolute_path)
    except OSError as error:
        if not _is_enoent(error):
            raise
        return None
    return {
        "version": _version_of(info),
        "mode": stat_mod.S_IMODE(info.st_mode),
        "type": _type_of(info),
        "size": info.st_size,
    }


class _WatchHandler(FileSystemEventHandler):
    """watchdog 事件 → 目标/直接子项过滤 → changed（对齐 fs-local chokidar 过滤）。

    文件目标只转发命中该目标路径的事件；目录目标转发其直接子项的任意事件。
    载体差异：changed 由 watchdog 的 emitter 线程调用（上游在事件循环回调）。
    """

    def __init__(self, changed: Callable[..., None], target_path: str,
                 directory: bool) -> None:
        super().__init__()
        self._changed = changed
        self._target = os.path.normcase(os.path.abspath(target_path))
        self._directory = directory

    def _maybe(self, event: Any) -> None:
        if self._directory:
            self._changed()
            return
        for raw in (getattr(event, "dest_path", "") or "",
                    getattr(event, "src_path", "") or ""):
            if raw and os.path.normcase(os.path.abspath(raw)) == self._target:
                self._changed()
                return

    def on_created(self, event: Any) -> None:
        self._maybe(event)

    def on_modified(self, event: Any) -> None:
        self._maybe(event)

    def on_deleted(self, event: Any) -> None:
        self._maybe(event)

    def on_moved(self, event: Any) -> None:
        self._maybe(event)


class LocalFileSystem(FileSystem):
    """宿主文件系统后端（对齐上游 LocalFileSystem）。"""

    def __init__(self, ctx: Context, config: dict | None = None):
        config = config or {}
        cwd = config.get("cwd") or os.getcwd()
        diff_max = config.get("diffBasisMaxBytes", DEFAULT_DIFF_BASIS_MAX_BYTES)
        if (not isinstance(diff_max, int) or isinstance(diff_max, bool)
                or diff_max <= 0 or diff_max > MAX_DIFF_BASIS_BYTES):
            raise ValueError(
                "fs-local: diffBasisMaxBytes must be a positive safe integer "
                f"no greater than {MAX_DIFF_BASIS_BYTES}")
        super().__init__(ctx)
        self.config = {"cwd": cwd, "diffBasisMaxBytes": diff_max}
        self._locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def _lock_for(self, target_key: str) -> threading.Lock:
        with self._locks_guard:
            lock = self._locks.get(target_key)
            if lock is None:
                lock = threading.Lock()
                self._locks[target_key] = lock
            return lock

    # ---------- 观察 ----------

    async def watch(self, target: FsTarget, changed: Callable[..., None],
                    signal: Any = None) -> Callable[[], Awaitable[None]]:
        """watchdog 实现（对齐上游 fs-local chokidar 适配）。

        文件/缺失目标 watch 其父目录（depth 0）并只转发命中目标路径的事件；
        目录目标 watch 目录本身、转发其直接子项事件。watchdog 无 `ready` 事件，
        本方法在 observer 线程启动后即返回；`signal` 只在 stat 前后检查，不在
        等待期观察（载体差异，见 verified-diffs §3.41）。
        """
        _throw_if_aborted(signal, "watch")
        path = os.path.abspath(self.process_path(target))
        info = await self.stat(target, signal)
        _throw_if_aborted(signal, "watch")
        directory = info is not None and info.type == "directory"
        root = path if directory else os.path.dirname(path)
        if not os.path.isdir(root):
            error = FsError(
                f'cannot watch "{target.display_path}": parent directory does not exist',
                "FS_IO_ERROR")
            changed(error)
            raise error
        observer = Observer()
        observer.schedule(_WatchHandler(changed, path, directory), root, recursive=False)

        async def close() -> None:
            observer.stop()
            await asyncio.to_thread(observer.join, 5.0)

        try:
            observer.start()
        except BaseException as error:
            await close()
            changed(error)
            raise
        return close

    # ---------- 身份 / 路径 ----------

    async def resolve(self, path: str, opts: dict | None = None) -> FsTarget:
        opts = opts or {}
        _throw_if_aborted(opts.get("signal"), "resolve")
        display, key = _resolve_local_target(opts.get("cwd") or self.config["cwd"], path)
        _throw_if_aborted(opts.get("signal"), "resolve")
        return FsTarget(target_key=key, display_path=display)

    def process_path(self, target: FsTarget) -> str:
        return target.target_key

    def process_path_from_host_path(self, host_path: str) -> str | None:
        return os.path.abspath(host_path) if os.path.isabs(host_path) else None

    def file_url(self, target: FsTarget) -> str:
        from pathlib import Path
        return Path(self.process_path(target)).as_uri()

    def contains(self, parent: FsTarget, child: FsTarget) -> bool:
        rel = os.path.relpath(self.process_path(child), self.process_path(parent))
        return rel == "." or (rel != ".." and not rel.startswith(".." + os.sep)
                              and not os.path.isabs(rel))

    # ---------- 元数据 ----------

    async def stat(self, target: FsTarget, signal: Any = None) -> FsInfo | None:
        _throw_if_aborted(signal, "stat")
        info = _probe(target.target_key)
        if info is None:
            return None
        return FsInfo(version=info["version"], type=info["type"], size=info["size"])

    async def lstat(self, path: str, opts: dict | None = None,
                    signal: Any = None) -> FsPathInfo | None:
        _throw_if_aborted(signal, "lstat")
        if path.strip() == "":
            raise FsError("file_path must be a non-empty string", "FS_NOT_FOUND")
        display = local_display_path((opts or {}).get("cwd") or self.config["cwd"], path)
        try:
            info = os.lstat(display)
        except OSError as error:
            if _is_enoent(error):
                return None
            raise
        kind = "symlink" if stat_mod.S_ISLNK(info.st_mode) else _type_of(info)
        return FsPathInfo(version=_version_of(info), type=kind, size=info.st_size)

    async def list_dir(self, target: FsTarget, signal: Any = None) -> list[FsDirEntry]:
        _throw_if_aborted(signal, "list")
        info = _probe(target.target_key)
        if info is None:
            raise FsError(f'cannot list "{target.display_path}": not found', "FS_NOT_FOUND")
        if info["type"] != "directory":
            raise FsError(f'cannot list "{target.display_path}": not a directory',
                          "FS_NOT_DIRECTORY")
        try:
            names = sorted(os.listdir(target.target_key))
        except OSError as error:
            raise FsError(f'cannot list "{target.display_path}": {error}', "FS_IO_ERROR",
                          error) from error
        entries: list[FsDirEntry] = []
        for name in names:
            _throw_if_aborted(signal, "list")
            child_display = local_display_path(target.display_path, name)
            _, child_key = _resolve_local_target(target.target_key, name)
            child = _probe(child_key)
            entries.append(FsDirEntry(
                name=name,
                type=(child["type"] if child else "other"),
                target=FsTarget(target_key=child_key, display_path=child_display),
                version=(child["version"] if child else None),
                size=(child["size"] if child and child["type"] == "file" else None),
            ))
        return entries

    # ---------- 读取 ----------

    def _stat_regular_file(self, target: FsTarget, verb: str, signal: Any) -> os.stat_result:
        _throw_if_aborted(signal, verb)
        try:
            info = os.stat(target.target_key)
        except OSError as error:
            if _is_enoent(error):
                raise FsError(f'cannot {verb} "{target.display_path}": not found',
                              "FS_NOT_FOUND", error) from error
            raise
        if not stat_mod.S_ISREG(info.st_mode):
            raise FsError(f'cannot {verb} "{target.display_path}": not a regular file',
                          "FS_NOT_REGULAR_FILE")
        return info

    async def read_text(self, target: FsTarget, signal: Any = None) -> str:
        self._stat_regular_file(target, "read", signal)
        with open(target.target_key, "rb") as handle:
            raw = handle.read()
        _throw_if_aborted(signal, "read")
        if 0 in raw[:BINARY_SAMPLE_BYTES]:
            raise FsError(f'cannot read "{target.display_path}": binary file', "FS_NOT_TEXT")
        return _decode_utf8(raw, "read", target.display_path)

    def stream_text(self, target: FsTarget, signal: Any = None) -> AsyncIterator[str]:
        self._stat_regular_file(target, "read", signal)
        decoder = codecs.getincrementaldecoder("utf-8")("strict")
        sampled = 0

        async def gen() -> AsyncIterator[str]:
            nonlocal sampled
            with open(target.target_key, "rb") as handle:
                while True:
                    _throw_if_aborted(signal, "read")
                    chunk = handle.read(65536)
                    if not chunk:
                        break
                    if sampled < BINARY_SAMPLE_BYTES:
                        sample = chunk[:BINARY_SAMPLE_BYTES - sampled]
                        if 0 in sample:
                            raise FsError(f'cannot read "{target.display_path}": binary file',
                                          "FS_NOT_TEXT")
                        sampled += len(sample)
                    try:
                        text = decoder.decode(chunk)
                    except UnicodeDecodeError as error:
                        raise FsError(
                            f'cannot read "{target.display_path}": invalid UTF-8 text',
                            "FS_NOT_TEXT", error) from error
                    if text:
                        yield text
            try:
                tail = decoder.decode(b"", final=True)
            except UnicodeDecodeError as error:
                raise FsError(
                    f'cannot read "{target.display_path}": invalid UTF-8 text',
                    "FS_NOT_TEXT", error) from error
            if tail:
                yield tail

        return gen()

    async def read_bytes(self, target: FsTarget, signal: Any, max_bytes: int) -> bytes:
        info = self._stat_regular_file(target, "read", signal)
        if info.st_size > max_bytes:
            raise FsError(
                f'cannot read "{target.display_path}": {info.st_size} bytes exceeds '
                f"the {max_bytes}-byte limit", "FS_TOO_LARGE")
        data = bytearray()
        with open(target.target_key, "rb") as handle:
            while len(data) <= max_bytes:
                _throw_if_aborted(signal, "read")
                chunk = handle.read(min(65536, max_bytes + 1 - len(data)))
                if not chunk:
                    break
                data += chunk
        if len(data) > max_bytes:
            raise FsError(
                f'cannot read "{target.display_path}": content exceeds '
                f"the {max_bytes}-byte limit", "FS_TOO_LARGE")
        return bytes(data)

    async def read_byte_range(self, target: FsTarget, offset: int, length: int,
                              signal: Any = None) -> bytes:
        self._stat_regular_file(target, "read", signal)
        if length == 0:
            return b""
        with open(target.target_key, "rb") as handle:
            handle.seek(offset)
            _throw_if_aborted(signal, "read")
            return handle.read(length)

    # ---------- 写入 / 编辑 ----------

    async def write_text(self, target: FsTarget, content: str,
                         expected: FsWriteIntent | None = None, signal: Any = None,
                         sandbox_policy: Any = None) -> FsWriteOutcome:
        lock = self._lock_for(target.target_key)
        lock.acquire()
        try:
            _throw_if_aborted(signal, "write")
            existing = _probe(target.target_key)
            if existing is not None and existing["type"] != "file":
                raise FsError(
                    f'cannot write "{target.display_path}": not a regular file',
                    "FS_NOT_REGULAR_FILE")
            if expected is not None and expected.kind == "replaceIfVersion":
                if existing is None:
                    raise FsError(
                        f'cannot write "{target.display_path}": file no longer exists',
                        "FS_STALE_VERSION")
                if existing["version"] != expected.version:
                    raise FsError(
                        f'cannot write "{target.display_path}": file changed since it was read',
                        "FS_STALE_VERSION")
            elif expected is not None and expected.kind == "createIfAbsent" and existing is not None:
                raise FsError(
                    f'cannot overwrite existing "{target.display_path}" without reading it first',
                    "FS_NOT_OBSERVED")

            diffable = (existing is not None
                        and len(content.encode("utf-8")) < self.config["diffBasisMaxBytes"])
            before = (self._read_text_for_diff(target.target_key,
                                               self.config["diffBasisMaxBytes"], signal)
                      if diffable else None)
            self._write_file_atomic(
                target.target_key, content, existing["mode"] if existing else None,
                signal, create_if_absent=(expected is not None
                                          and expected.kind == "createIfAbsent"),
                display_path=target.display_path)
            after = _probe(target.target_key)
            return FsWriteOutcome(
                operation="update" if existing else "create",
                version=(after["version"] if after
                         else f"missing:{target.target_key}"),
                before=before,
                after=_normalize_line_endings(content),
            )
        finally:
            lock.release()

    async def edit_text(self, target: FsTarget, edit: FsEditRequest,
                        expected: dict | None = None, signal: Any = None,
                        sandbox_policy: Any = None) -> FsEditOutcome:
        lock = self._lock_for(target.target_key)
        lock.acquire()
        try:
            _throw_if_aborted(signal, "edit")
            existing = _probe(target.target_key)
            if existing is None:
                raise FsError(
                    f'cannot edit "{target.display_path}": file changed since it was read',
                    "FS_STALE_VERSION")
            if existing["type"] != "file":
                raise FsError(
                    f'cannot edit "{target.display_path}": not a regular file',
                    "FS_NOT_REGULAR_FILE")
            if expected is not None and existing["version"] != expected.get("version"):
                raise FsError(
                    f'cannot edit "{target.display_path}": file changed since it was read',
                    "FS_STALE_VERSION")
            original, line_endings = self._read_for_edit(target.target_key,
                                                         target.display_path, signal)
            edited = apply_literal_edit(original, edit.old_string, edit.new_string,
                                        edit.replace_all, target.display_path)
            content = _restore_line_endings(edited, line_endings)
            self._write_file_atomic(target.target_key, content, existing["mode"], signal)
            after = _probe(target.target_key)
            return FsEditOutcome(
                version=(after["version"] if after
                         else f"missing:{target.target_key}"),
                before=original,
                after=edited,
            )
        finally:
            lock.release()

    # ---------- 内部 IO ----------

    def _read_for_edit(self, absolute_path: str, display_path: str,
                       signal: Any) -> tuple[str, _LineEndings]:
        _throw_if_aborted(signal, "edit")
        with open(absolute_path, "rb") as handle:
            raw = handle.read()
        if b"\x00" in raw:
            raise FsError(f'cannot edit "{display_path}": binary file', "FS_NOT_TEXT")
        text = _decode_utf8(raw, "edit", display_path)
        return _normalize_line_endings(text), _detect_line_endings(text)

    def _read_text_for_diff(self, absolute_path: str, max_bytes: int,
                            signal: Any) -> str | None:
        try:
            _throw_if_aborted(signal, "read")
            info = os.stat(absolute_path)
            if not stat_mod.S_ISREG(info.st_mode):
                return None
            if info.st_size >= max_bytes:
                return None
            with open(absolute_path, "rb") as handle:
                raw = handle.read(info.st_size + 1)
            if len(raw) != info.st_size:
                return None
            if b"\x00" in raw:
                return None
            try:
                return _normalize_line_endings(raw.decode("utf-8"))
            except UnicodeDecodeError:
                return None
        except FsError:
            raise
        except OSError:
            return None

    def _write_file_atomic(self, absolute_path: str, content: str,
                           mode: int | None, signal: Any, *,
                           create_if_absent: bool = False,
                           display_path: str | None = None) -> None:
        _throw_if_aborted(signal, "write")
        directory = os.path.dirname(absolute_path)
        os.makedirs(directory, exist_ok=True)
        _throw_if_aborted(signal, "write")
        staging = os.path.join(
            directory,
            f".{os.path.basename(absolute_path)}.{os.getpid()}.{uuid.uuid4()}.tmpdir")
        temp = os.path.join(staging, f"{os.path.basename(absolute_path)}.tmp")
        try:
            os.mkdir(staging, 0o700)
        except OSError:
            raise
        try:
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(fd, "wb") as handle:
                    handle.write(content.encode("utf-8"))
                    handle.flush()
                    os.fsync(handle.fileno())
                    if mode is not None:
                        os.chmod(temp, mode)
            except BaseException:
                raise
            _throw_if_aborted(signal, "write")
            if create_if_absent:
                try:
                    os.link(temp, absolute_path)
                except FileExistsError as error:
                    existing = _probe(absolute_path, follow=False)
                    if existing is not None and existing["type"] != "file":
                        raise FsError(
                            f'cannot write "{display_path}": not a regular file',
                            "FS_NOT_REGULAR_FILE", error) from error
                    raise FsError(
                        f'cannot overwrite existing "{display_path}" without reading it first',
                        "FS_NOT_OBSERVED", error) from error
            else:
                os.replace(temp, absolute_path)
            try:
                os.rmdir(staging)
            except OSError:
                import shutil
                shutil.rmtree(staging, ignore_errors=True)
        except BaseException:
            import shutil
            try:
                shutil.rmtree(staging, ignore_errors=True)
            except OSError:
                pass
            raise


def install_local_fs(ctx: Context, config: dict | None = None) -> LocalFileSystem:
    """幂等装配本地文件系统后端（`ctx.fs`）。"""
    existing = ctx.get("fs")
    if existing is not None:
        return existing
    return LocalFileSystem(ctx, config)
