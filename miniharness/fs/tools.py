"""模型侧文件工具：`read` / `write` / `edit`（对齐 packages/fs/tool-fs）。

- `read`：一次 stat 决定类型/分派/观测版本；大文件或未知大小走流式；窗口化（offset/limit、
  单行/总字节上限）后 emit `fs/observed`。
- `write`：从 `fs/write-intent` 单槽取意图（无策略 → 无条件原子写），带沙箱策略；成功后 emit。
- `edit`：从 `fs/edit-intent` 单槽取版本守卫，字面替换；成功后 emit。

工具注册进 `ctx.tools`。沙箱升级（`sandbox_permissions`/`justification`）经 `ctx.get("approval")`
（duck-typed，避免 L1 引 L3）；无审批服务时 fail loud。
"""
from __future__ import annotations

from typing import Any, AsyncIterator, Iterable

from ..core.scope import Context
from ..core.tools import Tool
from .diff import fs_diff_meta
from .types import FsEditRequest, FsError, FsObservation

__all__ = [
    "READ_LIMIT",
    "READ_MAX_BYTES",
    "READ_MAX_LINE_LENGTH",
    "STREAM_MIN_SIZE",
    "apply_edit_tool",
    "apply_read_tool",
    "apply_write_tool",
    "build_window",
    "format_edit_output",
    "format_read_output",
    "format_write_output",
    "install_fs_tools",
    "lang_from_path",
    "parse_edit_args",
    "parse_read_args",
    "parse_write_args",
    "remediate_fs_error",
    "session_cwd",
    "write_presentation_meta",
]

READ_LIMIT = 2000
READ_MAX_LINE_LENGTH = 2000
READ_MAX_BYTES = 50 * 1024
STREAM_MIN_SIZE = 10 * 1024 * 1024

WIDER_MODES = {
    "read-only": ("workspace-write", "danger-full-access"),
    "workspace-write": ("danger-full-access",),
}
ESCALATION_TARGETS = ("workspace-write", "danger-full-access")


def session_cwd(exec: Any) -> str | None:
    """调用方 agent 会话工作区（`exec.agent.session.meta.cwd`）；无 agent → None。"""
    agent = getattr(exec, "agent", None)
    session = getattr(agent, "session", None)
    meta = getattr(session, "meta", None)
    return meta.get("cwd") if isinstance(meta, dict) else None


def _session_resolve_options(exec: Any, workspace_root: str | None = None) -> dict:
    cwd = workspace_root or session_cwd(exec)
    opts: dict = {"signal": getattr(exec, "signal", None)}
    if cwd is not None:
        opts["cwd"] = cwd
    return opts


# ---------- read 窗口 ----------


def _parse_positive_integer(value: Any, name: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return value


def parse_read_args(args: dict, max_limit: int) -> dict:
    if not isinstance(args.get("file_path"), str) or args["file_path"].strip() == "":
        raise ValueError("file_path must be a non-empty string")
    offset = 1 if args.get("offset") is None else _parse_positive_integer(args["offset"], "offset")
    limit = max_limit if args.get("limit") is None else _parse_positive_integer(args["limit"], "limit")
    if limit > max_limit:
        raise ValueError(f"limit must be less than or equal to {max_limit}")
    return {"filePath": args["file_path"], "offset": offset, "limit": limit}


def _truncate_line(line: str, max_line_length: int) -> str:
    if len(line) <= max_line_length:
        return line
    return f"{line[:max_line_length]}... (line truncated to {max_line_length} chars)"


def _line_byte_size(line: str, current_line_count: int) -> int:
    return len(line.encode("utf-8")) + (1 if current_line_count > 0 else 0)


async def build_window(chunks: Any, request: dict, display_path: str) -> dict:
    """从（同步或异步）文本块构建窗口，扫描到精确总行数，越界 offset 抛 FS_NOT_FOUND。"""
    acc = {"lines": [], "totalLines": 0, "outputBytes": 0, "truncatedByBytes": False}
    line_cap = request["maxLineLength"] + 1
    line_buffer = ""
    pending = ""

    def consume(raw_line: str) -> None:
        acc["totalLines"] += 1
        if (acc["truncatedByBytes"] or acc["totalLines"] < request["offset"]
                or len(acc["lines"]) >= request["limit"]):
            return
        text = _truncate_line(raw_line, request["maxLineLength"])
        size = _line_byte_size(text, len(acc["lines"]))
        if acc["outputBytes"] + size > request["maxBytes"]:
            acc["truncatedByBytes"] = True
            return
        acc["outputBytes"] += size
        acc["lines"].append({"number": acc["totalLines"], "text": text})

    def feed(segment: str) -> None:
        nonlocal line_buffer
        if len(line_buffer) < line_cap:
            line_buffer = (line_buffer + segment)[:line_cap]

    def flush() -> None:
        nonlocal line_buffer
        consume(line_buffer[:-1] if line_buffer.endswith("\r") else line_buffer)
        line_buffer = ""

    def handle_text(text: str) -> None:
        nonlocal pending
        parts = (pending + text).split("\n")
        pending = parts[-1]
        for part in parts[:-1]:
            feed(part)
            flush()

    async def run_async(source: AsyncIterator[str]) -> None:
        nonlocal pending
        async for chunk in source:
            handle_text(chunk)
        if pending:
            feed(pending)
            pending = ""

    def run_sync(source: Iterable[str]) -> None:
        nonlocal pending
        for chunk in source:
            handle_text(chunk)
        if pending:
            feed(pending)
            pending = ""

    if hasattr(chunks, "__aiter__"):
        await run_async(chunks)
    else:
        run_sync(chunks)
    if line_buffer:
        flush()

    if (not acc["truncatedByBytes"] and request["offset"] > acc["totalLines"]
            and not (acc["totalLines"] == 0 and request["offset"] == 1)):
        raise FsError(
            f'offset {request["offset"]} is out of range for "{display_path}" '
            f"({acc['totalLines']} lines)", "FS_NOT_FOUND")
    return {"lines": acc["lines"], "totalLines": acc["totalLines"],
            "truncatedByBytes": acc["truncatedByBytes"]}


def format_read_output(display_path: str, outcome: dict) -> str:
    lines = outcome["lines"]
    end_line = lines[-1]["number"] if lines else max(0, outcome["offset"] - 1)
    if outcome.get("truncatedByBytes"):
        footer = (f"(Output capped. Showing lines {outcome['offset']}-{end_line}. "
                  f"Use offset={end_line + 1} to continue.)")
    elif end_line < outcome["totalLines"]:
        footer = (f"(Showing lines {outcome['offset']}-{end_line} of {outcome['totalLines']}. "
                  f"Use offset={end_line + 1} to continue.)")
    else:
        footer = f"(End of file - total {outcome['totalLines']} lines)"
    body = "\n".join(f"{line['number']}: {line['text']}" for line in lines)
    body = f"{body}\n\n{footer}" if lines else footer
    return f"<path>{display_path}</path>\n<type>file</type>\n<content>\n{body}\n</content>"


_LANG_BY_EXTENSION = {
    "ts": "ts", "tsx": "tsx", "mts": "ts", "cts": "ts",
    "js": "js", "jsx": "jsx", "mjs": "js", "cjs": "js",
    "json": "json", "jsonc": "json",
    "py": "py", "rb": "rb", "go": "go", "rs": "rs", "java": "java",
    "c": "c", "h": "c", "cc": "cpp", "cpp": "cpp", "hpp": "cpp", "cxx": "cpp",
    "cs": "cs", "kt": "kotlin", "swift": "swift", "php": "php",
    "sh": "sh", "bash": "sh", "zsh": "sh",
    "yaml": "yaml", "yml": "yaml", "toml": "toml", "ini": "ini",
    "md": "md", "markdown": "md", "mdx": "mdx",
    "html": "html", "htm": "html", "css": "css", "scss": "scss", "less": "less",
    "sql": "sql", "xml": "xml", "lua": "lua",
}


def lang_from_path(path: str) -> str | None:
    base = path.replace("\\", "/").rsplit("/", 1)[-1]
    dot = base.rfind(".")
    if dot <= 0:
        return None
    return _LANG_BY_EXTENSION.get(base[dot + 1:].lower())


# ---------- 错误改写 / 沙箱 ----------


def remediate_fs_error(error: BaseException, display_path: str) -> BaseException:
    """守卫式变更失败的模型可见诊断（对齐 tool-fs/src/error.ts）。"""
    if not isinstance(error, FsError):
        return error
    if error.code == "FS_NOT_OBSERVED":
        return FsError(
            f'cannot modify "{display_path}": file has not been read — read the file, then retry',
            error.code, error)
    if error.code == "FS_STALE_VERSION":
        return FsError(f"{error} — re-read the file, then retry", error.code, error)
    return error


def sandbox_denial_marker(mode: str) -> str:
    return f"[sandbox: file access denied under {mode} mode]"


def escalation_hint_marker(subject: str) -> str:
    return (f"[sandbox: escalation available — retry this exact {subject} once with "
            "sandbox_permissions (the narrowest wider mode that suffices) + justification; "
            "the approval prompt asks the user]")


def _validate_escalation_args(sandbox_permissions: Any, justification: Any) -> None:
    if sandbox_permissions is not None and justification is None:
        raise ValueError("invalid escalation: sandbox_permissions requires a justification")
    if justification is not None and sandbox_permissions is None:
        raise ValueError(
            "invalid escalation: justification is only valid together with sandbox_permissions")
    if justification is not None and justification.strip() == "":
        raise ValueError("invalid justification: expected a non-empty sentence")


class FsSandboxGate:
    """write/edit 共享的沙箱升级 API（对齐 tool-fs/src/sandbox.ts）。"""

    def __init__(self, ctx: Context):
        fs = ctx.get("fs")
        default_mode = getattr(fs, "sandbox_mode", None)
        self.escalation_modes = () if default_mode is None else ESCALATION_TARGETS
        self._policy = ctx.get("sandboxPolicy") if default_mode is not None else None
        if default_mode is not None and self._policy is None:
            raise RuntimeError(
                "tool-fs: the mounted filesystem confines but ctx.sandboxPolicy is missing")

    def resolve_policy(self, tool_name: str, args: dict, exec: Any) -> dict | None:
        _validate_escalation_args(args.get("sandbox_permissions"), args.get("justification"))
        agent = getattr(exec, "agent", None)
        session = getattr(agent, "session", None)
        standing = (self._policy.resolve({"session": session} if session is not None else {})
                    if self._policy is not None else None)
        if args.get("sandbox_permissions") is None:
            return standing
        if not self.escalation_modes:
            raise RuntimeError(
                "sandbox_permissions is not available in this composition "
                "(no sandboxing filesystem to escalate)")
        mode = args["sandbox_permissions"]
        effective = (standing or {}).get("mode")
        if mode not in WIDER_MODES.get(effective, ()):
            raise RuntimeError(
                f'sandbox escalation to "{mode}" is not strictly wider than this call\'s '
                f'current "{effective}" mode')
        approval = self._policy.ctx.get("approval") if self._policy is not None else None
        if approval is None:
            raise RuntimeError(
                f'sandbox escalation to "{mode}" requires approval, but no approval service is composed')
        if session is None:
            raise RuntimeError(
                f'sandbox escalation to "{mode}" requires approval, but the call has no agent to route it through')
        outcome = approval.request(
            session, tool_name, getattr(exec, "call_id", None),
            f"escalate sandbox to {mode}: {args['justification']}",
            getattr(exec, "signal", None))
        if outcome == "allowed-once":
            return {**standing, "mode": mode}
        if outcome == "rejected":
            raise RuntimeError(f'the user rejected escalating this operation to "{mode}"')
        if outcome == "cancelled":
            raise RuntimeError(f'approval for escalating to "{mode}" was cancelled')
        raise RuntimeError(
            f'sandbox escalation to "{mode}" requires approval, but no approval channel is available')

    def map_error(self, error: BaseException, policy: dict | None) -> BaseException:
        if not isinstance(error, FsError) or error.code != "FS_SANDBOX_DENIED":
            return error
        mode = (policy or {}).get("mode", "read-only")
        return FsError(f"{sandbox_denial_marker(mode)}\n{escalation_hint_marker('operation')}",
                       "FS_SANDBOX_DENIED", error)


# ---------- 目标解析 / 工具 ----------


async def _resolve_regular_read_target(ctx: Context, exec: Any, requested_path: str) -> tuple:
    fs = ctx.get("fs")
    target = await fs.resolve(requested_path, _session_resolve_options(exec))
    info = await fs.stat(target, getattr(exec, "signal", None))
    if info is None:
        ctx.emit("fs/observed", (target, FsObservation("absent"), exec))
        raise FsError(f'cannot read "{target.display_path}": not found', "FS_NOT_FOUND")
    if info.type != "file":
        raise FsError(f'cannot read "{target.display_path}": not a regular file',
                      "FS_NOT_REGULAR_FILE")
    return target, info


def _escalation_schema_fields(gate: FsSandboxGate) -> dict:
    return {
        "sandbox_permissions": {
            "type": "string", "enum": list(gate.escalation_modes),
            "description": "The wider sandbox mode this file operation needs. Only valid as a "
                           "one-shot retry of an operation the sandbox just denied; requires "
                           "justification and user approval.",
        },
        "justification": {
            "type": "string",
            "description": "Required with sandbox_permissions: one sentence for the user "
                           "explaining why this exact file operation needs the wider access.",
        },
    }


def parse_write_args(args: dict) -> dict:
    if not isinstance(args.get("file_path"), str) or args["file_path"].strip() == "":
        raise ValueError("file_path must be a non-empty string")
    return {"filePath": args["file_path"], "content": args.get("content", "")}


def format_write_output(display_path: str, operation: str) -> str:
    verb = "Created" if operation == "create" else "Updated"
    return (f"<path>{display_path}</path>\n<type>file</type>\n<content>\n{verb} file\n</content>")


def write_presentation_meta(args: dict, value: dict) -> dict:
    """write 落盘 meta（对齐 write.ts output.presentationMeta）：operation + diffs。

    diff 路径用模型给出的原始 `file_path`（同上游 `args.file_path`）；create 的
    空 hunk 列表与不变覆盖由 `operation` 区分。
    """
    return fs_diff_meta(args["file_path"], value.get("before"),
                        value.get("after"), value["operation"])


def parse_edit_args(args: dict) -> dict:
    if not isinstance(args.get("file_path"), str) or args["file_path"].strip() == "":
        raise ValueError("file_path must be a non-empty string")
    old = args.get("old_string")
    new = args.get("new_string")
    if not isinstance(old, str) or old == "":
        raise ValueError("old_string must be a non-empty string")
    if old == new:
        raise ValueError("old_string and new_string must differ")
    return {"filePath": args["file_path"], "oldString": old, "newString": new,
            "replaceAll": bool(args.get("replace_all", False))}


def format_edit_output(display_path: str, replace_all: bool) -> str:
    if replace_all:
        return (f"The file {display_path} has been updated. "
                "All occurrences were successfully replaced.")
    return f"The file {display_path} has been updated successfully."


def apply_read_tool(ctx: Context, *, limit: int, max_line_length: int,
                    max_bytes: int, stream_min_size: int) -> Tool:
    tool = Tool(
        name="read",
        description="Read a UTF-8 text file and return line-numbered content.",
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "Path to read, resolved by the filesystem backend."},
                "offset": {"type": "number", "description": "1-based first line to return. Defaults to 1."},
                "limit": {"type": "number", "description": f"Maximum number of lines to return. Defaults to {limit}."},
            },
            "required": ["file_path"],
        },
        is_concurrency_safe=True,
        execute=lambda args, exec: _read_execute(ctx, args, exec, limit,
                                                max_line_length, max_bytes, stream_min_size),
        render=lambda args, value: [{"type": "text", "text": format_read_output(
            value["path"], {"offset": value["offset"], "lines": value["lines"],
                            "totalLines": value["totalLines"]})}],
    )
    ctx.get("tools").register(tool)
    return tool


async def _read_execute(ctx: Context, args: dict, exec: Any, limit: int,
                        max_line_length: int, max_bytes: int, stream_min_size: int) -> dict:
    input_ = parse_read_args(args, limit)
    fs = ctx.get("fs")
    target, info = await _resolve_regular_read_target(ctx, exec, input_["filePath"])
    if info.size is None or info.size >= stream_min_size:
        chunks = fs.stream_text(target, getattr(exec, "signal", None))
    else:
        chunks = [await fs.read_text(target, getattr(exec, "signal", None))]
    window = await build_window(chunks, {
        "offset": input_["offset"], "limit": input_["limit"],
        "maxLineLength": max_line_length, "maxBytes": max_bytes,
    }, target.display_path)
    ctx.emit("fs/observed", (target, FsObservation("present", info.version), exec))
    return {"path": target.display_path, "offset": input_["offset"],
            "lines": window["lines"], "totalLines": window["totalLines"]}


def apply_write_tool(ctx: Context, gate: FsSandboxGate) -> Tool:
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to write, resolved by the filesystem backend."},
            "content": {"type": "string", "description": "Full UTF-8 text content to write."},
        },
        "required": ["file_path", "content"],
    }
    if gate.escalation_modes:
        parameters["properties"].update(_escalation_schema_fields(gate))
    tool = Tool(
        name="write",
        description="Create or fully replace a UTF-8 text file.",
        parameters=parameters,
        execute=lambda args, exec: _write_execute(ctx, gate, args, exec),
        render=lambda args, value: [{"type": "text", "text": format_write_output(
            value["path"], value["operation"])}],
        presentation_meta=write_presentation_meta,
    )
    ctx.get("tools").register(tool)
    return tool


async def _write_execute(ctx: Context, gate: FsSandboxGate, args: dict, exec: Any) -> dict:
    input_ = parse_write_args(args)
    policy = gate.resolve_policy("write", args, exec)
    fs = ctx.get("fs")
    target = await fs.resolve(input_["filePath"], _session_resolve_options(
        exec, policy.get("workspaceRoot") if policy else None))
    intent = ctx.waterfall("fs/write-intent", (target, exec), base=lambda payload: None)
    try:
        outcome = await fs.write_text(target, input_["content"], intent,
                                      getattr(exec, "signal", None), policy)
    except BaseException as error:
        raise remediate_fs_error(gate.map_error(error, policy), target.display_path)
    ctx.emit("fs/observed", (target, FsObservation("present", outcome.version), exec))
    return {"path": target.display_path, "operation": outcome.operation,
            "before": outcome.before, "after": outcome.after}


def apply_edit_tool(ctx: Context, gate: FsSandboxGate) -> Tool:
    parameters = {
        "type": "object",
        "properties": {
            "file_path": {"type": "string", "description": "Path to edit, resolved by the filesystem backend."},
            "old_string": {"type": "string", "description": "Literal text to replace. Must match exactly."},
            "new_string": {"type": "string", "description": "Literal replacement text. Use an empty string to delete the match."},
            "replace_all": {"type": "boolean", "description": "Replace all matches. Defaults to false; when false, old_string must appear exactly once."},
        },
        "required": ["file_path", "old_string", "new_string"],
    }
    if gate.escalation_modes:
        parameters["properties"].update(_escalation_schema_fields(gate))
    tool = Tool(
        name="edit",
        description="Edit an existing UTF-8 text file by replacing literal text.",
        parameters=parameters,
        execute=lambda args, exec: _edit_execute(ctx, gate, args, exec),
        render=lambda args, value: [{"type": "text", "text": format_edit_output(
            value["path"], bool(args.get("replace_all", False)))}],
    )
    ctx.get("tools").register(tool)
    return tool


async def _edit_execute(ctx: Context, gate: FsSandboxGate, args: dict, exec: Any) -> dict:
    input_ = parse_edit_args(args)
    policy = gate.resolve_policy("edit", args, exec)
    fs = ctx.get("fs")
    target = await fs.resolve(input_["filePath"], _session_resolve_options(
        exec, policy.get("workspaceRoot") if policy else None))
    try:
        intent = ctx.waterfall("fs/edit-intent", (target, exec), base=lambda payload: None)
        outcome = await fs.edit_text(
            target, FsEditRequest(input_["oldString"], input_["newString"], input_["replaceAll"]),
            intent, getattr(exec, "signal", None), policy)
    except BaseException as error:
        raise remediate_fs_error(gate.map_error(error, policy), target.display_path)
    ctx.emit("fs/observed", (target, FsObservation("present", outcome.version), exec))
    return {"path": target.display_path, "before": outcome.before, "after": outcome.after}


def install_fs_tools(ctx: Context, *, read_limit: int = READ_LIMIT,
                     read_max_line_length: int = READ_MAX_LINE_LENGTH,
                     read_max_bytes: int = READ_MAX_BYTES,
                     read_stream_min_size: int = STREAM_MIN_SIZE) -> dict:
    """注册 read/write/edit 三工具（已注册同名则跳过）。返回工具表。"""
    registry = ctx.get("tools")
    if registry is None:
        raise RuntimeError("tool-fs: ctx.tools is required")
    for name, value in (("readLimit", read_limit), ("readMaxLineLength", read_max_line_length),
                        ("readMaxBytes", read_max_bytes),
                        ("readStreamMinSize", read_stream_min_size)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"tool-fs: {name} must be a positive integer")
    gate = FsSandboxGate(ctx)
    if registry.resolve("read") is None:
        apply_read_tool(ctx, limit=read_limit, max_line_length=read_max_line_length,
                        max_bytes=read_max_bytes, stream_min_size=read_stream_min_size)
    if registry.resolve("write") is None:
        apply_write_tool(ctx, gate)
    if registry.resolve("edit") is None:
        apply_edit_tool(ctx, gate)
    return {"read": registry.resolve("read"), "write": registry.resolve("write"),
            "edit": registry.resolve("edit")}
