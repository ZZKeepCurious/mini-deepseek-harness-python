"""Codex-style patch 解析与纯文本 hunk 应用（对齐 packages/fs/fs-patch）。"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

__all__ = [
    "ApplyHunksResult",
    "PatchHunk",
    "PatchOperation",
    "PatchParseError",
    "apply_hunks",
    "parse_patch",
]

_BEGIN = "*** Begin Patch"
_END = "*** End Patch"
_EOF = "*** End of File"
_MOVE = "*** Move to: "
_ADD = "*** Add File: "
_DELETE = "*** Delete File: "
_UPDATE = "*** Update File: "

_WS = re.compile(r"[\s\u0085\u200e\u200f\u2028\u2029]+", re.UNICODE)


def _trim(value: str) -> str:
    start = 0
    while start < len(value) and _WS.match(value[start : start + 1]):
        start += 1
    end = len(value)
    while end > start and _WS.match(value[end - 1 : end]):
        end -= 1
    return value[start:end]


def _trim_end(value: str) -> str:
    end = len(value)
    while end > 0 and _WS.match(value[end - 1 : end]):
        end -= 1
    return value[:end]


@dataclass
class _InputLine:
    text: str
    number: int


class PatchParseError(Exception):
    def __init__(self, line_number: int, reason: str):
        super().__init__(f"Invalid patch at line {line_number}: {reason}")
        self.line_number = line_number
        self.reason = reason


@dataclass
class PatchHunk:
    line_number: int
    old_lines: list[str] = field(default_factory=list)
    new_lines: list[str] = field(default_factory=list)
    is_end_of_file: bool = False
    context: str | None = None


@dataclass
class PatchOpAdd:
    kind: Literal["add"] = "add"
    path: str = ""
    line_number: int = 0
    content: str = ""


@dataclass
class PatchOpDelete:
    kind: Literal["delete"] = "delete"
    path: str = ""
    line_number: int = 0


@dataclass
class PatchOpUpdate:
    kind: Literal["update"] = "update"
    path: str = ""
    line_number: int = 0
    move_to: str | None = None
    hunks: list[PatchHunk] = field(default_factory=list)


PatchOperation = PatchOpAdd | PatchOpDelete | PatchOpUpdate


def _patch_lines(patch: str) -> list[_InputLine]:
    leading = patch[: len(patch) - len(patch.lstrip())]
    offset = leading.count("\n")
    lines = [
        _InputLine(text=text, number=offset + index + 1)
        for index, text in enumerate(_trim(patch).splitlines())
    ]
    if len(lines) >= 4 and lines[0].text in ("<<EOF", "<<'EOF'", '<<"EOF"') and lines[-1].text.endswith("EOF"):
        lines = lines[1:-1]
    if not lines:
        raise PatchParseError(1, f"Expected '{_BEGIN}'")
    if _trim(lines[0].text) != _BEGIN:
        raise PatchParseError(lines[0].number, f"Expected '{_BEGIN}'")
    if _trim(lines[-1].text) != _END:
        raise PatchParseError(lines[-1].number, f"Expected '{_END}'")
    return [
        _InputLine(text=line.text.replace("\r", "") if index < len(lines) - 1 else line.text, number=line.number)
        for index, line in enumerate(lines)
    ]


def _empty_hunk(hunk: PatchHunk) -> bool:
    return len(hunk.old_lines) == 0 and len(hunk.new_lines) == 0


def _ensure_update(operation: PatchOperation | None, line_number: int) -> None:
    if not isinstance(operation, PatchOpUpdate):
        return
    last = operation.hunks[-1] if operation.hunks else None
    if last is None:
        raise PatchParseError(operation.line_number, f"Update for '{operation.path}' requires at least one hunk")
    if _empty_hunk(last):
        raise PatchParseError(line_number, "Update hunk must contain context, added, or removed lines")


def _new_hunk(line_number: int) -> PatchHunk:
    return PatchHunk(line_number=line_number)


def _read_update(operation: PatchOpUpdate, line: _InputLine) -> None:
    text = _trim_end(line.text)
    previous = operation.hunks[-1] if operation.hunks else None
    marker = text == "@@" or text.startswith("@@ ")
    if previous is not None and previous.is_end_of_file:
        if text == "":
            return
        if not marker:
            raise PatchParseError(line.number, "Expected '@@' after '*** End of File'")
    if previous is None and operation.move_to is None and text.startswith(_MOVE):
        operation.move_to = text[len(_MOVE) :]
        return
    if marker:
        if previous is not None and _empty_hunk(previous):
            raise PatchParseError(line.number, "Update hunk must contain context, added, or removed lines")
        hunk = _new_hunk(line.number)
        if text.startswith("@@ "):
            hunk.context = text[3:]
        operation.hunks.append(hunk)
        return
    if text == _EOF:
        if previous is not None:
            if _empty_hunk(previous):
                raise PatchParseError(line.number, "Update hunk must contain context, added, or removed lines")
            previous.is_end_of_file = True
        return
    if line.text != "" and line.text[0] not in (" ", "+", "-"):
        raise PatchParseError(
            line.number,
            "Expected a context (' '), added ('+'), removed ('-') line, or '@@'",
        )
    hunk = previous if previous is not None else _new_hunk(line.number)
    if previous is None:
        operation.hunks.append(hunk)
    content = line.text[1:]
    prefix = line.text[0] if line.text else " "
    if prefix == "+":
        hunk.new_lines.append(content)
    elif prefix == "-":
        hunk.old_lines.append(content)
    else:
        hunk.old_lines.append(content)
        hunk.new_lines.append(content)


def parse_patch(patch: str) -> list[PatchOperation]:
    lines = _patch_lines(patch)
    operations: list[PatchOperation] = []
    ended = False
    for line in lines[1:]:
        operation = operations[-1] if operations else None
        header = (
            _trim_end(line.text)
            if isinstance(operation, PatchOpUpdate) and line is not lines[-1]
            else _trim(line.text)
        )
        if ended and line is not lines[-1]:
            if _trim(line.text) != "":
                raise PatchParseError(line.number, f"Unexpected content after '{_END}'")
            continue
        if header == _END:
            _ensure_update(operation, line.number)
            ended = True
        elif header.startswith(_ADD):
            _ensure_update(operation, line.number)
            operations.append(PatchOpAdd(path=header[len(_ADD) :], line_number=line.number))
        elif header.startswith(_DELETE):
            _ensure_update(operation, line.number)
            operations.append(PatchOpDelete(path=header[len(_DELETE) :], line_number=line.number))
        elif header.startswith(_UPDATE):
            _ensure_update(operation, line.number)
            operations.append(PatchOpUpdate(path=header[len(_UPDATE) :], line_number=line.number))
        elif not operations and header.startswith("*** Environment ID:"):
            raise PatchParseError(line.number, "Environment ID headers are unsupported; the caller selects the filesystem provider")
        elif isinstance(operation, PatchOpAdd) and line.text.startswith("+"):
            operation.content += f"{line.text[1:]}\n"
        elif isinstance(operation, PatchOpUpdate):
            _read_update(operation, line)
        else:
            raise PatchParseError(
                line.number,
                "Expected an Add File, Delete File, or Update File header; added content must start with +",
            )
    return operations


def _trim_ws(value: str, leading: bool) -> str:
    start = 0
    end = len(value)
    if leading:
        while start < end and _WS.match(value[start : start + 1]):
            start += 1
    while end > start and _WS.match(value[end - 1 : end]):
        end -= 1
    return value[start:end]


def _normalize_punctuation(value: str) -> str:
    return (
        _trim_ws(value, True)
        .replace("\u2010", "-")
        .replace("\u2011", "-")
        .replace("\u2012", "-")
        .replace("\u2013", "-")
        .replace("\u2014", "-")
        .replace("\u2015", "-")
        .replace("\u2212", "-")
        .replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201a", "'")
        .replace("\u201b", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u201e", '"')
        .replace("\u201f", '"')
        .replace("\u00a0", " ")
        .replace("\u2002", " ")
        .replace("\u2003", " ")
        .replace("\u2004", " ")
        .replace("\u2005", " ")
        .replace("\u2006", " ")
        .replace("\u2007", " ")
        .replace("\u2008", " ")
        .replace("\u2009", " ")
        .replace("\u200a", " ")
        .replace("\u202f", " ")
        .replace("\u205f", " ")
        .replace("\u3000", " ")
    )


_MATCH_PASSES = (
    lambda value: value,
    lambda value: _trim_ws(value, False),
    lambda value: _trim_ws(value, True),
    _normalize_punctuation,
)


def _seek_sequence(lines: list[str], pattern: list[str], start: int, eof: bool) -> int | None:
    if not pattern:
        return start
    if len(pattern) > len(lines):
        return None
    last = len(lines) - len(pattern)
    first = last if eof else start
    for normalize in _MATCH_PASSES:
        expected = [normalize(line) for line in pattern]
        for index in range(first, last + 1):
            if all(normalize(lines[index + offset]) == expected[offset] for offset in range(len(pattern))):
                return index
    return None


@dataclass
class HunkApplyFailure:
    hunk_line_number: int
    stage: Literal["context", "oldLines"]
    search_start: int
    pattern: list[str]


ApplyHunksResult = tuple[Literal[True], str] | tuple[Literal[False], HunkApplyFailure]


def apply_hunks(before: str, hunks: list[PatchHunk]) -> ApplyHunksResult:
    original = before.split("\n")
    if original and original[-1] == "":
        original.pop()
    replacements: list[tuple[int, int, list[str]]] = []
    line_index = 0
    for hunk in hunks:
        if hunk.context is not None:
            pattern = [hunk.context]
            found = _seek_sequence(original, pattern, line_index, False)
            if found is None:
                return False, HunkApplyFailure(hunk.line_number, "context", line_index, pattern)
            line_index = found + 1
        if len(hunk.old_lines) == 0:
            start = len(original) - 1 if original and original[-1] == "" else len(original)
            replacements.append((start, 0, list(hunk.new_lines)))
            continue
        pattern = list(hunk.old_lines)
        new_lines = list(hunk.new_lines)
        found = _seek_sequence(original, pattern, line_index, hunk.is_end_of_file)
        if found is None and pattern and pattern[-1] == "":
            pattern = pattern[:-1]
            if new_lines and new_lines[-1] == "":
                new_lines = new_lines[:-1]
            found = _seek_sequence(original, pattern, line_index, hunk.is_end_of_file)
        if found is None:
            return False, HunkApplyFailure(hunk.line_number, "oldLines", line_index, list(hunk.old_lines))
        replacements.append((found, len(pattern), new_lines))
        line_index = found + len(pattern)
    replacements.sort(key=lambda item: item[0])
    lines = original
    for start, old_length, new_lines in reversed(replacements):
        lines = lines[:start] + new_lines + lines[start + old_length :]
    if not lines or lines[-1] != "":
        lines.append("")
    return True, "\n".join(lines)
