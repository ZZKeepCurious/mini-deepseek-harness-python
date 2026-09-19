"""沙箱强制文件系统后端（对齐 packages/fs/fs-sandbox + containment.ts）。

`SandboxedFileSystem` 扩展 `LocalFileSystem`：文本存储机制（resolve/stat/读/列/原子写/编辑
临界区）逐字继承，本模块只加**每次调用的策略围栏**（仅两个变更入口）。读取一律放行。

围栏是对模型可控路径的**信任代码策略检查**，不是内核边界（内核级隔离是 bash-sandbox 的职责）：
- `read-only` 拒绝一切变更；
- `workspace-write` 仅当目标在策略 workspaceRoot（或平台临时区）之下时放行，且立即重新
  canonicalize 并返回**新目标**（消除 check-here-write-there TOCTOU）；
- `danger-full-access` 不放围栏。
拒绝抛 `FS_SANDBOX_DENIED`。
"""
from __future__ import annotations

import os
import tempfile
from typing import Any

from ..core.scope import Context
from .local import LocalFileSystem
from .types import (
    FsEditOutcome,
    FsEditRequest,
    FsError,
    FsTarget,
    FsWriteIntent,
    FsWriteOutcome,
)

__all__ = ["SandboxedFileSystem", "install_sandboxed_fs", "is_path_under", "writable_roots"]


def _canonical_path(path: str) -> str:
    return os.path.realpath(path)


def writable_roots(policy: dict) -> list[str]:
    """workspace-write 的可写根：workspaceRoot + '/tmp' + 平台临时目录（去重，对齐 roots.ts）。"""
    if policy.get("mode") != "workspace-write":
        return []
    candidates = [policy.get("workspaceRoot"), "/tmp", tempfile.gettempdir()]
    roots: list[str] = []
    for candidate in candidates:
        if not candidate:
            continue
        canonical = _canonical_path(candidate)
        if canonical not in roots:
            roots.append(canonical)
    return roots


def _same_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return left.st_dev == right.st_dev and left.st_ino == right.st_ino


def is_path_under(path: str, root: str, case_sensitive: bool | None = None) -> bool:
    """canonical 目标是否等于 root 或在其之下（对齐 containment.isPathUnder）。"""
    if case_sensitive is None:
        case_sensitive = os.name != "nt"

    def comparable(value: str) -> str:
        return value if case_sensitive else value.lower()

    target = comparable(path)
    base = comparable(root)
    prefix = base if base.endswith(os.sep) else base + os.sep
    if target == base or target.startswith(prefix):
        return True

    def stat_if_present(value: str) -> os.stat_result | None:
        try:
            return os.stat(value)
        except OSError as error:
            if error.errno in (2, 20):
                return None
            raise

    root_info = stat_if_present(root)
    if root_info is None:
        return False
    ancestor = path
    while True:
        ancestor_info = stat_if_present(ancestor)
        if ancestor_info is not None and _same_identity(ancestor_info, root_info):
            return True
        parent = os.path.dirname(ancestor)
        if parent == ancestor:
            return False
        ancestor = parent


class SandboxedFileSystem(LocalFileSystem):
    """按每次调用策略强制围栏的文件系统后端（注册为 `ctx.fs`）。"""

    def __init__(self, ctx: Context, config: dict | None = None,
                 *, default_mode: str | None = None):
        super().__init__(ctx, config)
        service = ctx.get("sandboxPolicy")
        self._default_mode = (default_mode
                              or getattr(service, "default_mode", None)
                              or "read-only")

    @property
    def sandbox_mode(self) -> str:
        return self._default_mode

    async def write_text(self, target: FsTarget, content: str,
                         expected: FsWriteIntent | None = None, signal: Any = None,
                         sandbox_policy: Any = None) -> FsWriteOutcome:
        return await super().write_text(
            await self._checked_target(target, sandbox_policy), content, expected, signal)

    async def edit_text(self, target: FsTarget, edit: FsEditRequest,
                        expected: dict | None = None, signal: Any = None,
                        sandbox_policy: Any = None) -> FsEditOutcome:
        return await super().edit_text(
            await self._checked_target(target, sandbox_policy), edit, expected, signal)

    async def _checked_target(self, target: FsTarget, sandbox_policy: Any) -> FsTarget:
        policy = sandbox_policy if sandbox_policy is not None else self._policy()
        mode = policy.get("mode")
        if mode == "danger-full-access":
            return target
        if mode == "read-only":
            raise FsError(
                f'cannot write "{target.display_path}": file access denied under read-only mode',
                "FS_SANDBOX_DENIED")
        fresh = await self.resolve(target.display_path)
        if not any(is_path_under(fresh.target_key, root) for root in writable_roots(policy)):
            raise FsError(
                f'cannot write "{target.display_path}": file access denied under workspace-write mode',
                "FS_SANDBOX_DENIED")
        return fresh

    def _policy(self) -> dict:
        service = self.ctx.get("sandboxPolicy")
        if service is not None:
            resolved = service.resolve()
            if resolved:
                return resolved
        return {"mode": self._default_mode, "workspaceRoot": self.config["cwd"]}


def install_sandboxed_fs(ctx: Context, config: dict | None = None,
                         *, default_mode: str | None = None) -> SandboxedFileSystem:
    """幂等装配沙箱文件系统后端（`ctx.fs`）。"""
    existing = ctx.get("fs")
    if existing is not None:
        return existing
    return SandboxedFileSystem(ctx, config, default_mode=default_mode)
