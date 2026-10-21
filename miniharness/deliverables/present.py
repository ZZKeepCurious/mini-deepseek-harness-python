"""`present` 工具：声明最终交付文件（packages/deliverables/tool-present）。

上游在成功的 `tools/result` 之后追加 `deliverables/presented`；mini 无该事件总线
钩子，故在工具 execute 完成全部校验后立即追加——失败结果不会走到追加点。
"""
from __future__ import annotations

from typing import Any

from ..core.tools import Tool

__all__ = ["register_present_tool"]

DEFAULT_MAX_FILES = 8


def _presented_file(file: Any, index: int) -> dict:
    if not isinstance(file, dict):
        raise ValueError(f"present files[{index}] must be an object")
    if set(file) - {"path", "description"}:
        raise ValueError(f"present files[{index}] has unexpected fields")
    path = file.get("path")
    if not isinstance(path, str) or path.strip() == "":
        raise ValueError("present requires a non-empty file path")
    description = file.get("description")
    if description is not None and not isinstance(description, str):
        raise ValueError("present file description must be a string")
    row = {"path": path}
    if description is not None:
        row["description"] = description
    return row


def register_present_tool(reg, ctx, config: dict | None = None) -> Any:
    """注册 `present`（model-facing 工具）；返回 registry 的 disposer。"""
    max_files = (config or {}).get("maxFiles", DEFAULT_MAX_FILES)
    if not isinstance(max_files, int) or isinstance(max_files, bool) or max_files < 1:
        raise ValueError("present requires a positive integer maxFiles")
    fs = ctx.get("fs")
    projections = ctx.get("sessionProjections")
    working_directory = ctx.get("workingDirectory")
    if fs is None or projections is None or working_directory is None:
        raise RuntimeError(
            "present requires ctx.fs, ctx.sessionProjections and ctx.workingDirectory")

    async def execute(args: dict, exec_: Any) -> dict:
        agent = getattr(exec_, "agent", None)
        if agent is None:
            raise ValueError("present requires an agent Session")
        session = agent.session
        boundary = projections.state_of(session, "turnBoundary")
        if (not boundary or boundary.get("openTurnStartSeq") is None
                or not isinstance(boundary.get("lastTurn"), int)):
            raise ValueError("present requires an open turn")
        raw_files = args.get("files")
        if not isinstance(raw_files, list) or not 1 <= len(raw_files) <= max_files:
            raise ValueError(f"present accepts 1 to {max_files} files")
        files = [_presented_file(file, index) for index, file in enumerate(raw_files)]
        cwd = working_directory.get(session)
        signal = getattr(exec_, "signal", None)
        resolved = []
        for file in files:
            entry = await fs.lstat(file["path"], {"cwd": cwd, "signal": signal})
            if entry is not None and getattr(entry, "type", None) != "file":
                raise ValueError(f"Cannot present {file['path']}: not a regular file")
            target = await fs.resolve(file["path"], {"cwd": cwd, "signal": signal})
            info = await fs.stat(target, signal)
            if info is None:
                raise ValueError(
                    f"Cannot present {file['path']}: file not found. Check the path, "
                    "create the file if needed, and retry.")
            if getattr(info, "type", None) != "file":
                raise ValueError(f"Cannot present {file['path']}: not a regular file")
            row = {"path": fs.process_path(target)}
            if "description" in file:
                row["description"] = file["description"]
            resolved.append(row)
        session.append("deliverables/presented", {
            "turn": boundary["lastTurn"],
            "callId": getattr(exec_, "call_id", None),
            "files": resolved,
        })
        return {"turn": boundary["lastTurn"], "files": resolved}

    def render(_args: dict, value: dict) -> list[dict]:
        return [{"type": "text", "text": "\n".join(
            f"Presented {file['path']}" for file in value.get("files", []))}]

    tool = Tool(
        name="present",
        description=(
            "Declare existing files as final deliverables for the user. Use it when "
            "the user needs a separate file, especially Office documents, spreadsheets, "
            "and slide decks; prefer your final response when that suffices. The user "
            "opens the current files; their contents are not copied."),
        parameters={
            "type": "object",
            "properties": {
                "files": {
                    "type": "array",
                    "description": "Usually the 1-2 most important deliverables; at most 4 per call.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Path of an existing regular file. Relative paths use the Session working directory.",
                            },
                            "description": {
                                "type": "string",
                                "description": "Brief description for the user.",
                            },
                        },
                        "required": ["path"],
                    },
                },
            },
            "required": ["files"],
        },
        execute=execute,
        output={
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "turn": {"type": "integer"},
                    "files": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "path": {"type": "string"},
                                "description": {"type": "string"},
                            },
                            "required": ["path"],
                        },
                    },
                },
                "required": ["turn", "files"],
            },
            "render": render,
        },
    )
    return reg.register(tool)