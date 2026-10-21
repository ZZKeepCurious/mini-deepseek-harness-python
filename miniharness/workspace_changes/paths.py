"""变更文件路径的分类与显示形式。

上游对照：`packages/deliverables/workspace-changes/src/paths.ts`。
"""
from __future__ import annotations

import os
import tempfile

__all__ = [
    "canonical_path",
    "compare_display",
    "display_path_of",
    "durable_path_of",
    "is_inside",
    "is_temporary_path",
    "temporary_roots",
    "to_posix",
]


def to_posix(path: str) -> str:
    return path.replace(os.sep, "/")


def is_inside(root: str, path: str) -> bool:
    try:
        common = os.path.commonpath([os.path.normcase(root), os.path.normcase(path)])
    except ValueError:
        return False
    return common == os.path.normcase(root)


def canonical_path(path: str) -> str:
    """符号链接解析后的路径；缺失的尾段经最近存在祖先解析。"""
    missing: list[str] = []
    head = path
    while True:
        try:
            return os.path.join(os.path.realpath(head), *reversed(missing))
        except OSError:
            parent = os.path.dirname(head)
            if parent == head:
                return path
            missing.append(os.path.basename(head))
            head = parent


def temporary_roots(candidates: tuple[str, ...] | None = None) -> list[str]:
    roots: list[str] = []
    for root in candidates or ("/tmp", tempfile.gettempdir()):
        for candidate in (root, canonical_path(root)):
            if candidate not in roots:
                roots.append(candidate)
    return roots


def is_temporary_path(path: str, roots: list[str]) -> bool:
    return any(is_inside(root, path) for root in roots)


def display_path_of(absolute: str, cwd: str, root: str, home: str) -> str:
    if is_inside(cwd, absolute) or is_inside(root, absolute):
        return to_posix(os.path.relpath(absolute, cwd))
    if home != "" and is_inside(home, absolute):
        return "~/" + to_posix(os.path.relpath(absolute, home))
    return to_posix(absolute)


def durable_path_of(absolute: str, cwd: str) -> str:
    return to_posix(os.path.relpath(absolute, cwd)) if is_inside(cwd, absolute) else absolute


def compare_display(a: dict, b: dict) -> int:
    left, right = a["display"], b["display"]
    return (left > right) - (left < right)
