"""模型侧 `present`：声明会话文件交付物（对齐 packages/fs/tool-present）。

校验每个 path 解析为已存在普通文件（相对路径按会话工作区），去重、限 `max_files`，
返回 `{turn, files}`。

**载体差异（登记）**：上游经 `ctx.sessionProjections`（session-projection 注册 API v2，
即 M7）把交付物写入会话投影，使 UI 读取；mini 尚无投影注册表，本工具完成校验与结果形状，
投影声明面随 M7 立项补齐。
"""
from __future__ import annotations

from typing import Any

from ..core.scope import Context
from ..core.tools import Tool
from .types import FsError

__all__ = ["apply_present_tool", "install_present_tool"]

MAX_FILES = 8


def _session_cwd(exec: Any) -> str | None:
    agent = getattr(exec, "agent", None)
    session = getattr(agent, "session", None)
    meta = getattr(session, "meta", None)
    return meta.get("cwd") if isinstance(meta, dict) else None


def _current_turn(exec: Any) -> int:
    agent = getattr(exec, "agent", None)
    session = getattr(agent, "session", None)
    events = getattr(session, "events", None)
    if not events:
        return 0
    turn = 0
    for event in events:
        if event.get("type") == "turn/start":
            data = event.get("data") or {}
            if isinstance(data.get("turn"), int):
                turn = max(turn, data["turn"])
    return turn


def _parse_args(args: dict, max_files: int) -> list:
    files = args.get("files")
    if not isinstance(files, list) or not files:
        raise ValueError("files must be a non-empty array")
    if len(files) > max_files:
        raise ValueError(f"present accepts at most {max_files} files per call")
    parsed = []
    for entry in files:
        if not isinstance(entry, dict):
            raise ValueError("each file entry must be an object")
        path = entry.get("path")
        if not isinstance(path, str) or path.strip() == "":
            raise ValueError("file path must be a non-empty string")
        description = entry.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError("description must be a string when given")
        parsed.append({"path": path, "description": description})
    return parsed


async def _execute(ctx: Context, args: dict, exec: Any, max_files: int) -> dict:
    entries = _parse_args(args, max_files)
    fs = ctx.get("fs")
    resolved: list[dict] = []
    seen: set[str] = set()
    for entry in entries:
        target = await fs.resolve(entry["path"], {
            "cwd": _session_cwd(exec), "signal": getattr(exec, "signal", None)})
        info = await fs.stat(target, getattr(exec, "signal", None))
        if info is None or info.type != "file":
            raise FsError(
                f"presented path is not an existing regular file: {target.display_path}",
                "FS_NOT_FOUND")
        if target.target_key in seen:
            continue
        seen.add(target.target_key)
        resolved.append({"path": target.display_path,
                         "description": entry["description"]})
    return {"turn": _current_turn(exec), "files": resolved}


def apply_present_tool(ctx: Context, *, max_files: int = MAX_FILES) -> Tool:
    tool = Tool(
        name="present",
        description=(
            "Declare existing files accessible through the Session filesystem as final "
            "deliverables. When a file you create or update is an output the user asked to "
            "receive, you must call present after writing it and before your final response. "
            "The files must already exist."),
        parameters={
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {"type": "string", "description": "Path of an existing regular file."},
                            "description": {"type": "string", "description": "Brief description for the user."},
                        },
                        "required": ["path"],
                    },
                },
            },
            "required": ["files"],
        },
        execute=lambda args, exec: _execute(ctx, args, exec, max_files),
        render=lambda args, value: [{"type": "text", "text": _format(value)}],
    )
    ctx.get("tools").register(tool)
    return tool


def _format(value: dict) -> str:
    lines = [f"Presented {len(value['files'])} file(s) as deliverables:"]
    for entry in value["files"]:
        suffix = f" — {entry['description']}" if entry.get("description") else ""
        lines.append(f"- {entry['path']}{suffix}")
    return "\n".join(lines)


def install_present_tool(ctx: Context, *, max_files: int = MAX_FILES) -> Tool:
    registry = ctx.get("tools")
    if registry is None:
        raise RuntimeError("tool-present: ctx.tools is required")
    if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files < 1:
        raise ValueError("present requires a positive integer maxFiles")
    if registry.resolve("present") is not None:
        return registry.resolve("present")
    return apply_present_tool(ctx, max_files=max_files)
