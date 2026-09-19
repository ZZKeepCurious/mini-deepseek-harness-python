"""模型侧文件检索工具 `glob` / `grep`（对齐 packages/fs/tool-fs-search）。

**载体差异（登记）**：上游经打包的 ripgrep 二进制 + `ctx.subprocess.spawn` 执行
（固定 argv、spill store 溢出、采样、meta 预算）。mini 以 stdlib `os.walk` + `re`
承载等价发现语义：VCS 元数据目录排除、相对 workdir 路径、结果上限、模型可见分组输出。
regex 方言为 Python `re`（上游 ripgrep 语法）；`.gitignore` 感知与 spill 不承载。
"""
from __future__ import annotations

import os
import re
from typing import Any

from ..core.scope import Context
from ..core.tools import Tool

__all__ = [
    "GLOB_MAX_RESULTS",
    "GLOB_VCS_EXCLUDES",
    "GREP_MAX_LINE_BYTES",
    "GREP_MAX_MATCHES",
    "apply_glob_tool",
    "apply_grep_tool",
    "install_fs_search_tools",
    "parse_glob_args",
    "parse_grep_args",
]

GLOB_MAX_RESULTS = 100
GREP_MAX_MATCHES = 250
GREP_MAX_LINE_BYTES = 2000
GLOB_VCS_EXCLUDES = (".git", ".svn", ".hg", ".bzr", ".jj", ".sl")


def _session_cwd(exec: Any) -> str | None:
    agent = getattr(exec, "agent", None)
    session = getattr(agent, "session", None)
    meta = getattr(session, "meta", None)
    return meta.get("cwd") if isinstance(meta, dict) else None


def _workdir(exec: Any, path: str | None) -> str:
    if path is not None:
        base = _session_cwd(exec) or os.getcwd()
        return os.path.abspath(os.path.join(base, path))
    return os.path.abspath(_session_cwd(exec) or os.getcwd())


def parse_glob_args(args: dict) -> dict:
    if not isinstance(args.get("pattern"), str) or args["pattern"].strip() == "":
        raise ValueError("pattern must be a non-empty string")
    if args.get("path") is not None and (
            not isinstance(args["path"], str) or args["path"].strip() == ""):
        raise ValueError("path must be a non-empty string when given")
    out = {"pattern": args["pattern"]}
    if args.get("path") is not None:
        out["path"] = args["path"]
    return out


def _validate_include(include: str) -> None:
    if include.strip() == "":
        raise ValueError("include must be a non-empty glob when given")
    if include.startswith("!"):
        raise ValueError(
            'include must be a positive glob filter; negated patterns ("!…") are not supported')
    if include.count("{") != include.count("}"):
        raise ValueError("include has unbalanced braces")


def parse_grep_args(args: dict) -> dict:
    if not isinstance(args.get("pattern"), str) or args["pattern"] == "":
        raise ValueError("pattern must be a non-empty string")
    if args.get("path") is not None and (
            not isinstance(args["path"], str) or args["path"].strip() == ""):
        raise ValueError("path must be a non-empty string when given")
    if args.get("include") is not None:
        _validate_include(args["include"])
    out = {"pattern": args["pattern"]}
    if args.get("path") is not None:
        out["path"] = args["path"]
    if args.get("include") is not None:
        out["include"] = args["include"]
    return out


def _glob_to_regex(pattern: str) -> re.Pattern:
    if "/" not in pattern and os.sep not in pattern:
        pattern = "**/" + pattern
    out = ["^"]
    i = 0
    while i < len(pattern):
        char = pattern[i]
        if char == "*":
            if pattern[i:i + 2] == "**":
                out.append(".*")
                i += 2
                if pattern[i:i + 1] == "/":
                    out.append("/?")
                    i += 1
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    out.append("$")
    return re.compile("".join(out))


def _include_matcher(include: str) -> list:
    expanded = [include]
    if "{" in include:
        prefix, rest = include.split("{", 1)
        body, suffix = rest.split("}", 1)
        expanded = [f"{prefix}{part}{suffix}" for part in body.split(",")]
    return [_glob_to_regex(pat) for pat in expanded]


def _walk_files(root: str):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in GLOB_VCS_EXCLUDES]
        for name in filenames:
            yield os.path.join(dirpath, name)


def _relative(root: str, path: str) -> str:
    return os.path.relpath(path, root).replace(os.sep, "/")


def _run_glob(ctx: Context, args: dict, exec: Any, max_results: int) -> dict:
    input_ = parse_glob_args(args)
    root = _workdir(exec, input_.get("path"))
    matcher = _glob_to_regex(input_["pattern"])
    matches = []
    for path in _walk_files(root):
        rel = _relative(root, path)
        if matcher.match(rel):
            try:
                mtime = os.stat(path).st_mtime
            except OSError:
                mtime = 0.0
            matches.append((mtime, rel))
    matches.sort(key=lambda item: (item[0], item[1]))
    paths = [rel for _, rel in matches]
    return {"root": root, "paths": paths[:max_results], "total": len(paths)}


def _format_glob(value: dict) -> str:
    paths = value["paths"]
    body = "\n".join(paths)
    shown = len(paths)
    total = value["total"]
    if shown >= total:
        return f"{body}\n\n(Showing {shown} of {total} paths.)"
    return (f"{body}\n\n(Showing {shown} of {total} paths. "
            "Narrow pattern or path to see more.)")


def _truncate_bytes(text: str, max_bytes: int) -> str:
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    return raw[:max_bytes].decode("utf-8", errors="ignore")


def _run_grep(ctx: Context, args: dict, exec: Any, max_matches: int,
              max_line_bytes: int) -> dict:
    input_ = parse_grep_args(args)
    root = _workdir(exec, input_.get("path"))
    try:
        pattern = re.compile(input_["pattern"])
    except re.error as error:
        raise ValueError(f"invalid regular expression: {error}") from error
    includes = _include_matcher(input_["include"]) if input_.get("include") else None

    matches: list[dict] = []
    truncated = False
    for path in _walk_files(root):
        rel = _relative(root, path)
        if includes is not None and not any(rx.match(rel) for rx in includes):
            continue
        try:
            with open(path, "rb") as handle:
                raw = handle.read()
        except OSError:
            continue
        if b"\x00" in raw[:8192]:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        for number, line in enumerate(text.splitlines(), start=1):
            if pattern.search(line):
                if len(matches) >= max_matches:
                    truncated = True
                    break
                matches.append({"path": rel, "lineNumber": number,
                                "line": _truncate_bytes(line.rstrip("\r"), max_line_bytes)})
        if truncated:
            break
    return {"root": root, "matches": matches, "truncated": truncated}


def _format_grep(value: dict) -> str:
    matches = value["matches"]
    if not matches:
        return "No matches found."
    lines: list[str] = []
    current = None
    for match in matches:
        if match["path"] != current:
            current = match["path"]
            lines.append(f"{current}:")
        lines.append(f"  {match['lineNumber']}: {match['line']}")
    body = "\n".join(lines)
    if value["truncated"]:
        body += (f"\n\n(Showing the first {len(matches)} matches; narrow pattern or path "
                 "to see more.)")
    return body


def apply_glob_tool(ctx: Context, *, max_results: int = GLOB_MAX_RESULTS) -> Tool:
    tool = Tool(
        name="glob",
        description=("Find files whose paths match a glob pattern. Returns matching file "
                     "paths — never directories — including hidden and ignored files "
                     "(VCS metadata directories are excluded)."),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Glob pattern to match file paths against."},
                "path": {"type": "string", "description": "Directory to search in. Defaults to the session workspace."},
            },
            "required": ["pattern"],
        },
        execute=lambda args, exec: _glob_execute(ctx, args, exec, max_results),
        render=lambda args, value: [{"type": "text", "text": _format_glob(value)}],
    )
    ctx.get("tools").register(tool)
    return tool


async def _glob_execute(ctx: Context, args: dict, exec: Any, max_results: int) -> dict:
    return _run_glob(ctx, args, exec, max_results)


async def _grep_execute(ctx: Context, args: dict, exec: Any, max_matches: int,
                        max_line_bytes: int) -> dict:
    return _run_grep(ctx, args, exec, max_matches, max_line_bytes)


def apply_grep_tool(ctx: Context, *, max_matches: int = GREP_MAX_MATCHES,
                    max_line_bytes: int = GREP_MAX_LINE_BYTES) -> Tool:
    tool = Tool(
        name="grep",
        description=("Search file contents with a regular expression. Returns matching lines "
                     "with line numbers, grouped by file."),
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "Regular expression to search for."},
                "path": {"type": "string", "description": "File or directory to search. Defaults to the session workspace."},
                "include": {"type": "string", "description": 'One glob filter for which files to search (e.g. "*.ts").'},
            },
            "required": ["pattern"],
        },
        execute=lambda args, exec: _grep_execute(ctx, args, exec, max_matches, max_line_bytes),
        render=lambda args, value: [{"type": "text", "text": _format_grep(value)}],
    )
    ctx.get("tools").register(tool)
    return tool


def install_fs_search_tools(ctx: Context, *, glob_max_results: int = GLOB_MAX_RESULTS,
                            grep_max_matches: int = GREP_MAX_MATCHES,
                            grep_max_line_bytes: int = GREP_MAX_LINE_BYTES) -> dict:
    registry = ctx.get("tools")
    if registry is None:
        raise RuntimeError("tool-fs-search: ctx.tools is required")
    if registry.resolve("glob") is None:
        apply_glob_tool(ctx, max_results=glob_max_results)
    if registry.resolve("grep") is None:
        apply_grep_tool(ctx, max_matches=grep_max_matches, max_line_bytes=grep_max_line_bytes)
    return {"glob": registry.resolve("glob"), "grep": registry.resolve("grep")}
