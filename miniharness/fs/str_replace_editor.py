"""模型侧 `str_replace_editor`（对齐 packages/fs/tool-str-replace-editor）。

四个命令：`view`（文件 → `cat -n` 行号视图；目录 → 两层非隐藏列示）、`create`（目标不得
已存在）、`str_replace`（唯一字面替换）、`insert`（在第 N 行后插入）。路径须为绝对路径。
变更经 `ctx.fs.write_text` 的受保护意图（create → createIfAbsent；replace/insert →
replaceIfVersion）与观测版本；沙箱拒绝折算 `FS_SANDBOX_DENIED` 标记。
"""
from __future__ import annotations

import os
from typing import Any

from ..core.scope import Context
from ..core.tools import Tool
from .types import FsError, FsWriteIntent

__all__ = ["install_str_replace_editor", "parse_str_replace_args"]

TRUNCATED_MESSAGE = (
    "<response clipped><NOTE>To save on context only part of this file has been shown to you. "
    "You should retry this tool after you have searched inside the file with `grep -n` in order "
    "to find the line numbers of what you are looking for.</NOTE>")

_COMMANDS = ("view", "create", "str_replace", "insert")


def parse_str_replace_args(args: dict) -> dict:
    command = args.get("command")
    if command not in _COMMANDS:
        raise ValueError(
            f"command must be one of {list(_COMMANDS)}; received {command!r}")
    if not isinstance(args.get("path"), str) or args["path"].strip() == "":
        raise ValueError("path must be a non-empty string")
    return {"command": command, "path": args["path"],
            "file_text": args.get("file_text"), "old_str": args.get("old_str"),
            "new_str": args.get("new_str"), "insert_line": args.get("insert_line"),
            "view_range": args.get("view_range")}


def _required(value: Any, parameter: str, command: str) -> Any:
    if value is None:
        raise ValueError(f"Parameter `{parameter}` is required for command: {command}")
    return value


def _resolve_absolute(ctx: Context, path: str, exec: Any):
    if not os.path.isabs(path):
        raise ValueError(
            f"The path {path} is not an absolute path, it should start with `/`. "
            f"Maybe you meant /{path}?")
    return path


async def _stat_existing(ctx: Context, fs: Any, path: str, command: str, exec: Any):
    target = await fs.resolve(path, {"signal": getattr(exec, "signal", None)})
    info = await fs.stat(target, getattr(exec, "signal", None))
    if info is None:
        raise FsError(f"The path {target.display_path} does not exist. Please provide a valid path.",
                      "FS_NOT_FOUND")
    if info.type == "directory" and command != "view":
        raise FsError(
            f"The path {target.display_path} is a directory and only the `view` command can "
            "be used on directories", "FS_NOT_REGULAR_FILE")
    return target, info


async def _view(ctx: Context, fs: Any, args: dict, exec: Any) -> dict:
    path = args["path"]
    target = await fs.resolve(path, {"signal": getattr(exec, "signal", None)})
    info = await fs.stat(target, getattr(exec, "signal", None))
    if info is None:
        raise FsError(
            f"The path {target.display_path} does not exist. Please provide a valid path.",
            "FS_NOT_FOUND")
    if info.type == "directory":
        lines = _list_directory_2_levels(target.target_key, path)
        return {"path": target.display_path, "output": lines}
    content = await fs.read_text(target, getattr(exec, "signal", None))
    raw_lines = content.split("\n")
    view_range = args.get("view_range")
    if view_range is not None:
        if (not isinstance(view_range, list) or len(view_range) != 2
                or not all(isinstance(v, int) for v in view_range)):
            raise ValueError("view_range must be an array of two integers")
        start, end = view_range
        if start < 1:
            raise ValueError("view_range start must be a line number greater than 0")
        if end != -1 and end < start:
            raise ValueError("view_range end must be greater than or equal to start")
        selected = raw_lines[start - 1:len(raw_lines) if end == -1 else end]
        offset = start
    else:
        selected = raw_lines[:]
        offset = 1
    body = "\n".join(f"{offset + i:6}\t{text}" for i, text in enumerate(selected))
    if len(body) > 20000:
        body = body[:20000] + TRUNCATED_MESSAGE
    return {"path": target.display_path, "output": body}


def _list_directory_2_levels(root: str, display_root: str) -> str:
    lines = [f"Listing non-hidden files and directories under {display_root}:"]
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(name for name in dirnames if not name.startswith("."))
        filenames = sorted(name for name in filenames if not name.startswith("."))
        depth = os.path.relpath(dirpath, root)
        if depth != "." and depth.count(os.sep) >= 2:
            dirnames[:] = []
            continue
        for name in dirnames + filenames:
            rel = os.path.normpath(os.path.join(dirpath, name))
            lines.append(os.path.relpath(rel, root))
    return "\n".join(lines)


async def _create(ctx: Context, fs: Any, args: dict, exec: Any) -> dict:
    file_text = _required(args.get("file_text"), "file_text", "create")
    target = await fs.resolve(args["path"], {"signal": getattr(exec, "signal", None)})
    existing = await fs.stat(target, getattr(exec, "signal", None))
    if existing is not None:
        raise FsError(f"File already exists: {target.display_path}", "FS_NOT_OBSERVED")
    await fs.write_text(target, file_text, FsWriteIntent("createIfAbsent"),
                        getattr(exec, "signal", None))
    return {"path": target.display_path,
            "output": f"New file created successfully at: {target.display_path}"}


def _apply_replacement(content: str, old_str: str, new_str: str, path: str) -> str:
    occurrences = content.count(old_str)
    if occurrences == 0:
        raise FsError(
            f"No replacement was performed, old_str `{old_str}` did not appear verbatim in {path}.",
            "FS_EDIT_NOT_FOUND")
    if occurrences > 1:
        raise FsError(
            f"No replacement was performed. Multiple occurrences of old_str `{old_str}` in "
            f"{path}. Please ensure it is unique.", "FS_AMBIGUOUS_EDIT")
    return content.replace(old_str, new_str)


async def _str_replace(ctx: Context, fs: Any, args: dict, exec: Any) -> dict:
    old_str = _required(args.get("old_str"), "old_str", "str_replace")
    new_str = args.get("new_str")
    if new_str is None:
        new_str = ""
    target, info = await _stat_existing(ctx, fs, args["path"], "str_replace", exec)
    content = await fs.read_text(target, getattr(exec, "signal", None))
    replaced = _apply_replacement(content, old_str, new_str, target.display_path)
    await fs.write_text(target, replaced, FsWriteIntent("replaceIfVersion", info.version),
                        getattr(exec, "signal", None))
    return {"path": target.display_path,
            "output": f"The file {target.display_path} has been edited successfully."}


async def _insert(ctx: Context, fs: Any, args: dict, exec: Any) -> dict:
    insert_line = _required(args.get("insert_line"), "insert_line", "insert")
    new_str = _required(args.get("new_str"), "new_str", "insert")
    if not isinstance(insert_line, int) or isinstance(insert_line, bool):
        raise ValueError("insert_line must be an integer")
    target, info = await _stat_existing(ctx, fs, args["path"], "insert", exec)
    content = await fs.read_text(target, getattr(exec, "signal", None))
    lines = content.split("\n")
    if insert_line < 1 or insert_line > len(lines):
        raise ValueError(
            f"Invalid `insert_line` parameter: {insert_line}. It should be within the range of "
            f"lines in the file: [1, {len(lines)}]")
    lines.insert(insert_line, new_str)
    await fs.write_text(target, "\n".join(lines),
                        FsWriteIntent("replaceIfVersion", info.version),
                        getattr(exec, "signal", None))
    return {"path": target.display_path,
            "output": f"The file {target.display_path} has been edited successfully."}


async def _execute(ctx: Context, args: dict, exec: Any) -> dict:
    input_ = parse_str_replace_args(args)
    _resolve_absolute(ctx, input_["path"], exec)
    fs = ctx.get("fs")
    if input_["command"] == "view":
        return await _view(ctx, fs, input_, exec)
    if input_["command"] == "create":
        return await _create(ctx, fs, input_, exec)
    if input_["command"] == "str_replace":
        return await _str_replace(ctx, fs, input_, exec)
    return await _insert(ctx, fs, input_, exec)


def install_str_replace_editor(ctx: Context) -> Tool:
    registry = ctx.get("tools")
    if registry is None:
        raise RuntimeError("tool-str-replace-editor: ctx.tools is required")
    if registry.resolve("str_replace_editor") is not None:
        return registry.resolve("str_replace_editor")
    tool = Tool(
        name="str_replace_editor",
        description="Custom editing tool for viewing, creating and editing files",
        parameters={
            "type": "object",
            "properties": {
                "command": {"type": "string", "enum": list(_COMMANDS),
                            "description": "The command to run."},
                "path": {"type": "string", "description": "Absolute path to the file or directory."},
                "file_text": {"type": "string", "description": "Content for the `create` command."},
                "old_str": {"type": "string", "description": "Text to replace for `str_replace`."},
                "new_str": {"type": "string", "description": "Replacement text."},
                "insert_line": {"type": "integer", "description": "Line after which to insert."},
                "view_range": {"type": "array", "items": {"type": "integer"},
                               "description": "Optional [start, end] line range for `view`."},
            },
            "required": ["command", "path"],
        },
        execute=lambda args, exec: _execute(ctx, args, exec),
        render=lambda args, value: [{"type": "text", "text": value["output"]}],
    )
    registry.register(tool)
    return tool
