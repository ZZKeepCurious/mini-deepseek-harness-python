"""工具桥：发现 MCP 工具、以确定性 server 限定公共名注册进 harness 工具注册表、
并在 server 工具列表变化时重新同步。

对应 dsh 真实源码：packages/mcp/mcp-client/src/tools.ts。

命名契约（上游 Agent Note "Naming invariants"）：每个 MCP 工具稳定身份为
`(serverName, rawName)`；模型面对的公共名是 `mcp__<serverName>__<rawName>`，
规范化到 DeepSeek 函数名约束。raw name 只在 wire 上发送（tools/call）；
公共名永不解析回调 raw name。

图像投影：MCP 结果中的 image 块按上游 decode → admission → save 流程落
durable 附件；任何拒绝把全部图像投成文本诊断，canonical 原始值仍留给程序化
调用方。finalize_content 三条件相等检查在冻结内容域（MappingProxyType/
tuple）内做深比较（mini 载体适配，语义与上游 isDeepStrictEqual 对齐）。
"""
from __future__ import annotations

import base64
import hashlib
import re
from typing import Any, Callable, Mapping
from weakref import WeakKeyDictionary

from mcp.types import Tool as McpSdkTool

from ..attachment.error import is_image_admission_error
from ..attachment.types import SaveImageAttachment
from ..core.scope import Context
from ..core.tools import Tool, ToolExec

__all__ = [
    "MAX_PUBLIC_NAME_LENGTH",
    "INVALID_NAME_CHARS",
    "HASH_LENGTH",
    "public_tool_name",
    "sync_tools",
    "create_mcp_tool_definition",
    "McpToolDefinitionOptions",
    "ToolBridgeOptions",
    "extract_text",
    "project_content",
    "frost_equal",
    "_structured_to_dict",
]

#: DeepSeek 函数名契约：最多 64 字符（wire 协议常量，非配置）。
MAX_PUBLIC_NAME_LENGTH = 64

#: DeepSeek 函数名契约：只允许 `[A-Za-z0-9_-]`。
INVALID_NAME_CHARS = re.compile(r"[^A-Za-z0-9_-]")

#: 有损规范化时附加的 SHA-256 身份哈希 hex 长度。
HASH_LENGTH = 12

#: durable 附件词汇表支持的栅格格式。
IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/webp", "image/gif"})

#: 规范 RFC 4648 base64，排除空白与 URL-safe 别名。
CANONICAL_BASE64 = re.compile(r"^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$")


class ToolBridgeOptions:
    """已解析的桥接选项。"""

    __slots__ = ("registrationFailure", "serverName", "toolCallTimeoutMs")

    def __init__(self, registration_failure: str, server_name: str, tool_call_timeout_ms: int):
        self.registrationFailure = registration_failure
        self.serverName = server_name
        self.toolCallTimeoutMs = tool_call_timeout_ms


def public_tool_name(server_name: str, raw_name: str) -> str:
    """衍生一个 MCP 工具的模型面对公共名（确定性纯函数）。

    干净情形 = `mcp__<serverName>__<rawName>` 逐字。字符替换或截断到
    DeepSeek 函数名契约（64 字符）改变名字时，附加 SHA-256 身份的 12 hex
    字符，不同 MCP 身份永不塌缩进同一公共名。
    """
    joined = f"mcp__{server_name}__{raw_name}"
    normalized = INVALID_NAME_CHARS.sub("_", joined)
    if normalized == joined and len(normalized) <= MAX_PUBLIC_NAME_LENGTH:
        return normalized
    digest = hashlib.sha256(f"{server_name}\0{raw_name}".encode("utf-8")).hexdigest()
    hash_part = digest[:HASH_LENGTH]
    return f"{normalized[:MAX_PUBLIC_NAME_LENGTH - HASH_LENGTH - 1]}_{hash_part}"


def _as_json_schema(schema: Any) -> dict:
    """pydantic 模型或裸 dict 都归一为 JSON Schema dict。"""
    if isinstance(schema, dict):
        return schema
    if hasattr(schema, "model_dump"):
        return schema.model_dump(exclude_none=True, mode="json")
    return {}


def _structured_to_dict(value: Any) -> Any:
    """structuredContent 的 pydantic 模型 → JSON dict（否则原样）。"""
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return value


async def sync_tools(
    client: Any,
    ctx: Context,
    opts: ToolBridgeOptions,
    previous: dict[str, Callable],
) -> dict[str, Callable]:
    """把 MCP server 的工具列表同步进 harness 工具注册表。

    两阶段保证换代为安全：
    1. 抓取：聚合 `tools/list` 并在公共名下构建下一整代 ToolDefinition。任何
       失败（网络错误或重复 raw name）在此拒绝，前一代注册保持不动。
    2. 换代：拆掉前一代 disposer，注册新一代。此处注册冲突只可能是外部注册
       占据了本 server 的 `mcp__<serverName>__` 命名空间——回滚部分代（本
       server 零工具）并记录。初始严格同步可传播冲突让父事务拒绝；普通客户端
       与后续重新同步返回空 map。

    @param client - 已连接的用户侧桥接对象（list_tools/call_tool）。
    @param ctx - 提供 `tools` 服务的上下文（注册入口）。
    @param opts - 桥接选项：server 命名空间 + 单次调用超时。
    @param previous - 前一同步代的 disposer map；抓取成功后 swap 阶段拆掉。
    @returns 公共名 → 注销 disposer 的 map，即本 server 拥有的全部活跃注册。
    """
    # Phase 1: 不触碰注册表，构建下一整代。
    definitions: dict[str, Tool] = {}
    caps = getattr(client, "server_capabilities", None)
    tools: list[McpSdkTool] = [] if caps is None or getattr(caps, "tools", None) is None \
        else await client.list_tools()
    for tool in tools:
        public_name = public_tool_name(opts.serverName, tool.name)
        if public_name in definitions:
            raise RuntimeError(
                f'mcp-client({opts.serverName}): server listed tool "{tool.name}" '
                "more than once — invalid tool list")
        task_required = tool.execution is not None and getattr(tool.execution, "taskSupport", None) == "required"
        definitions[public_name] = create_mcp_tool_definition(ctx, {
            "name": public_name,
            "rawName": tool.name,
            "description": tool.description or "",
            "inputSchema": _as_json_schema(tool.input_schema),
            "outputSchema": tool.output_schema,
            "taskRequired": task_required,
            "call": (lambda name: (lambda args, exec_: client.call_tool(name, args, exec_)))(tool.name),
        })

    # Phase 2: swap 换代。
    for dispose in previous.values():
        dispose()
    disposers: dict[str, Callable] = {}
    registry = ctx.get("tools")
    if registry is None:
        raise RuntimeError("no tools registry is mounted")
    try:
        for public_name, definition in definitions.items():
            disposers[public_name] = registry.register(definition)
    except Exception as error:  # noqa: BLE001 - contain/throw 语义由 opts 决定
        # 命名空间被外部注册占据：回滚，模型只见整代或零代，绝不见部分集。
        for dispose in disposers.values():
            dispose()
        if opts.registrationFailure == "throw":
            raise
        return {}
    return disposers


class McpToolDefinitionOptions:
    """一个上游 MCP 工具 + 获取其原始协议结果的回调。"""

    __slots__ = ("name", "rawName", "description", "inputSchema", "outputSchema",
                 "taskRequired", "call")

    def __init__(self, name, raw_name, description, input_schema, output_schema,
                 task_required, call: Callable[[dict, ToolExec], Any]):
        self.name = name
        self.rawName = raw_name
        self.description = description
        self.inputSchema = input_schema
        self.outputSchema = output_schema
        self.taskRequired = task_required
        self.call = call


def frost_equal(a: Any, b: Any) -> bool:
    """冻结内容域深比较：MappingProxyType==dict、tuple==list，递归。

    对齐上游 isDeepStrictEqual 对 result.value / result.content 与投影
    fallback 的三条件相等检查；mini 成功态 content 是 deep_freeze 结果
    （tuple of MappingProxyType），故在冻结域比较。
    """
    if isinstance(a, Mapping) or isinstance(b, Mapping):
        if not (isinstance(a, Mapping) and isinstance(b, Mapping)):
            return False
        if set(a.keys()) != set(b.keys()):
            return False
        return all(frost_equal(a[k], b[k]) for k in a.keys())
    if isinstance(a, (tuple, list)) or isinstance(b, (tuple, list)):
        if not (isinstance(a, (tuple, list)) and isinstance(b, (tuple, list))):
            return False
        if len(a) != len(b):
            return False
        return all(frost_equal(x, y) for x, y in zip(a, b))
    if isinstance(a, (int, bool)) and isinstance(b, (int, bool)):
        return a == b
    return a == b


def create_mcp_tool_definition(
    ctx: Context,
    options: McpToolDefinitionOptions | dict,
) -> Tool:
    """把一个上游 MCP 工具适配为 canonical 值 + durable 图片内容。

    注册、provider 生命周期、期限与传输都属于调用方。
    """
    if not isinstance(options, McpToolDefinitionOptions):
        options = McpToolDefinitionOptions(
            name=options["name"],
            raw_name=options["rawName"],
            description=options.get("description", ""),
            input_schema=options.get("inputSchema", {}),
            output_schema=options.get("outputSchema"),
            task_required=options.get("taskRequired", False),
            call=options["call"],
        )
    projections: "WeakKeyDictionary[ToolExec, dict]" = WeakKeyDictionary()
    output_schema, render = create_output(options.rawName, options.outputSchema)

    async def execute(args: Any, exec_: ToolExec) -> Any:
        if options.taskRequired:
            raise RuntimeError(
                f'Tool "{options.rawName}" requires task-based execution, which this bridge does not support')
        args_obj = args if isinstance(args, dict) else {}
        result = await options.call(args_obj, exec_)
        text = extract_text(result.get("content") or [], options.rawName)
        if result.get("isError") is True:
            raise RuntimeError(text)
        value = {"content": result.get("content") or []}
        if result.get("structuredContent") is not None:
            value["structuredContent"] = _structured_to_dict(result["structuredContent"])
        if contains_image(value["content"]):
            fallback = [{"type": "text", "text": extract_text(value["content"], options.rawName)}]
            projected = prepare_image_projection(ctx, exec_, value["content"], options.rawName)
            projections[exec_] = {"value": value, "fallback": fallback, "content": projected}
        return value

    def finalize_content(exec_: ToolExec, box: dict) -> Any:
        projection = projections.get(exec_)
        if projection is None:
            return None
        projections.pop(exec_, None)
        if box.get("is_error"):
            return None
        if not frost_equal(box.get("value"), projection["value"]):
            return None
        if not frost_equal(box.get("content"), projection["fallback"]):
            return None
        return projection["content"]

    return Tool(
        name=options.name,
        description=options.description,
        execute=execute,
        parameters=options.inputSchema,
        output=output_schema,
        render=render,
        finalize_content=finalize_content,
    )


def create_output(raw_name: str, structured_schema: Any) -> tuple[dict, Callable]:
    """构建 canonical 结果 schema + 既有纯文本投影（上游 createOutput）。"""
    structured = _as_json_schema(structured_schema) if structured_schema is not None else None
    schema = {
        "type": "object",
        "properties": {
            "content": {"type": "array", "items": {}},
            "structuredContent": structured if structured is not None else {},
        },
        "required": ["content"] if structured is None else ["content", "structuredContent"],
        "additionalProperties": False,
    }

    def render(args: dict, value: Any) -> list[dict]:
        content = value.get("content") if isinstance(value, dict) else []
        return [{"type": "text", "text": extract_text(content or [], raw_name)}]

    return schema, render


def contains_image(content: list) -> bool:
    """一个不可信 MCP 内容数组是否声明了 image 块。"""
    return any(is_record(value) and value.get("type") == "image" for value in content)


def is_record(value: Any) -> bool:
    return isinstance(value, dict)


def extract_text(content: list, tool_name: str) -> str:
    """从 MCP 内容数组抽取单个字符串：
    - text 块以 '\\n' 连接
    - image/audio/resource 块替换为占位符
    """
    projected = project_content(content, tool_name)
    return "\n".join(block["text"] for block in projected if isinstance(block, dict) and block.get("type") == "text")


def _image_diagnostic(block: dict, reason: str) -> str:
    """一个未被受理的 image 块的稳定诊断文本。"""
    media_type = block.get("mimeType") or "unknown media type"
    return (f"[image unavailable: {media_type}; {reason}; "
            "raw image data remains available to programmatic callers]")


def project_content(
    content: list,
    tool_name: str,
    image: Callable[[dict, int], dict] | None = None,
) -> list[dict]:
    """把有序 MCP 块投影到 core 内容词汇。

    text 类 runs 以 '\\n' 合并；受理的 image 在原位切开这些 runs。
    默认 image 回调产出诊断文本（未受理到 durable 模型上下文）。
    """
    if image is None:
        default_reason = "this result was not admitted to durable model context"

        def image(block: dict, index: int) -> dict:  # noqa: F811
            return {"type": "text", "text": _image_diagnostic(block, default_reason)}

    projected: list[dict] = []
    text: list[str] = []

    def flush_text() -> None:
        if not text:
            return
        projected.append({"type": "text", "text": "\n".join(text)})
        text.clear()

    for index, value in enumerate(content):
        if not is_record(value):
            text.append("[unsupported MCP content block: expected an object]")
            continue
        block_type = value.get("type")
        if block_type == "text":
            if value.get("text") is not None:
                text.append(str(value["text"]))
        elif block_type == "image":
            flush_text()
            projected.append(image(value, index))
        elif block_type == "resource_link":
            if value.get("name") is None or value.get("uri") is None:
                text.append("[resource link unavailable: the MCP block is missing its name or URI]")
            else:
                text.append(f"Resource link: {value['name']} ({value['uri']})")
        elif block_type == "audio":
            media_type = value.get("mimeType") or "unknown media type"
            text.append(f"[audio result unsupported: {media_type}; raw audio data remains available to programmatic callers]")
        elif block_type == "resource":
            text.append("[embedded resource unsupported; raw resource data remains available to programmatic callers]")
        else:
            text.append(f"[unsupported MCP content type: {block_type}]")
    flush_text()
    return projected if projected else [{"type": "text", "text": f"({tool_name} returned no model-visible content)"}]


def decode_image(block: dict) -> SaveImageAttachment:
    """解码一个待投影的 image 块，拒绝 base64 别名与非栅格媒体类型。"""
    media_type = block.get("mimeType")
    if media_type not in IMAGE_MEDIA_TYPES:
        raise RuntimeError("the declared media type is not PNG, JPEG, WebP, or GIF")
    data = block.get("data") or ""
    if not isinstance(data, str) or not CANONICAL_BASE64.match(data):
        raise RuntimeError("the image data is not canonical base64")
    try:
        decoded = base64.b64decode(data, validate=True)
    except Exception as error:  # noqa: BLE001 - 映射为稳定诊断文本
        raise RuntimeError("the image data is not canonical base64") from error
    if base64.b64encode(decoded).decode("ascii") != data:
        raise RuntimeError("the image data is not canonical base64")
    return SaveImageAttachment(data=decoded, mediaType=media_type)


def resolve_image_admission(ctx: Context, exec_: ToolExec) -> Any:
    """解析活跃模型路由与 durable 附件 store（精确正向图像能力证明后）。"""
    attachments = ctx.get("attachments")
    if attachments is None:
        raise RuntimeError("no attachment store is mounted")
    agent = getattr(exec_, "agent", None)
    provider = model = None
    if agent is not None:
        try:
            header = agent.session.request_header() if getattr(agent, "session", None) is not None else None
            routed = (header or {}).get("config") if isinstance(header, dict) else None
        except Exception:  # noqa: BLE001 - 路由解析失败等同不可验证
            routed = None
        options = getattr(agent, "options", None) or {}
        provider = (routed or {}).get("provider") or options.get("provider")
        model = (routed or {}).get("model") or options.get("model")
    info = _resolve_model_info(ctx, agent)
    if provider is None or model is None or info is None:
        raise RuntimeError("the current model route could not be resolved")
    modalities = info.get("input_modalities") or ["text"]
    if isinstance(modalities, str):
        modalities = [modalities]
    if "image" not in modalities:
        raise RuntimeError(f'model "{model}" does not declare image input')
    signal = getattr(exec_, "signal", None)
    if signal is not None and getattr(signal, "is_set", None) is not None and signal.is_set():
        raise RuntimeError("the tool call was canceled before image storage")
    return attachments


def _resolve_model_info(ctx: Context, agent: Any) -> dict | None:
    """mini 模型能力载体：优先 agent.adapter（教学扩展），回退 'llm' 服务。"""
    adapter = getattr(agent, "adapter", None)
    if adapter is not None and hasattr(adapter, "resolve_model_info"):
        try:
            return adapter.resolve_model_info()
        except Exception:  # noqa: BLE001 - 解析失败按不可验证处理
            return None
    llm = ctx.get("llm")
    if llm is not None and hasattr(llm, "resolve_model_info"):
        try:
            return llm.resolve_model_info()
        except Exception:  # noqa: BLE001
            return None
    return None


def prepare_image_projection(ctx: Context, exec_: ToolExec, content: list, tool_name: str) -> list[dict]:
    """解码、预检并 durable 保存一个 MCP 结果的图片批次。

    任何拒绝（解码失败 / 模型路由无法正向证明 image 能力 / 保存失败）都把
    全部 image 投成文本诊断，canonical 原始值仍留给程序化调用方。
    """
    decoded: list[SaveImageAttachment] = []
    validation_errors: dict[int, str] = {}
    image_indexes: list[int] = []
    for index, value in enumerate(content):
        if not is_record(value) or value.get("type") != "image":
            continue
        image_indexes.append(index)
        try:
            decoded.append(decode_image(value))
        except Exception as error:
            validation_errors[index] = str(error)
    if validation_errors:
        return project_content(
            content,
            tool_name,
            image=lambda block, index: {
                "type": "text",
                "text": _image_diagnostic(
                    block, validation_errors.get(index) or "another image in the same result was invalid"),
            },
        )

    try:
        attachments = resolve_image_admission(ctx, exec_)
    except Exception as error:  # noqa: BLE001 - resolveImageAdmission 只抛 Error
        reason = str(error)
        return project_content(
            content, tool_name,
            image=lambda block, index: {"type": "text", "text": _image_diagnostic(block, reason)})

    try:
        refs = attachments.save_images(decoded)
        by_index = dict(zip(image_indexes, refs))
        return project_content(
            content, tool_name,
            image=lambda block, index: {"type": "image", "attachment": by_index[index]})
    except Exception as error:  # noqa: BLE001 - 结构化回退诊断
        reason = (f"image admission rejected the result: {error}" if is_image_admission_error(error)
                  else "durable image storage rejected the result")
        return project_content(
            content, tool_name,
            image=lambda block, index: {"type": "text", "text": _image_diagnostic(block, reason)})