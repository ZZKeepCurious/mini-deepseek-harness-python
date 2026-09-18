"""DeepSeek 官方 chat API 的 wire 序列化与 SSE 适配器。

对应 dsh 真实源码：packages/llm/llm-deepseek/src（adapter.ts + serialize.ts +
sse.ts + translate.ts）。

  * 请求体 stream:true + stream_options.include_usage
  * SSE 必须出现字面 [DONE]，EOF 未到 [DONE] 抛 STREAM_CLOSED（截断响应不可信）
  * finish reason 与 usage 在 [DONE] 之后发射；空响应抛 EMPTY_RESPONSE
  * 错误映射：401/403→AUTH、413→INVALID_REQUEST、quota 措辞→QUOTA、
    429→RATE_LIMIT、400 上下文超限→CONTEXT_WINDOW_EXCEEDED（否则 INVALID_REQUEST）、
    500+→SERVER、其余→HTTP_<status>；LlmError facts（status / providerRetryAfterMs / requestId）
"""
from __future__ import annotations

import asyncio
import email.utils
import json
import os
import re
from datetime import datetime, timezone
from typing import Any

import httpx

from ..core.session import reasoning_block
from .protocol import (
    AUTH,
    CONTEXT_WINDOW_EXCEEDED,
    EMPTY_RESPONSE,
    IMAGE_OFFLOAD_REQUIRED,
    INVALID_REQUEST,
    MALFORMED_RESPONSE,
    QUOTA,
    RATE_LIMIT,
    SERVER,
    STREAM_CLOSED,
    TIMEOUT,
    TRANSPORT,
    LlmAdapter,
    LlmFailure,
    StreamChunk,
    StreamAborted,
    _aiter_raced,
)
from .retry_policy import resolve_retry_policy
from .content import (
    content_has_image,
    content_has_file,
    offloaded_image_text,
    project_offloaded_images,
    project_images_for_text_model,
    required_image_offload,
    resolve_image_attachment_access,
    text_only_image_text,
    request_image_handle_text,
)
from ..attachment.types import AttachmentId, Dimensions, ImageAttachmentRef
from .deepseek_files import (
    DEFAULT_CONTEXT_WINDOW,
    DEFAULT_FILES_API_TIMEOUT_MS,
    DEFAULT_FILE_EXPIRY_SECONDS,
    DEFAULT_FILE_QUOTA_CLEANUP_BATCH,
    DEFAULT_FILE_REFRESH_MARGIN_SECONDS,
    DEFAULT_IMAGE_OFFLOAD_BYTE_QUANTUM,
    DEFAULT_IMAGE_OFFLOAD_COUNT_QUANTUM,
    DEFAULT_INLINE_IMAGE_OFFLOAD_BYTE_QUANTUM,
    DEFAULT_MAX_IMAGES_PER_REQUEST,
    DEFAULT_MAX_INLINE_REQUEST_IMAGE_BYTES,
    DEFAULT_MAX_REQUEST_FILES_BYTES,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODELS,
    DEFAULT_STREAM_IDLE_TIMEOUT_MS,
    DeepSeekConnectionOptions,
    DeepSeekFileConnection,
    DeepSeekFilePolicy,
    DeepSeekFileStore,
    FileResolutionFailure,
    ImageWireLocation,
    RequestDefaults,
    RequestFiles,
    deep_seek_image_request_pricing,
    model_info,
    resolve_request_image_target,
)

__all__ = [
    "DeepSeekAdapter",
    "UNSUPPORTED_CONTENT",
    "content_has_file",
    "content_has_image",
    "IMAGE_OFFLOAD_REQUIRED",
    "provider_retry_after_ms",
    "request_id",
    "serialize_messages",
    "serialize_messages_with_images",
    "project_offloaded_images",
    "project_images_for_text_model",
    "required_image_offload",
    "text_only_image_text",
    "offloaded_image_text",
    "request_image_handle_text",
]


# ---------- DeepSeek wire 序列化（llm-deepseek/src/serialize.ts） ----------

UNSUPPORTED_CONTENT = "UNSUPPORTED_CONTENT"


def serialize_messages(messages: list[dict]) -> list[dict]:
    """把 harness 消息序列化为 DeepSeek chat-completions wire 消息。

    与上游一致：system → {role:'system'}；assistant 的 text 合并为 content、
    reasoning 凡携带即作为 reasoning_content 回传（rc.2 放宽：旧为仅带
    tool_calls 时回传——网关转编码其他厂商时靠这段文本恢复该 turn 的
    thinking signature）、tool-call 块转为 tool_calls；user role 消息的
    文本走 {role:'user'}，每个 tool-result 块展开为独立的
    {role:'tool', tool_call_id} 消息（空输出用 '(no output)'）。

    image 块：offloaded=True 时投影为占位文本（projectOffloadedImages），
    offloaded=False 时抛 UNSUPPORTED_CONTENT（text-only 路由）。
    file 块在请求组装即被 project_files_to_text 无条件投影为 handle 文本。
    """
    wire: list[dict] = []

    def flatten_text(blocks: list) -> str:
        return "".join(b["text"] for b in blocks if b.get("type") == "text")

    for message in messages:
        blocks = message.get("content", [])
        if content_has_file(blocks):
            raise LlmFailure(
                UNSUPPORTED_CONTENT,
                "The DeepSeek chat-completions adapter does not support file content.",
            )
        if content_has_image(blocks):
            if any(b.get("type") == "image" and b.get("offloaded") is not True for b in blocks):
                raise LlmFailure(
                    UNSUPPORTED_CONTENT,
                    "The DeepSeek chat-completions adapter does not support image content.",
                )
        if message.get("role") == "system":
            wire.append({"role": "system", "content": flatten_text(blocks)})
            continue
        if message.get("role") == "assistant":
            text = flatten_text(blocks)
            reasoning = "".join(b["text"] for b in blocks if b.get("type") == "reasoning")
            tool_calls = [
                {"id": b["id"], "type": "function",
                 "function": {"name": b["name"], "arguments": b["arguments"]}}
                for b in blocks if b.get("type") == "tool-call"
            ]
            wire.append({
                "role": "assistant",
                "content": text,
                **({"reasoning_content": reasoning} if reasoning else {}),
                **({"tool_calls": tool_calls} if tool_calls else {}),
            })
            continue
        # user role：文本 + 每个 tool-result 块展开为独立 tool 消息
        tool_results = [b for b in blocks if b.get("type") == "tool-result"]
        text = flatten_text(blocks)
        if text or not tool_results:
            wire.append({"role": "user", "content": text})
        for result in tool_results:
            wire.append({
                "role": "tool",
                "tool_call_id": result["toolCallId"],
                "content": flatten_text(result.get("content", [])) or "(no output)",
            })
    return wire


# ---------- 图像 offload 管线 ----------
# 导入自 content.py：text_only_image_text, request_image_handle_text,
# offloaded_image_text, project_offloaded_images, project_images_for_text_model,
# required_image_offload, content_has_image, content_has_file


async def serialize_messages_with_images(
    messages: list[dict],
    images: dict,
) -> list[dict]:
    """Serialize image-capable history after resolving durable attachments.

    上游 serializeMessagesWithImages（serialize.ts:278-333）。
    Consecutive tool results keep string tool messages and share one
    following user message containing their images.
    @param messages - request history whose offloaded occurrences are already placeholder text.
    @param images - ImageSerializationOptions equivalent dict.
    @returns ordered DeepSeek wire messages.
    """
    assert_supported_image_roles(messages)
    assert_retained_images_fit(messages, images)
    request_messages = project_offloaded_images(
        messages,
        lambda ref: offloaded_image_text(ref, _access_for(images, ref)),
    )
    return await _serialize_messages_with_images_impl(request_messages, images)


def _access_for(images: dict, ref: dict):
    """按当前执行世界解析一个 durable 图片引用的只读访问（可选）。"""
    resolve_access = images.get("resolveImageAccess")
    return resolve_access(ref) if resolve_access is not None else None


async def _serialize_messages_with_images_impl(
    messages: list[dict],
    images: dict,
) -> list[dict]:
    """Internal implementation of serializeMessagesWithImages."""
    wire: list[dict] = []
    pending_tool_images: list[dict] = []

    def flatten_text(blocks: list) -> str:
        return "".join(b.get("text", "") for b in blocks if b.get("type") == "text")

    def flush_tool_images() -> None:
        nonlocal pending_tool_images
        if not pending_tool_images:
            return
        wire.append({
            "role": "user",
            "content": [{"type": "text", "text": "Attached image(s) from tool result:"}] + pending_tool_images,
        })
        pending_tool_images = []

    for message_index, message in enumerate(messages):
        next_image = {"value": 0}
        if message.get("role") == "system":
            flush_tool_images()
            wire.append({"role": "system", "content": flatten_text(message.get("content") or [])})
            continue
        if message.get("role") == "assistant":
            flush_tool_images()
            text = flatten_text(message.get("content") or [])
            reasoning = "".join(b.get("text", "") for b in message.get("content") or [] if b.get("type") == "reasoning")
            tool_calls = [
                {"id": b["id"], "type": "function",
                 "function": {"name": b["name"], "arguments": b["arguments"]}}
                for b in message.get("content") or [] if b.get("type") == "tool-call"
            ]
            wire.append({
                "role": "assistant",
                "content": text,
                **({"reasoning_content": reasoning} if reasoning else {}),
                **({"tool_calls": tool_calls} if tool_calls else {}),
            })
            continue

        regular = [b for b in message.get("content") or [] if b.get("type") != "tool-result"]
        tool_results = [b for b in message.get("content") or [] if b.get("type") == "tool-result"]
        content_parts = await _content_parts(regular, images, message_index + 1, next_image)
        content = user_content(content_parts)
        if content or not tool_results:
            flush_tool_images()
            wire.append({"role": "user", "content": content})
        for result in tool_results:
            parts = await _content_parts(result.get("content") or [], images, message_index + 1, next_image)
            image_parts = [p for p in parts if p.get("type") != "text"]
            text = "".join(p.get("text", "") for p in parts if p.get("type") == "text")
            wire.append({
                "role": "tool",
                "tool_call_id": result.get("toolCallId"),
                "content": text or "(no output)",
            })
            pending_tool_images.extend(image_parts)
    flush_tool_images()
    return wire


async def _content_parts(
    blocks: list,
    images: dict,
    message_index: int,
    next_image: dict,
) -> list:
    """Convert user or nested tool-result blocks into ordered wire parts."""
    parts: list = []
    for block in blocks or []:
        btype = block.get("type")
        if btype == "text":
            if block.get("text", ""):
                parts.append({"type": "text", "text": block["text"]})
        elif btype == "image":
            next_image["value"] += 1
            parts.extend(await _image_parts(block, images, {"message": message_index, "image": next_image["value"]}, len(parts) > 0))
        elif btype == "tool-result":
            parts.extend(await _content_parts(block.get("content") or [], images, message_index, next_image))
    return parts


async def _image_parts(
    block: dict,
    images: dict,
    location: dict,
    preceded_by_content: bool,
) -> list:
    """Resolve one durable image into its descriptor and transient DeepSeek image part."""
    aid = str(block["attachment"]["attachmentId"])
    version = images["requestImages"].get(aid)
    if version is None:
        raise LlmFailure(
            "INVALID_REQUEST",
            f"DeepSeek request image {aid} was not prepared.",
        )
    if images["representation"]["kind"] == "file":
        file_id = await images["representation"]["resolveFileId"](version, block, location)
        image_part = {"type": "file", "file_id": file_id}
    else:
        import base64
        image_part = {
            "type": "image_url",
            "image_url": {"url": f"data:{version['mediaType']};base64,{base64.b64encode(version['data']).decode('ascii')}"},
        }
    text_part = {
        "type": "text",
        "text": ("\n" if preceded_by_content else "") + request_image_handle_text(
            block["attachment"], version, _access_for(images, block["attachment"]),
        ),
    }
    return [text_part, image_part]


def user_content(parts: list) -> str | list:
    """Keep text-only user messages on the compact string wire form."""
    text: list[str] = []
    for part in parts:
        if part.get("type") != "text":
            return parts
        text.append(part.get("text", ""))
    return "".join(text)


def assert_supported_image_roles(messages: list[dict]) -> None:
    """Reject roles whose DeepSeek history format cannot carry image input."""
    for message in messages:
        if message.get("role") != "user" and content_has_image(message.get("content") or []):
            raise LlmFailure(
                UNSUPPORTED_CONTENT,
                f"The DeepSeek chat-completions adapter cannot represent image content in a {message['role']} message.",
            )


def assert_retained_images_fit(messages: list[dict], images: dict) -> None:
    """Reject a request whose retained occurrences exceed the route budget."""
    representation = images["representation"]["kind"]
    from .protocol import LlmImageRequestBudget
    offload_images = required_image_offload(
        messages,
        LlmImageRequestBudget(
            representation=representation,
            maxBytes=images.get("maxRequestImageBytes"),
            maxImages=images.get("maxImagesPerRequest"),
            byteQuantum=images.get("byteQuantum"),
            countQuantum=images.get("countQuantum"),
        ),
        lambda block: images["requestImages"].get(str(block["attachment"]["attachmentId"]))["bytes"] if images["requestImages"].get(str(block["attachment"]["attachmentId"])) else 0,
    )
    if offload_images > 0:
        raise LlmFailure(
            IMAGE_OFFLOAD_REQUIRED,
            f"DeepSeek {representation} request images exceed the route budget; {offload_images} more oldest occurrence(s) must be offloaded.",
        )


# ---------- End of image offload pipeline ----------

# 上游 error.ts 的正则集合（isContextWindowExceededError / isQuotaExceededError）
# 匹配 error.code+type+message 拼接串；mini 以 body 为待测串（stdlib 载体简化）。
_STRUCTURED_CONTEXT_OVERFLOW = re.compile(
    r"(?:^|[^a-z0-9])context[\s_-](?:length|window)[\s_-]"
    r"(?:exceed(?:ed|s)?|overflow(?:ed)?|limit[\s_-]exceeded)(?:$|[^a-z0-9])",
    re.I,
)
_CONTEXT_LENGTH_WINDOW = re.compile(
    r"\b(?:maximum|max)(?:\s+(?:allowed|supported))?\s+context\s+(?:length|window)\b", re.I
)
_TOO_LARGE_FOR_CONTEXT = re.compile(
    r"\b(?:request|prompt|input|messages?)\s+(?:is\s+|are\s+)?"
    r"too\s+(?:large|long)\s+for\s+(?:(?:this|the)\s+)?"
    r"(?:model(?:'s)?\s+)?context(?:\s+window)?\b",
    re.I,
)
_TOO_LONG_FOR_MODEL = re.compile(
    r"\b(?:input|prompt|request)\s+(?:is\s+)?too\s+(?:long|large)\s+for\s+(?:this|the)\s+model\b",
    re.I,
)
_EXCEEDS_MODEL_CONTEXT = re.compile(
    r"\b(?:input|prompt|request|messages?)\b.{0,40}"
    r"\b(?:exceed(?:s|ed)?|overflows?|is\s+larger\s+than)\b.{0,40}"
    r"\b(?:the\s+)?(?:model(?:'s)?\s+)?context(?:\s+(?:length|window))?\b",
    re.I,
)
_QUOTA_INSUFFICIENT = re.compile(r"\binsufficient[\s_-]+(?:quota|balance|credits?)\b", re.I)
_QUOTA_EXCEEDED = re.compile(r"\b(?:quota|usage[\s_-]+limit)[\s_-]+(?:exceeded|exhausted|reached)\b", re.I)


def _http_error_code(status: int, body: str) -> str:
    """上游 httpErrorCode 映射（llm-deepseek/src/adapter.ts:333-345 @ rc.2）：
    401/403→AUTH；413（payload too large）→INVALID_REQUEST；quota 措辞
    （任意状态，先于 429）→QUOTA；429→RATE_LIMIT；
    400 上下文超限→CONTEXT_WINDOW_EXCEEDED、否则→INVALID_REQUEST；
    ≥500→SERVER；其余→HTTP_<status>。

    上下文/quota 判定复刻上游 error.ts 正则集（isContextWindowExceededError /
    isQuotaExceededError），避免裸子串误判。
    """
    if status in (401, 403):
        return AUTH
    if status == 413:
        return INVALID_REQUEST
    text = body.lower()
    if _QUOTA_INSUFFICIENT.search(text) or _QUOTA_EXCEEDED.search(text):
        return QUOTA
    if status == 429:
        return RATE_LIMIT
    if status >= 500:
        return SERVER
    if status == 400:
        if (
            _STRUCTURED_CONTEXT_OVERFLOW.search(text)
            or _CONTEXT_LENGTH_WINDOW.search(text)
            or _TOO_LARGE_FOR_CONTEXT.search(text)
            or _TOO_LONG_FOR_MODEL.search(text)
            or _EXCEEDS_MODEL_CONTEXT.search(text)
        ):
            return CONTEXT_WINDOW_EXCEEDED
        return INVALID_REQUEST
    return f"HTTP_{status}"


def provider_retry_after_ms(value: str | None) -> int | None:
    """上游 providerRetryAfterMs（llm-deepseek/src/adapter.ts:117-125 同构）。

    纯数字秒 → ×1000；否则尝试 ISO 8601（Date.parse 兼容集）或
    HTTP-date（RFC 7231）解析为相对毫秒；无效/非正 → None（视为未提供）。
    """
    if value is None or len(value.strip()) == 0:
        return None
    text = value.strip()
    if text.isdigit():
        delay = int(text) * 1000
        return delay if delay > 0 else None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    delay = int((parsed.astimezone(timezone.utc) - datetime.now(timezone.utc)).total_seconds() * 1000)
    return delay if delay > 0 else None


def request_id(headers) -> str | None:
    """上游 requestId（llm-deepseek/src/adapter.ts 同构）。"""
    value = headers.get("x-request-id") or headers.get("x-deepseek-request-id")
    if value is None or len(value) == 0:
        return None
    return str(value)


def _accept_identity(current: str | None, incoming) -> str | None:
    """上游 acceptIdentity（translate.ts:74-87）：tool-call 的 id/name 是 identity
    而非累加——wire 只在首 delta 发送一次；continuation 重发 ''/null（部分
    OpenAI 兼容网关会填充 null）表示「无更新」，绝不覆盖已建立值。"""
    return incoming if isinstance(incoming, str) and len(incoming) > 0 else current


def _map_finish_reason(reason: str | None) -> dict:
    """上游 mapFinishReason（translate.ts:31-43）：stop→stop、
    tool_calls→tool-calls、length→max-tokens；缺省 → {kind:'stop'}
    （translate.ts:107）；其它 → {kind:'error', failure:
    {message: "model stopped: <reason>", code: <reason 大写>}}。"""
    if reason is None or reason == "stop":
        return {"kind": "stop"}
    if reason == "tool_calls":
        return {"kind": "tool-calls"}
    if reason == "length":
        return {"kind": "max-tokens"}
    return {"kind": "error", "failure": {
        "message": f"model stopped: {reason}", "code": reason.upper(),
    }}


def _map_usage(usage: dict) -> dict:
    """上游 mapUsage（llm-deepseek/src/translate.ts 同构）：TokenUsage 子集。

    inputTokens = prompt_tokens - cacheReadTokens；cacheReadTokens 来自
    prompt_tokens_details.cached_tokens（OpenAI 拼写）兜底 prompt_cache_hit_tokens；
    reasoningTokens 来自 completion_tokens_details.reasoning_tokens。
    totalTokens = prompt_tokens + completion_tokens（权威聚合总数），仅在
    prompt/completion 计数均有效且与 wire total_tokens 一致时提供（否则省略
    —— TokenUsage.totalTokens 可缺省，见 llm/src/types.ts:138-145）。
    """
    details = usage.get("prompt_tokens_details") or {}
    cache_read = details.get("cached_tokens")
    if cache_read is None:
        cache_read = usage.get("prompt_cache_hit_tokens")
    prompt_tokens = usage.get("prompt_tokens") or 0
    completion_tokens = usage.get("completion_tokens") or 0
    input_tokens = int(prompt_tokens) - int(cache_read or 0)
    mapped = {"inputTokens": input_tokens,
              "outputTokens": int(completion_tokens)}
    combined = int(prompt_tokens) + int(completion_tokens)
    wire_total = usage.get("total_tokens")
    has_exact_total = (
        isinstance(prompt_tokens, int) and not isinstance(prompt_tokens, bool)
        and prompt_tokens >= 0
        and isinstance(completion_tokens, int) and not isinstance(completion_tokens, bool)
        and completion_tokens >= 0
        and combined >= 0
        and (wire_total is None or wire_total == combined)
    )
    if has_exact_total:
        mapped["totalTokens"] = combined
    if cache_read:
        mapped["cacheReadTokens"] = int(cache_read)
    comp_details = usage.get("completion_tokens_details") or {}
    if comp_details.get("reasoning_tokens"):
        mapped["reasoningTokens"] = int(comp_details["reasoning_tokens"])
    return mapped


# ---------- image-capable 请求辅助（serialize.ts / request-files.ts 载体桥） ----------

def _ref_from_dict(value):
    """消息块里的 ImageAttachmentRef dict → dataclass（attachment 服务期望 dataclass）。"""
    if isinstance(value, ImageAttachmentRef):
        return value
    original = value.get("originalDimensions")
    return ImageAttachmentRef(
        attachmentId=AttachmentId(str(value["attachmentId"])),
        mediaType=value["mediaType"],
        bytes=value["bytes"],
        width=value["width"],
        height=value["height"],
        name=value.get("name"),
        originalDimensions=(Dimensions(**original) if isinstance(original, dict)
                            else original),
    )


def _version_dict(version) -> dict:
    """RequestImageAttachment（dataclass）→ llm/content 序列化期望的 dict 载体。"""
    attachment = version.attachment
    return {
        "variantId": str(version.variantId),
        "attachment": attachment.to_dict() if hasattr(attachment, "to_dict") else attachment,
        "data": version.data,
        "mediaType": version.mediaType,
        "bytes": version.bytes,
        "width": version.width,
        "height": version.height,
        "depth": version.depth,
        "space": version.space,
        "hasAlpha": version.hasAlpha,
    }


def _make_resolve_file_id(request_files, objects: dict):
    """把一个请求版本的 Files 解析包装成序列化器期望的 async resolveFileId 回调。"""
    async def resolve_file_id(version, block, location):
        attachment_id = str(block["attachment"]["attachmentId"])
        resolved = await request_files.resolve(
            objects[attachment_id],
            ImageWireLocation(location["message"], location["image"]))
        return resolved
    return resolve_file_id


def _provider_error_fields(raw: str) -> tuple[str | None, str]:
    """从 provider 错误体提取 (message, code+type+message)（上游分类字段）。"""
    try:
        parsed = json.loads(raw)
    except ValueError:
        return None, ""
    if not isinstance(parsed, dict):
        return None, ""
    error = parsed.get("error")
    if not isinstance(error, dict):
        return None, ""
    message = error.get("message") if isinstance(error.get("message"), str) else None
    detail = " ".join(
        field for field in (error.get("code"), error.get("type"), error.get("message"))
        if isinstance(field, str))
    return message, detail


class DeepSeekAdapter(LlmAdapter):
    """DeepSeek 官方 chat API 的 SSE 适配器（httpx 异步传输）。

    与上游 llm-deepseek 一致：
      * 请求体 stream:true + stream_options.include_usage
      * SSE 必须出现字面 [DONE]，EOF 未到 [DONE] 抛 STREAM_CLOSED（截断响应不可信）
      * finish reason 与 usage 在 [DONE] 之后发射；空响应抛 EMPTY_RESPONSE
      * per-read idle 超时 300s（对齐上游 fetch watchdog）+ 真取消：abort
        置位即关闭连接（httpx 原生 asyncio 传输，无遗留线程）

     推理 effort（2026-08-22 对齐上游 rc.7）：请求侧承载 off/low/high/max
     四档（REASONING_EFFORTS，rc.7 新增 low）；'off'/未设置省略，其余原样
     透传为 wire 参数 reasoning_effort（对齐 upstream serialize.ts）。值经
     适配器构造参数 reasoning_effort 注入；request/header 信封的
     config.reasoningEffort 携带字符串值、adapterDefaults.reasoningEffort 为
     布尔标记（由 AgentLoop._adapter_defaults 输出）。响应侧 reasoning_content
     与 usage.reasoningTokens 已支持。见 AGENTS.md 已对齐项。
     """

    provider = "deepseek-official"

    # 上游 llm-deepseek REASONING_EFFORTS（rc.7 新增 low）：'off' 在 wire 上省略
    REASONING_EFFORTS = ("off", "low", "high", "max")

    # 上游 per-read idle watchdog（fetch 流读间隙超时）；连接超时取常用值
    CONNECT_TIMEOUT_S = 30.0
    READ_TIMEOUT_S = 300.0

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str = "deepseek-chat", max_tokens: int | None = None,
                 retry_policy: dict | None = None, transport=None,
                 reasoning_effort: str | None = None,
                 models=None, attachments=None, map_host_path=None,
                 files_store=None, files_transport=None,
                 file_policy=None, default_context_window=None,
                 thinking=None):
        self._key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
        self._base = (base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self._model = model
        self._max_tokens = max_tokens
        self._transport = transport  # httpx transport（MockTransport 测试注入口）
        if reasoning_effort is not None and reasoning_effort not in self.REASONING_EFFORTS:
            raise ValueError(
                f"invalid reasoning mode {reasoning_effort!r}; "
                f"expected one of {self.REASONING_EFFORTS}")
        self._reasoning_effort = reasoning_effort
        # 上游 llm-deepseek：retryPolicy 省略即 normal 默认（resolveRetryPolicy(undefined)）
        self.retry_policy = resolve_retry_policy(retry_policy, "llm-deepseek: retryPolicy")
        # 请求侧图片能力（上游 DeepSeekAdapterOptions 的 mini 载体）：
        #   * models——建议性目录（缺省 DEFAULT_MODELS，inputModalities 含 image
        #     才宣称支持图片输入）；
        #   * attachments——按操作解析的 attachment 服务（callable，缺省 None）；
        #   * map_host_path——宿主路径→工具执行世界的映射（callable，可选）；
        #   * files_store / files_transport——Files API 执行簇与测试注入。
        self._models = tuple(models) if models is not None else DEFAULT_MODELS
        self._resolve_attachments = attachments
        self._map_host_path = map_host_path
        self._files_store = files_store
        self._files_transport = files_transport
        self._connection = DeepSeekConnectionOptions(
            protocol="chat-completions",
            baseURL=self._base,
            defaults=RequestDefaults(thinking=thinking, reasoningEffort=reasoning_effort),
            maxTokens=max_tokens if max_tokens is not None else DEFAULT_MAX_TOKENS,
            defaultContextWindow=(default_context_window if default_context_window is not None
                                  else DEFAULT_CONTEXT_WINDOW),
            models=self._models,
            streamIdleTimeoutMs=int(self.READ_TIMEOUT_S * 1000),
            maxRequestFilesBytes=DEFAULT_MAX_REQUEST_FILES_BYTES,
            maxInlineRequestImageBytes=DEFAULT_MAX_INLINE_REQUEST_IMAGE_BYTES,
            maxImagesPerRequest=DEFAULT_MAX_IMAGES_PER_REQUEST,
            imageOffloadByteQuantum=DEFAULT_IMAGE_OFFLOAD_BYTE_QUANTUM,
            inlineImageOffloadByteQuantum=DEFAULT_INLINE_IMAGE_OFFLOAD_BYTE_QUANTUM,
            imageOffloadCountQuantum=DEFAULT_IMAGE_OFFLOAD_COUNT_QUANTUM,
            filesApiTimeoutMs=DEFAULT_FILES_API_TIMEOUT_MS,
            filePolicy=file_policy if file_policy is not None else DeepSeekFilePolicy(
                DEFAULT_FILE_EXPIRY_SECONDS, DEFAULT_FILE_REFRESH_MARGIN_SECONDS,
                DEFAULT_FILE_QUOTA_CLEANUP_BATCH),
        )

    def _files(self) -> DeepSeekFileStore:
        """进程级上传复用 store（上游 resolveFiles）。"""
        if self._files_store is None:
            self._files_store = DeepSeekFileStore(transport=self._files_transport)
        return self._files_store

    def image_request_pricing(self, model: str) -> "object":
        """按连接快照构建 provider 侧请求图定价（上游 imageRequestPricing）。"""
        model_entry = next((entry for entry in self._models if entry.id == model), None)
        if model_entry is None or "image" not in (model_entry.inputModalities or ()):
            return deep_seek_image_request_pricing(self._connection, model)
        attachments = self._resolve_attachments() if self._resolve_attachments is not None else None
        if attachments is None or self._map_host_path is None:
            return deep_seek_image_request_pricing(self._connection, model)

        def resolve_access(ref):
            return resolve_image_attachment_access(
                attachments, self._map_host_path, _ref_from_dict(ref))

        return deep_seek_image_request_pricing(self._connection, model, resolve_access)

    @property
    def model(self) -> str | None:
        return self._model

    @property
    def reasoning_effort(self) -> str | None:
        """推理 effort 档位（'off'|'low'|'high'|'max'，None 表示未设置）。

        对齐上游 llm-deepseek 的 connection.defaults.reasoningEffort；'off'
        在 wire 上省略（见 serialize.ts：reasoning_effort 仅在非 off 时入请求体）。
        """
        return self._reasoning_effort

    def resolve_model_info(self) -> dict:
        """按模型目录解析能力（上游 adapter.ts resolveModelInfo → modelInfo）。

        未编目 endpoint 安全地按 text-only 处理；目录中 inputModalities 含
        'image' 的条目才宣称图片输入支持（ACP/web 受理门据此判定）。
        """
        return model_info(self._connection, self.provider, self._model)

    async def stream(self, messages, tools, signal=None):
        """async 迭代器（对齐上游 async stream）：httpx 异步传输 + SSE 解析，
        逐 chunk 产出。signal.aborted/.event 置位即中止——_aiter_raced 在下一次
        取块前抛 StreamAborted，退出 async-with 关闭连接（真取消，无遗留线程）。

        含图片且模型目录宣称 image 输入时走 image-capable 序列化（Files API
        file-id 优先、解析失败整请求回退 inline base64），否则走原文本路径。
        """
        abort_event = getattr(signal, "event", None) if signal is not None else None
        if not any(content_has_image(message.get("content") or []) for message in messages):
            body = self._build_body(messages, tools)
            async for chunk in self._iter_chunks(body, abort_event):
                yield chunk
            return
        async for chunk in self._stream_images_impl(messages, tools, abort_event, signal):
            yield chunk

    async def _stream_images_impl(self, messages, tools, abort_event, signal):
        model = next((entry for entry in self._models if entry.id == self._model), None)
        if model is None or "image" not in (model.inputModalities or ()):
            raise LlmFailure(
                UNSUPPORTED_CONTENT,
                f'DeepSeek model "{self._model}" does not accept image input.')
        attachments = self._resolve_attachments() if self._resolve_attachments is not None else None
        if attachments is None:
            raise LlmFailure(
                UNSUPPORTED_CONTENT,
                "DeepSeek image conversion requires the durable attachment service.")
        request_images, request_objects = self._prepare_request_images(
            messages, attachments, model)
        resolve_access = None
        if self._map_host_path is not None:
            def resolve_access(ref):
                return resolve_image_attachment_access(
                    attachments, self._map_host_path, _ref_from_dict(ref))
        file_connection = DeepSeekFileConnection(
            baseURL=self._base, apiKey=self._key, protocol="chat-completions")
        request_files = RequestFiles(
            self._files(), file_connection, self._connection.filePolicy,
            self._connection.filesApiTimeoutMs, abort_event, lambda: None)
        representation: dict = {"kind": "file"}
        while True:
            request_files.begin_attempt()
            images = {
                "representation": (
                    representation if representation["kind"] == "base64"
                    else {"kind": "file",
                          "resolveFileId": _make_resolve_file_id(request_files, request_objects)}
                ),
                "requestImages": request_images,
                "maxRequestImageBytes": (
                    self._connection.maxInlineRequestImageBytes
                    if representation["kind"] == "base64"
                    else self._connection.maxRequestFilesBytes),
                "maxImagesPerRequest": self._connection.maxImagesPerRequest,
                "byteQuantum": (
                    self._connection.inlineImageOffloadByteQuantum
                    if representation["kind"] == "base64"
                    else self._connection.imageOffloadByteQuantum),
                "countQuantum": self._connection.imageOffloadCountQuantum,
            }
            if resolve_access is not None:
                images["resolveImageAccess"] = resolve_access
            try:
                wire = await serialize_messages_with_images(messages, images)
            except FileResolutionFailure:
                representation = {"kind": "base64"}
                continue
            body = self._request_body(wire, tools)
            retry_state = {"retry": False}
            async for chunk in self._iter_chunks(body, abort_event, request_files, retry_state):
                yield chunk
            if retry_state["retry"]:
                continue
            return

    def _prepare_request_images(self, messages, attachments, model) -> tuple[dict, dict]:
        """按路由目标为保守保留的规范化附件准备请求版本（上游 prepareRequestImages）。

        @returns (版本 dict 表[供序列化 handle/占位文本], 版本对象表[供 Files 解析])
        """
        refs: dict = {}

        def collect(blocks) -> None:
            for block in blocks or []:
                if block.get("type") == "image" and block.get("offloaded") is not True:
                    ref = block["attachment"]
                    refs[str(ref["attachmentId"])] = ref
                elif block.get("type") == "tool-result":
                    collect(block.get("content") or [])

        for message in messages:
            collect(message.get("content") or [])
        version_map: dict = {}
        object_map: dict = {}
        for attachment_id, ref in refs.items():
            version = attachments.read_image_request(
                _ref_from_dict(ref), resolve_request_image_target(model, ref))
            version_map[attachment_id] = _version_dict(version)
            object_map[attachment_id] = version
        return version_map, object_map

    def _build_body(self, messages, tools) -> dict:
        return self._request_body(serialize_messages(messages), tools)

    def _request_body(self, wire_messages, tools) -> dict:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": wire_messages,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if tools:
            body["tools"] = [
                {"type": "function", "function": {
                    "name": t["name"], "description": t.get("description", ""),
                    "parameters": t.get("parameters", {}),
                }}
                for t in tools
            ]
        if self._max_tokens is not None:
            body["max_tokens"] = self._max_tokens
        # 推理 effort：对齐上游 serialize.ts —— 'off'/未设置省略，其余原样透传
        if self._reasoning_effort and self._reasoning_effort != "off":
            body["reasoning_effort"] = self._reasoning_effort
        return body

    async def _iter_chunks(self, body: dict, abort_event=None,
                           request_files=None, retry_state=None):
        """httpx 异步传输：发起 POST + 错误映射，逐行喂给 SSE 解析器。

        错误映射对齐上游 adapter.ts:333-345（rc.2）：401/403→AUTH、
        413→INVALID_REQUEST、quota 措辞→QUOTA、429→RATE_LIMIT、
        400 上下文超限→CONTEXT_WINDOW_EXCEEDED（否则 INVALID_REQUEST）、
        500+→SERVER、其余→HTTP_<status>；LlmError facts
        （status / providerRetryAfterMs / requestId）逐项填写。超时→TIMEOUT、
        其它传输错误→TRANSPORT。abort 置位经 _aiter_raced 抛 StreamAborted，
        async-with 退出即关闭连接。

        request_files 非 None 时（image 路径）：stale file-id 响应先走有界
        invalidate 重试，retry_state["retry"]=True 由调用方重新序列化派发
        （上游 requestFiles.retry(detail)）；否则按规范化图片诊断收口错误文案。
        """
        headers = {"Content-Type": "application/json", "Authorization": "Bearer " + self._key}
        timeout = httpx.Timeout(self.CONNECT_TIMEOUT_S, read=self.READ_TIMEOUT_S)
        kwargs: dict[str, Any] = {"timeout": timeout}
        if self._transport is not None:
            kwargs["transport"] = self._transport
        try:
            async with httpx.AsyncClient(**kwargs) as client:
                async with client.stream(
                    "POST", self._base + "/chat/completions", json=body, headers=headers,
                ) as resp:
                    if resp.status_code >= 400:
                        raw = (await resp.aread()).decode("utf-8", "replace")
                        detail = raw[:500]
                        if (request_files is not None and retry_state is not None
                                and await request_files.retry(detail)):
                            retry_state["retry"] = True
                            return
                        if request_files is not None:
                            provider_message, provider_detail = _provider_error_fields(raw)
                            message = request_files.error_message(
                                resp.status_code,
                                provider_message or f"HTTP {resp.status_code}: {detail}",
                                provider_detail or detail)
                        else:
                            message = f"HTTP {resp.status_code}: {detail}"
                        raise LlmFailure(
                            _http_error_code(resp.status_code, detail),
                            message,
                            status=resp.status_code,
                            provider_retry_after_ms=provider_retry_after_ms(
                                resp.headers.get("Retry-After")),
                            request_id=request_id(resp.headers),
                        ) from None
                    async for chunk in self._parse_sse(resp.aiter_lines(), abort_event):
                        yield chunk
        except httpx.TimeoutException as e:
            raise LlmFailure(TIMEOUT, "请求超时") from e
        except httpx.HTTPError as e:
            raise LlmFailure(TRANSPORT, f"网络错误: {e}") from e

    async def _parse_sse(self, aiter_lines, abort_event=None):
        """SSE spec-strict 解析（上游 sse.ts:7-9 + eventsource-parser）。

        事件只在空行终结时派发，EOF 处的未终止尾部是截断而非可 flush 的
        载荷（丢弃）；多个 data: 行以 \n 连接（multi-data join）；[DONE] 之前
        EOF → STREAM_CLOSED（截断响应不可信）；畸形载荷 → MALFORMED_RESPONSE。
        abort 置位覆盖截断判定（取消路径不落 STREAM_CLOSED）。
        """
        texts: dict[int, str] = {}
        reasonings: dict[int, str] = {}
        pending: dict[int, dict[str, str]] = {}
        usage: dict | None = None
        finish_reason: str | None = None
        saw_done = False
        data_lines: list[str] = []
        async for line in _aiter_raced(aiter_lines, abort_event):
            if line == "":
                # 空行终结：派发当前事件
                if data_lines:
                    data = "\n".join(data_lines)
                    data_lines = []
                    if data == "[DONE]":
                        saw_done = True
                        break
                    try:
                        piece = json.loads(data)
                    except json.JSONDecodeError:
                        # 对齐上游：SSE 载荷非 JSON → MALFORMED_RESPONSE（截断/损坏不可信）
                        raise LlmFailure(MALFORMED_RESPONSE, f"malformed SSE payload: {data[:120]}") from None
                    if piece.get("usage"):
                        usage = _map_usage(piece["usage"])
                    for choice in piece.get("choices", []):
                        delta = choice.get("delta", {})
                        finish_reason = choice.get("finish_reason") or finish_reason
                        if delta.get("reasoning_content"):
                            reasonings[choice["index"]] = reasonings.get(choice["index"], "") + delta["reasoning_content"]
                        if delta.get("content"):
                            texts[choice["index"]] = texts.get(choice["index"], "") + delta["content"]
                        for tc in delta.get("tool_calls") or []:
                            slot = pending.setdefault(tc["index"], {"id": None, "name": None, "arguments": ""})
                            fn = tc.get("function") or {}
                            # identity 非累加（translate.ts:74-87 acceptIdentity）：id/name 由
                            # 首 delta 建立，continuation 重发 ''/null 表示「无更新」而非「清空」；
                            # arguments 片段为 identity 补集，null/缺省按 ''（translate.ts:186 ?? ''）。
                            slot["id"] = _accept_identity(slot["id"], tc.get("id"))
                            slot["name"] = _accept_identity(slot["name"], fn.get("name"))
                            fragment = fn.get("arguments")
                            slot["arguments"] += fragment if isinstance(fragment, str) else ""
                continue
            if line.startswith("data:"):
                payload = line[5:]
                if payload.startswith(" "):
                    payload = payload[1:]
                data_lines.append(payload)
            # 非 data 字段（注释/event:/id:/retry:）跳过
        # EOF：未终止的 data_lines 缓冲直接丢弃（截断）；取消覆盖截断判定
        if abort_event is not None and abort_event.is_set():
            raise StreamAborted("LLM 流被取消")
        if not saw_done:
            raise LlmFailure(STREAM_CLOSED, "SSE 流在 [DONE] 之前结束，响应不完整")

        emitted = False
        for idx in sorted(texts):
            emitted = True
            yield StreamChunk("block-start", index=idx, blockType="text")
            yield StreamChunk("text-delta", index=idx, text=texts[idx])
            yield StreamChunk("block-end", index=idx, block={"type": "text", "text": texts[idx]})
        for idx in sorted(reasonings):
            emitted = True
            yield StreamChunk("block-start", index=idx, blockType="reasoning")
            yield StreamChunk("reasoning-delta", index=idx, text=reasonings[idx])
            yield StreamChunk("block-end", index=idx, block=reasoning_block(reasonings[idx]))
        for idx, slot in sorted(pending.items()):
            emitted = True
            # 缺 identity 的 close 兜底：空串 stand-in（上游 closeBlock 的
            # `block.callId ?? ''` / `block.name ?? ''`；合成占位 id 属 mini
            # 旧发明，随 §2.21 acceptIdentity 批对齐移除）
            call_id = slot["id"] or ""
            name = slot["name"] or ""
            yield StreamChunk("block-start", index=idx, blockType="tool-call")
            yield StreamChunk("tool-call-delta", index=idx, id=call_id,
                              name=name, argumentsDelta=slot["arguments"])
            yield StreamChunk("block-end", index=idx, block={
                "type": "tool-call", "id": call_id, "name": name,
                "arguments": slot["arguments"],
            })
        if not emitted:
            raise LlmFailure(EMPTY_RESPONSE, "模型返回了空响应（无文本、无推理、无工具调用）")
        if usage is not None:
            yield StreamChunk("usage", usage=usage)
        yield StreamChunk("finish", reason=_map_finish_reason(finish_reason))