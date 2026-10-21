"""文件工具编辑周围的整文件捕获。

上游对照：`packages/deliverables/workspace-changes/src/capture.ts`。捕获内容
按 SHA-1 内容寻址存到会话临时目录，使比较不依赖 git；同一内容跨路径/跨轮共享。
"""
from __future__ import annotations

import hashlib
import os
import stat

__all__ = ["capture_file", "same_capture"]

BINARY_PROBE_BYTES = 8000


def capture_file(absolute: str, directory: str, max_bytes: int) -> dict | None:
    """捕获一个路径的当前内容；非普通文件返回 None，缺失返回 `absent`。"""
    if os.path.isdir(absolute):
        return None
    try:
        handle = open(absolute, "rb")
    except FileNotFoundError:
        return {"kind": "absent"}
    with handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            return None
        data = handle.read(max_bytes + 1)
    if len(data) > max_bytes:
        return {"kind": "oversized"}
    digest = hashlib.sha1(data).hexdigest()
    path = os.path.join(directory, digest)
    os.makedirs(directory, exist_ok=True)
    try:
        with open(path, "xb") as copy:
            copy.write(data)
    except FileExistsError:
        pass
    return {
        "kind": "file",
        "file": path,
        "binary": b"\0" in data[:BINARY_PROBE_BYTES],
    }


def same_capture(a: dict, b: dict) -> bool:
    if a["kind"] == "absent" or b["kind"] == "absent":
        return a["kind"] == b["kind"]
    return a["kind"] == "file" and b["kind"] == "file" and a["file"] == b["file"]
