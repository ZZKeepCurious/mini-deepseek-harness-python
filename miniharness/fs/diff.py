"""write/edit 的结果态上下文 diff（对齐 tool-fs/src/diff.ts）。

存储返回 before/after 文本；本模块用 stdlib difflib 生成每个 hunk 的三行上下文卡片。
"""
from __future__ import annotations

import difflib

__all__ = ["DIFF_CONTEXT", "compute_hunk_diffs", "diffs_from_meta"]

DIFF_CONTEXT = 3


def compute_hunk_diffs(path: str, before: str, after: str) -> list[dict]:
    """before→after 每个应用 hunk 一个 `{path, oldText, newText}`（上下文 3 行）。

    纯插入 → oldText=None；仅尾部无换行标记不进入内容；散落替换保持独立 hunk。
    """
    before_lines = before.split("\n")
    after_lines = after.split("\n")
    matcher = difflib.SequenceMatcher(None, before_lines, after_lines, autojunk=False)
    diffs: list[dict] = []
    for group in matcher.get_grouped_opcodes(DIFF_CONTEXT):
        old_lines: list[str] = []
        new_lines: list[str] = []
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                old_lines.extend(before_lines[i1:i2])
                new_lines.extend(after_lines[j1:j2])
            elif tag == "delete":
                old_lines.extend(before_lines[i1:i2])
            elif tag == "insert":
                new_lines.extend(after_lines[j1:j2])
            else:  # replace
                old_lines.extend(before_lines[i1:i2])
                new_lines.extend(after_lines[j1:j2])
        diffs.append({
            "path": path,
            "oldText": "\n".join(old_lines) if old_lines else None,
            "newText": "\n".join(new_lines),
        })
    return diffs


def _is_file_diff(value: object) -> bool:
    if not isinstance(value, dict):
        return False
    path = value.get("path")
    old = value.get("oldText")
    new = value.get("newText")
    return (isinstance(path, str)
            and (old is None or isinstance(old, str))
            and isinstance(new, str))


def diffs_from_meta(meta: object) -> list[dict] | None:
    """从落盘 meta 收窄出非空 diff 列表；畸形 → None（回放兜底不抛）。"""
    if not isinstance(meta, dict):
        return None
    diffs = meta.get("diffs")
    if not isinstance(diffs, list) or not diffs or not all(_is_file_diff(d) for d in diffs):
        return None
    return diffs
