"""模型侧文件工具：`read` / `read_image` / `write` / `edit`（对齐 packages/fs/tool-fs）。

- `read`：一次 stat 决定类型/分派/观测版本；大文件或未知大小走流式；窗口化（offset/limit、
  单行/总字节上限）后 emit `fs/observed`。
- `read_image`：读 PNG/JPEG/WebP/GIF 并回图本体（无扩展名者按文件签名识别）；需挂
  attachment store；成功后 emit。路由须声明 image 输入。
- `write`：从 `fs/write-intent` 单槽取意图（无策略 → 无条件原子写），带沙箱策略；成功后 emit。
- `edit`：从 `fs/edit-intent` 单槽取版本守卫，字面替换；成功后 emit。
- 成功结果以 canonical `ctx.fs.process_path` 暴露路径（mutationResult / session-cwd 等价）。

工具注册进 `ctx.tools`。沙箱升级（`sandbox_permissions`/`justification`）经 `ctx.get("approval")`
（duck-typed，避免 L1 引 L3）；无审批服务时 fail loud。
"""
from __future__ import annotations

import os
from typing import Any, AsyncIterator, Iterable

from ..attachment.error import AttachmentError
from ..attachment.types import SaveImageAttachment
from ..core.code_language import read_lang_hint_for_path as lang_from_path
from ..core.scope import Context
from ..core.session import image_block
from ..core.tools import Tool
from ..core.working_cwd import working_directory_of
from .diff import fs_diff_meta
from .types import FsEditRequest, FsError, FsObservation

__all__ = [
    "READ_LIMIT",
    "READ_MAX_BYTES",
    "READ_MAX_LINE_LENGTH",
    "STREAM_MIN_SIZE",
    "apply_edit_tool",
    "apply_read_image_tool",
    "apply_read_tool",
    "apply_write_tool",
    "build_window",
    "format_edit_output",
    "format_image_read_output",
    "format_read_output",
    "format_write_output",
    "image_media_type_for_path",
    "install_fs_tools",
    "lang_from_path",
    "mutation_result",
    "parse_edit_args",
    "parse_read_args",
    "parse_write_args",
    "read_presentation_meta",
    "remediate_fs_error",
    "sandbox_permissions_description",
    "session_cwd",
    "sniff_image_media_type",
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
    """调用方 agent 会话当前工作目录（有效投影值）；无 agent → None。

    对齐上游：`workingDirectory.get(agent.session)`（相对路径基准随
    `working_directory` 工具的 cd 变更；服务未装时回退 `session.meta['cwd']`）。
    """
    agent = getattr(exec, "agent", None)
    if agent is None:
        return None
    return working_directory_of(agent)


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


def mutation_result(ctx: Context, target: Any, outcome: Any, exec: Any, result: dict) -> dict:
    """记录新文件版本并以 canonical 路径暴露变更结果（对齐 mutation-result.ts）。

    上游从成功 outcome 里剔除 `version`（版本留在观测事件里）再补 `path`；mini 的
    provider outcome 同样携带 `version`，由调用方显式列出其余结果字段。
    """
    ctx.emit("fs/observed", (target, FsObservation("present", outcome.version), exec))
    return {"path": ctx.get("fs").process_path(target), **result}


def sandbox_denial_marker(mode: str) -> str:
    return f"[sandbox: file access denied under {mode} mode]"


def escalation_hint_marker(subject: str) -> str:
    return (f"[sandbox: escalation available — retry this exact {subject} once with "
            "sandbox_permissions (the narrowest wider mode that suffices) + justification; "
            "the approval prompt asks the user]")


def sandbox_permissions_description(subject: str) -> str:
    """`sandbox_permissions` 参数说明（escalation.ts:95-97 逐字）。"""
    return (f"The narrowest wider sandbox mode for a one-shot retry of the exact "
            f"{subject} the sandbox just denied; the retry asks the user for approval.")


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
            getattr(exec, "signal", None),
            display_reason={
                "en": f"Allow this operation with {mode} permissions: "
                      f"{args['justification']}",
                "zh": f"允许本次操作使用 {mode} 权限：{args['justification']}",
            })
        if outcome == "allowed-once":
            return {**standing, "mode": mode}
        if outcome == "rejected":
            raise RuntimeError(
                f'the user rejected escalating this operation to "{mode}"; it '
                "stays denied, so stop and explain instead of working around it")
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
            "description": sandbox_permissions_description("operation"),
        },
        "justification": {
            "type": "string",
            "description": "Required with sandbox_permissions: one sentence for the user "
                           "explaining why this exact file operation needs the wider access. "
                           "Use the language of the user's current request.",
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


def read_presentation_meta(args: dict, value: dict) -> dict:
    """read 结果投影为持久 `meta`（对齐 read.ts:124-132）：结构化行窗口 + 短 `lang`。

    原始 canonical 值不上 wire，只有模型可见文本；行/语言数据必须在这里落到
    `meta`，重放时读卡片才能复原。
    """
    lang = lang_from_path(value["path"])
    meta = {
        "path": value["path"],
        "offset": value["offset"],
        "lines": [{"number": line["number"], "text": line["text"]}
                  for line in value["lines"]],
        "totalLines": value["totalLines"],
    }
    if lang is not None:
        meta["lang"] = lang
    return meta


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
        presentation_meta=read_presentation_meta,
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
    return {"path": ctx.get("fs").process_path(target), "offset": input_["offset"],
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
    intent = await ctx.awaterfall("fs/write-intent", (target, exec), base=lambda payload: None)
    try:
        outcome = await fs.write_text(target, input_["content"], intent,
                                      getattr(exec, "signal", None), policy)
    except BaseException as error:
        raise remediate_fs_error(gate.map_error(error, policy), target.display_path)
    return mutation_result(ctx, target, outcome, exec, {
        "operation": outcome.operation, "before": outcome.before, "after": outcome.after})


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
        intent = await ctx.awaterfall("fs/edit-intent", (target, exec), base=lambda payload: None)
        outcome = await fs.edit_text(
            target, FsEditRequest(input_["oldString"], input_["newString"], input_["replaceAll"]),
            intent, getattr(exec, "signal", None), policy)
    except BaseException as error:
        raise remediate_fs_error(gate.map_error(error, policy), target.display_path)
    return mutation_result(ctx, target, outcome, exec, {
        "before": outcome.before, "after": outcome.after})


_IMAGE_EXTENSIONS = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".gif": "image/gif",
}

_PNG_SIGNATURE = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A])
_JPEG_SIGNATURE = bytes([0xFF, 0xD8, 0xFF])


def _matches_bytes(data: bytes, offset: int, expected: bytes) -> bool:
    return data[offset:offset + len(expected)] == expected


def sniff_image_media_type(data: bytes) -> str | None:
    """按文件签名识别受支持图像媒体类型（对齐 sniffImageMediaType）。

    只读签名，不做完整解码——`attachments.save_image` 的完整解码才是权威。
    """
    if _matches_bytes(data, 0, _PNG_SIGNATURE):
        return "image/png"
    if _matches_bytes(data, 0, _JPEG_SIGNATURE):
        return "image/jpeg"
    if _matches_bytes(data, 0, b"GIF87a") or _matches_bytes(data, 0, b"GIF89a"):
        return "image/gif"
    if _matches_bytes(data, 0, b"RIFF") and _matches_bytes(data, 8, b"WEBP"):
        return "image/webp"
    return None


def image_media_type_for_path(file_path: str) -> str | None:
    """按扩展名声明媒体类型；非受支持扩展 → None（对齐 imageMediaTypeForPath）。"""
    return _IMAGE_EXTENSIONS.get(os.path.splitext(file_path)[1].lower())


def format_image_read_output(path: str, image: dict) -> str:
    """图片读的模型可见信封（对齐 formatImageReadOutput）。

    降采样读同时给出磁盘尺寸与坐标倍率（两轴四舍五入到同一值时只给一个倍率）。
    """
    scaled = ""
    original = image.get("originalDimensions")
    if original is not None:
        x = f"{original['width'] / image['width']:.2f}"
        y = f"{original['height'] / image['height']:.2f}"
        advice = (f"multiply coordinates by {x}" if x == y
                  else f"multiply x coordinates by {x} and y coordinates by {y}")
        scaled = (f" (downscaled from {original['width']}x{original['height']} px; "
                  f"{advice} to locate features in the original file)")
    return (f"<path>{path}</path>\n<type>image</type>\n<content>\n"
            f"{image['mediaType']} image, {image['width']}x{image['height']} px, "
            f"{image['bytes']} bytes{scaled}\n</content>")


def _assert_image_capable_route(exec: Any, requested_path: str) -> None:
    """严格判定调用路由是否声明 image 输入（对齐 assertImageCapableRoute）。

    mini 无 `ctx.llm`：以调用 Agent 的适配器当前模型信息为路由事实。未解析出
    provider/model、或未声明 image 输入 → 拒绝（比宿主上传预检更严格）。
    """
    agent = getattr(exec, "agent", None)
    adapter = getattr(agent, "adapter", None) if agent is not None else None
    resolve = getattr(adapter, "resolve_model_info", None)
    info = resolve() if callable(resolve) else None
    if not isinstance(info, dict) or info.get("model") is None:
        raise RuntimeError(
            f'cannot read "{requested_path}" as an image: the current model route '
            "could not be resolved")
    modalities = info.get("input_modalities")
    if not modalities or "image" not in modalities:
        raise RuntimeError(
            f'cannot read "{requested_path}" as an image: model "{info.get("model")}" '
            "does not declare image input; switch to an image-capable model to read images")


def _assert_deployment_accepts(attachments: Any, media_type: str, display_path: str) -> None:
    if media_type not in attachments.image_limits.mediaTypes:
        raise RuntimeError(
            f'cannot read "{display_path}": {media_type} images are not accepted by this deployment')


def _map_read_image_error(error: AttachmentError, limits: Any, declared: str | None,
                          extension: str, media_type: str,
                          display_path: str) -> BaseException:
    """attachment 错误 → 可恢复的模型可见诊断（对齐 read-image.ts 的 catch）。"""
    code = getattr(error, "code", None)
    if code == "IMAGE_DIMENSION_TOO_LARGE":
        return RuntimeError(
            f'cannot read "{display_path}": at least one image side exceeds the '
            f"{limits.maxImageDimension}px limit; downscale the image and read the smaller copy")
    if code == "IMAGE_TOO_MANY_PIXELS":
        return RuntimeError(
            f'cannot read "{display_path}": the image exceeds the {limits.maxImagePixels}-pixel '
            "decoded-size limit; downscale the image and read the smaller copy")
    if code == "IMAGE_TOO_LARGE":
        return RuntimeError(
            f'cannot read "{display_path}": the image cannot be stored within the deployment\'s '
            "byte limits; downscale the image and read the smaller copy")
    if code == "ATTACHMENT_WRITE_FAILED" and "16-bit PNG" in str(error):
        return RuntimeError(
            f'cannot read "{display_path}": the 16-bit PNG could not be converted to the '
            "normalized 8-bit sRGB form; convert it to an 8-bit PNG/JPEG/WebP and retry")
    if code == "INVALID_IMAGE" and declared is None:
        return RuntimeError(
            f'cannot read "{display_path}": the bytes do not decode as a supported '
            "PNG/JPEG/WebP/GIF image; the file may be truncated or corrupt")
    if code != "IMAGE_TYPE_MISMATCH":
        return error
    if declared is None:
        return RuntimeError(
            f'cannot read "{display_path}": the file signature claims {media_type}, but the '
            "bytes decode as a different image format; the file may be corrupt")
    return RuntimeError(
        f'cannot read "{display_path}": the {extension} extension declares {media_type}, but the '
        "bytes use a different image format; rename the file to match its actual format if it is "
        "PNG/JPEG/WebP/GIF, or convert it to one of those formats")


def apply_read_image_tool(ctx: Context) -> Tool:
    """注册 `read_image`（对齐 applyReadImageTool）。

    组装方负责 attachments 门：`install_fs_tools` 仅在挂 attachment store 时注册；
    执行体仍对直接调用方复查 `ctx.get("attachments")`。
    """
    async def execute(args: dict, exec_: Any) -> dict:
        file_path = args.get("file_path")
        if not isinstance(file_path, str) or file_path.strip() == "":
            raise RuntimeError("file_path must be a non-empty string")
        # 所有读前门都在任何文件 I/O 之前（拒绝不泄漏部分读或附件写）。
        extension = os.path.splitext(file_path)[1].lower()
        declared = image_media_type_for_path(file_path)
        if declared is None and extension != "":
            raise RuntimeError(
                f'cannot read "{file_path}": the {extension} extension does not declare a '
                "supported image format; read_image accepts PNG/JPEG/WebP/GIF files, including "
                "extension-less files in those formats")
        attachments = ctx.get("attachments")
        if attachments is None:
            raise RuntimeError(
                f'cannot read "{file_path}" as an image: no attachment service is mounted')
        if declared is not None:
            _assert_deployment_accepts(attachments, declared, file_path)
        _assert_image_capable_route(exec_, file_path)

        target, info = await _resolve_regular_read_target(ctx, exec_, file_path)
        limits = attachments.image_limits
        byte_cap = min(limits.maxImageBytes, limits.maxMessageImageBytes)
        data = await ctx.get("fs").read_bytes(target, getattr(exec_, "signal", None), byte_cap)
        media_type = declared or sniff_image_media_type(data)
        if media_type is None:
            raise RuntimeError(
                f'cannot read "{target.display_path}": the file content is not a supported image '
                "format; read_image accepts PNG/JPEG/WebP/GIF")
        if declared is None:
            _assert_deployment_accepts(attachments, media_type, target.display_path)
        try:
            ref = attachments.save_image(SaveImageAttachment(
                data=data, mediaType=media_type, name=os.path.basename(target.display_path)))
        except AttachmentError as error:
            raise _map_read_image_error(error, limits, declared, extension, media_type,
                                        target.display_path) from error
        ctx.emit("fs/observed", (target, FsObservation("present", info.version), exec_))
        image = {"attachmentId": str(ref.attachmentId), "mediaType": ref.mediaType,
                 "bytes": ref.bytes, "width": ref.width, "height": ref.height}
        if ref.name is not None:
            image["name"] = ref.name
        if ref.originalDimensions is not None:
            image["originalDimensions"] = ref.originalDimensions.to_dict()
        return {"path": ctx.get("fs").process_path(target), "image": image}

    tool = Tool(
        name="read_image",
        description=("Read a PNG/JPEG/WebP/GIF file and return the image itself. Large images "
                     "are downscaled automatically; do not install image libraries or create "
                     "thumbnails to inspect an image."),
        parameters={
            "type": "object",
            "properties": {
                "file_path": {"type": "string",
                              "description": "Path to the image file, resolved by the filesystem backend."},
            },
            "required": ["file_path"],
        },
        is_concurrency_safe=True,
        execute=execute,
        render=lambda args, value: [
            {"type": "text", "text": format_image_read_output(value["path"], value["image"])},
            image_block(dict(value["image"])),
        ],
        presentation_meta=lambda args, value: {"path": value["path"]},
    )
    ctx.get("tools").register(tool)
    return tool


def _mount_read_image_tool(image_ctx: Context, _config: Any = None) -> None:
    """inject 插件体：deps = attachments，装载时挂 read_image（签名 (ctx, config)）。"""
    apply_read_image_tool(image_ctx)


def install_fs_tools(ctx: Context, *, read: bool = True, read_image: bool = True,
                     write: bool = True, edit: bool = True,
                     read_limit: int = READ_LIMIT,
                     read_max_line_length: int = READ_MAX_LINE_LENGTH,
                     read_max_bytes: int = READ_MAX_BYTES,
                     read_stream_min_size: int = STREAM_MIN_SIZE) -> dict:
    """注册选定的文件工具（已注册同名则跳过）。返回工具表。

    注册开关对齐上游 Config：`read`/`read_image`/`write`/`edit`（缺省 true）。
    `read_image` 仅在有 attachment store 时注册（无持久化不可落盘图片字节）。
    """
    registry = ctx.get("tools")
    if registry is None:
        raise RuntimeError("tool-fs: ctx.tools is required")
    for name, value in (("readLimit", read_limit), ("readMaxLineLength", read_max_line_length),
                        ("readMaxBytes", read_max_bytes),
                        ("readStreamMinSize", read_stream_min_size)):
        if not isinstance(value, int) or isinstance(value, bool) or value < 1:
            raise ValueError(f"tool-fs: {name} must be a positive integer")
    gate = FsSandboxGate(ctx)
    if read and registry.resolve("read") is None:
        apply_read_tool(ctx, limit=read_limit, max_line_length=read_max_line_length,
                        max_bytes=read_max_bytes, stream_min_size=read_stream_min_size)
    # read_image 是组装条件：无挂载 attachment store 的部署不能持久化图片字节，
    # 缺失依赖的 fiber 保持 PENDING（对齐上游 ctx.inject(['attachments'])——
    # 服务到场才注册）；execute 体保留对直接调用方的防御性复查。
    if read_image:
        ctx.inject(["attachments"], _mount_read_image_tool)
    if write and registry.resolve("write") is None:
        apply_write_tool(ctx, gate)
    if edit and registry.resolve("edit") is None:
        apply_edit_tool(ctx, gate)
    return {"read": registry.resolve("read"), "read_image": registry.resolve("read_image"),
            "write": registry.resolve("write"), "edit": registry.resolve("edit")}
