"""两段整文件文本的行比较，超时退化为整文件替换。

上游对照：`packages/deliverables/workspace-changes/src/compare.ts`。
线比较用 Myers 差分（与上游 `diff` 的 Myers 同族）；`diffTimeoutMs` 到期时
退化为单个「旧侧全删 + 新侧全增」hunk，`coarse=True`。hunk 分组与上游一致：
变更之间相隔不超过 2*CONTEXT_LINES 个相同行时合并，否则各带 3 行上下文。
"""
from __future__ import annotations

import time
from typing import Any

__all__ = ["compare_text"]

CONTEXT_LINES = 3


class _Timeout(Exception):
    pass


def _terminated(text: str) -> str:
    return text if text == "" or text.endswith("\n") else text + "\n"


def _lines(text: str) -> list[str]:
    return [] if text == "" else text[:-1].split("\n")


def _myers(a: list[str], b: list[str], deadline: float) -> list[tuple[str, int, int, int]]:
    n, m = len(a), len(b)
    max_d = n + m
    v: dict[int, int] = {1: 0}
    trace: list[dict[int, int]] = []
    for d in range(max_d + 1):
        if time.monotonic() > deadline:
            raise _Timeout
        trace.append(dict(v))
        for k in range(-d, d + 1, 2):
            if k == -d or (k != d and v.get(k - 1, -1) < v.get(k + 1, -1)):
                x = v.get(k + 1, 0)
            else:
                x = v.get(k - 1, 0) + 1
            y = x - k
            while x < n and y < m and a[x] == b[y]:
                x += 1
                y += 1
            v[k] = x
            if x >= n and y >= m:
                return _backtrack(trace, a, b, d)
        if time.monotonic() > deadline:
            raise _Timeout
    raise AssertionError("Myers diff did not converge")


def _backtrack(trace: list[dict[int, int]], a: list[str], b: list[str],
               d: int) -> list[tuple[str, int, int, int]]:
    x, y = len(a), len(b)
    steps: list[tuple[str, int, int]] = []
    for depth in range(d, 0, -1):
        v = trace[depth]
        k = x - y
        if k == -depth or (k != depth and v.get(k - 1, -1) < v.get(k + 1, -1)):
            prev_k = k + 1
        else:
            prev_k = k - 1
        prev_x = v.get(prev_k, 0)
        prev_y = prev_x - prev_k
        while x > prev_x and y > prev_y:
            x -= 1
            y -= 1
            steps.append(("equal", x, y))
        if x == prev_x:
            y -= 1
            steps.append(("insert", x, y))
        else:
            x -= 1
            steps.append(("delete", x, y))
    while x > 0 and y > 0:
        x -= 1
        y -= 1
        steps.append(("equal", x, y))
    steps.reverse()
    ops: list[tuple[str, int, int, int]] = []
    for tag, i, j in steps:
        if ops and ops[-1][0] == tag and ops[-1][1] + ops[-1][3] == i \
                and ops[-1][2] + ops[-1][3] == j:
            prev = ops[-1]
            ops[-1] = (tag, prev[1], prev[2], prev[3] + 1)
        else:
            ops.append((tag, i, j, 1))
    return ops


def _stream(ops: list[tuple[str, int, int, int]], old: list[str],
            new: list[str]) -> list[dict[str, Any]]:
    """把 opcode 展开成带坐标的逐行流；同一位置的删除总在新增之前。"""
    stream: list[dict[str, Any]] = []
    old_pos = 0
    new_pos = 0
    index = 0
    while index < len(ops):
        tag, i, j, length = ops[index]
        if tag == "equal":
            for offset in range(length):
                stream.append({"tag": " ", "line": old[i + offset],
                               "old": i + offset, "new": j + offset})
            old_pos = i + length
            new_pos = j + length
            index += 1
            continue
        cluster: list[tuple[str, int, int, int]] = []
        while index < len(ops) and ops[index][0] != "equal":
            cluster.append(ops[index])
            index += 1
        deleted = 0
        inserted = 0
        for ctag, ci, cj, clen in cluster:
            if ctag == "delete":
                for offset in range(clen):
                    stream.append({"tag": "-", "line": old[ci + offset],
                                   "old": old_pos + deleted, "new": new_pos})
                deleted += clen
            else:
                for offset in range(clen):
                    stream.append({"tag": "+", "line": new[cj + offset],
                                   "old": old_pos, "new": new_pos + inserted})
                inserted += clen
        old_pos += deleted
        new_pos += inserted
    return stream


def _hunks(stream: list[dict[str, Any]], old_len: int) -> list[dict[str, Any]]:
    changes: list[tuple[int, int]] = []
    for item in stream:
        if item["tag"] == " ":
            continue
        start = item["old"]
        end = start + (1 if item["tag"] == "-" else 0)
        if not changes or start > changes[-1][1]:
            changes.append((start, end))
        else:
            changes[-1] = (changes[-1][0], max(changes[-1][1], end))
    if not changes:
        return []
    ranges: list[tuple[int, int]] = []
    for start, end in changes:
        low = max(0, start - CONTEXT_LINES)
        high = min(old_len, end + CONTEXT_LINES)
        if ranges and low <= ranges[-1][1]:
            ranges[-1] = (ranges[-1][0], max(ranges[-1][1], high))
        else:
            ranges.append((low, high))
    hunks: list[dict[str, Any]] = []
    for low, high in ranges:
        body: list[str] = []
        selected: list[dict[str, Any]] = []
        for item in stream:
            if item["tag"] == " ":
                if low <= item["old"] < high:
                    selected.append(item)
            elif item["tag"] == "-":
                if low <= item["old"] < high:
                    selected.append(item)
            elif low <= item["old"] <= high:
                selected.append(item)
        old_lines = sum(1 for item in selected if item["tag"] in " -")
        new_lines = sum(1 for item in selected if item["tag"] in " +")
        for item in selected:
            body.append(item["tag"] + item["line"])
        first = selected[0]
        old_start = low + 1 if old_lines > 0 else 1
        new_start = first["new"] + 1 if new_lines > 0 else 1
        hunks.append({
            "oldStart": old_start,
            "oldLines": old_lines,
            "newStart": new_start,
            "newLines": new_lines,
            "lines": body,
        })
    return hunks


def compare_text(before: str | None, after: str | None,
                 timeout_ms: float) -> dict[str, Any]:
    """逐行比较两段文本；`None` 表示文件在该侧不存在。"""
    old_text = _terminated(before or "")
    new_text = _terminated(after or "")
    old = _lines(old_text)
    new = _lines(new_text)
    deadline = time.monotonic() + max(timeout_ms, 0) / 1000.0
    try:
        ops = _myers(old, new, deadline)
        hunks = _hunks(_stream(ops, old, new), len(old))
        coarse = False
    except _Timeout:
        coarse = True
        hunks = [{
            "oldStart": 1,
            "oldLines": len(old),
            "newStart": 1,
            "newLines": len(new),
            "lines": [f"-{line}" for line in old] + [f"+{line}" for line in new],
        }]
    added = sum(1 for hunk in hunks for line in hunk["lines"] if line.startswith("+"))
    deleted = sum(1 for hunk in hunks for line in hunk["lines"] if line.startswith("-"))
    return {"hunks": hunks, "coarse": coarse, "added": added, "deleted": deleted}
