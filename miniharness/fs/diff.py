"""write/edit 的结果态上下文 diff（对齐 tool-fs/src/diff.ts）。

存储返回 before/after 文本；本模块用 stdlib difflib 生成每个 hunk 的三行上下文卡片。
write 的落盘 meta 即 `FsDiffMeta`：`diffs` 恒在，`operation?: 'create' | 'update'`
区分「create 的空 hunk 列表」与「内容未变的不变覆盖」。
"""
from __future__ import annotations

import difflib

__all__ = ["DIFF_CONTEXT", "FS_DIFF_OPERATIONS", "compute_hunk_diffs",
           "diffs_from_meta", "fs_diff_meta"]

DIFF_CONTEXT = 3
FS_DIFF_OPERATIONS = ("create", "update")


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


def fs_diff_meta(path: str, before: str | None, after: str,
                 operation: str) -> dict:
    """构建 write 工具的落盘 meta（对齐 `FsDiffMeta` / `output.presentationMeta`）。

    `before is None`（create）→ 空 hunk 列表，与内容未变的不变覆盖同为 `diffs: []`，
    由 `operation` 区分；其余经 {@link compute_hunk_diffs} 生成应用 hunk。
    """
    if operation not in FS_DIFF_OPERATIONS:
        raise ValueError(f"unknown fs diff operation: {operation!r}")
    return {
        "operation": operation,
        "diffs": [] if before is None else compute_hunk_diffs(path, before, after),
    }


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
