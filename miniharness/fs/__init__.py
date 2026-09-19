"""fs 族：文件系统 Service Definition + 本地后端（上游 packages/fs/*）。

- `service.FileSystem` —— 抽象 `ctx.fs` 后端契约；
- `types` —— 目标/版本标识、元数据、写/编辑意图与结果、错误码闭集；
- `local.LocalFileSystem` —— 本地后端（对齐 fs-local）。
"""
from .local import LocalFileSystem, install_local_fs
from .observation_policy import ObservedStateGate, install_fs_observation_policy
from .present import apply_present_tool, install_present_tool
from .sandbox import SandboxedFileSystem, install_sandboxed_fs, is_path_under, writable_roots
from .search import apply_glob_tool, apply_grep_tool, install_fs_search_tools
from .service import FileSystem
from .str_replace_editor import install_str_replace_editor
from .tools import install_fs_tools
from .types import (
    FS_ERROR_CODES,
    FsDirEntry,
    FsEditOutcome,
    FsEditRequest,
    FsError,
    FsErrorCode,
    FsInfo,
    FsObservation,
    FsPathInfo,
    FsTarget,
    FsTargetKey,
    FsVersion,
    FsWriteIntent,
    FsWriteOutcome,
)

__all__ = [
    "FS_ERROR_CODES",
    "FileSystem",
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
    "FsWriteIntent",
    "FsWriteOutcome",
    "LocalFileSystem",
    "ObservedStateGate",
    "SandboxedFileSystem",
    "apply_glob_tool",
    "apply_grep_tool",
    "apply_present_tool",
    "install_fs_observation_policy",
    "install_fs_search_tools",
    "install_fs_tools",
    "install_local_fs",
    "install_present_tool",
    "install_sandboxed_fs",
    "install_str_replace_editor",
    "is_path_under",
    "writable_roots",
]
