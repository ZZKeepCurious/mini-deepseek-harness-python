"""原子整文件替换（JSON backend 的发布协议）。

上游对照：packages/storage/storage-json/src/atomic.ts（writeAtomic）。

发布协议：写同目录临时文件 → fsync → os.replace 盖目标。replace 在 POSIX 与
Windows（MoveFileEx REPLACE_EXISTING）都是原子替换，last-write-wins 语义正确
——unit 文件每进程恰一写者。POSIX 上 replace 后再 fsync 父目录使新条目崩溃持久。
"""
from __future__ import annotations

import os
import tempfile

__all__ = ["write_atomic"]


def write_atomic(path: str, data: str) -> None:
    """持久替换 `path` 为 `data`（resolve 后替换即崩溃持久）。"""
    directory = os.path.dirname(path)
    fd, tmp = tempfile.mkstemp(prefix=".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(directory)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _fsync_directory(path: str) -> None:
    """fsync 一个 POSIX 目录使刚 replace 的条目崩溃持久。Windows 拒绝目录
    打开（libuv 同款落地），跳过。"""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    finally:
        os.close(fd)