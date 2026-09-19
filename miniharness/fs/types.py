"""文件系统 Service Definition（`ctx.fs`）词表（对齐 packages/fs/fs/src/types.ts）。

目标/版本是不透明标识：消费者不得解析；后端（本地/远程）负责稳定身份、文本读取、
二进制拒绝与原子变更。错误码闭集由本模块拥有，后端与策略层共用。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "FS_ERROR_CODES",
    "FsDirEntry",
    "FsEditOutcome",
    "FsEditRequest",
    "FsError",
    "FsErrorCode",
    "FsInfo",
    "FsObservation",
    "FsPathInfo",
    "FsTarget",
    "FsTargetKey",
    "FsVersion",
    "FsWriteOutcome",
]

FsTargetKey = str
FsVersion = str

FsErrorCode = Literal[
    "FS_NOT_FOUND",
    "FS_NOT_DIRECTORY",
    "FS_NOT_TEXT",
    "FS_NOT_REGULAR_FILE",
    "FS_TOO_LARGE",
    "FS_PERMISSION_DENIED",
    "FS_SANDBOX_DENIED",
    "FS_IO_ERROR",
    "FS_STALE_VERSION",
    "FS_NOT_OBSERVED",
    "FS_AMBIGUOUS_EDIT",
    "FS_EDIT_NOT_FOUND",
    "FS_ABORTED",
]

FS_ERROR_CODES: frozenset[str] = frozenset(FsErrorCode.__args__)  # type: ignore[attr-defined]


@dataclass(frozen=True)
class FsTarget:
    """后端解析出的稳定目标；`target_key` 不透明，`display_path` 供模型/UI 展示。"""

    target_key: FsTargetKey
    display_path: str


@dataclass(frozen=True)
class FsInfo:
    """`stat` 返回的目标元数据；`None` 表示目标缺失。"""

    version: FsVersion
    type: Literal["file", "directory", "other"]
    size: int | None = None


@dataclass(frozen=True)
class FsPathInfo:
    """路径级元数据（不跟随末段符号链接）；含 `symlink`。"""

    version: FsVersion
    type: Literal["file", "directory", "symlink", "other"]
    size: int | None = None


@dataclass(frozen=True)
class FsDirEntry:
    """`listDir` 返回的一个直接子项（仅元数据，不读内容）。"""

    name: str
    type: Literal["file", "directory", "other"]
    target: FsTarget
    version: FsVersion | None = None
    size: int | None = None


@dataclass(frozen=True)
class FsObservation:
    """一次权威观测：present 携带版本；absent 只授权受保护 create。"""

    kind: Literal["present", "absent"]
    version: FsVersion | None = None


@dataclass(frozen=True)
class FsWriteIntent:
    """受保护写意图：`createIfAbsent` 或 `replaceIfVersion`。"""

    kind: Literal["createIfAbsent", "replaceIfVersion"]
    version: FsVersion | None = None


@dataclass(frozen=True)
class FsWriteOutcome:
    """整文件写结果。`before` 为写前内容（create/无基线 → None），`after` 为 LF 归一化后内容。"""

    operation: Literal["create", "update"]
    version: FsVersion
    before: str | None
    after: str


@dataclass(frozen=True)
class FsEditRequest:
    """字面替换编辑请求。"""

    old_string: str
    new_string: str
    replace_all: bool = False


@dataclass(frozen=True)
class FsEditOutcome:
    """字面编辑结果（`before`/`after` 为原始存储文本，供消费者算 diff）。"""

    version: FsVersion
    before: str
    after: str


class FsError(Exception):
    """类型化文件系统错误：稳定 `code` + 可链式 `cause`。"""

    def __init__(self, message: str, code: FsErrorCode, cause: BaseException | None = None):
        super().__init__(message)
        self.code = code
        if cause is not None:
            self.__cause__ = cause
