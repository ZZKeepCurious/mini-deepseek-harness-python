"""DeepSeek Messages（Anthropic 兼容）wire 序列化与 SSE 翻译。

对应 dsh 真实源码：packages/llm/llm-deepseek/src/{serialize,translate,sse,
messages-api,transport}.ts。

上游 llm-deepseek 自 dsh-v0.1.7-rc.1 起删除 Chat Completions 协议、只保留
Messages：请求 POST 到 ``messagesApiRoot(baseURL) + "/messages"``，头为
``x-api-key`` + ``anthropic-version: 2023-06-01``（file id 请求追加
``anthropic-beta``）。mini 逐语义移植：

  * harness 消息（system/developer/user/assistant/tool）→ Anthropic content
    blocks（text/image/thinking/tool_use/tool_result）；V4 tool/result 消息
    （role 'tool' 平铺 content + 顶层 toolCallId/isError）序列化为 user 消息内
    的 tool_result 块；
  * 请求体 ``{model, max_tokens, system?, messages, tools?, stream, thinking?,
    output_config:{effort}?, temperature?, stop_sequences?}``；
  * SSE 事件派发（message_start / content_block_start/delta/stop /
    message_delta / message_stop / ping / error），以 message_stop 为完成点；
  * 响应翻译：tool_use→tool-call、thinking→reasoning、stop_reason 映射、
    usage 映射（Anthropic 拼写 input_tokens/output_tokens/cache_*）；
  * HTTP 错误码映射（401/403→AUTH、quota→QUOTA、429→RATE_LIMIT、
    400 上下文超限→CONTEXT_WINDOW_EXCEEDED、400/413→INVALID_REQUEST、
    500+→SERVER、其余 HTTP_<status>）。
"""
from __future__ import annotations

import base64
import email.utils
import json
import re
from datetime import datetime, timezone
from typing import Any

from ..core.session import reasoning_block
from .content import (
    content_has_image,
    offloaded_image_text,
    project_offloaded_images,
    request_image_handle_text,
    required_image_offload,
)
from .protocol import (
    AUTH,
    CONTEXT_WINDOW_EXCEEDED,
    EMPTY_RESPONSE,
    INVALID_REQUEST,
    MALFORMED_RESPONSE,
    QUOTA,
    RATE_LIMIT,
    SERVER,
    STREAM_CLOSED,
    UNSUPPORTED_REASONING_EFFORT,
    LlmFailure,
    StreamAborted,
    StreamChunk,
    _aiter_raced,
)

__all__ = [
    "MESSAGES_FILES_BETA",
    "UNSUPPORTED_CONTENT",
    "messages_api_root",
    "provider_error",
    "provider_retry_after_ms",
    "request_id",
    "serialize",
    "serialize_messages",
    "serialize_messages_with_images",
    "tool_input",
]

#: Messages 文件操作与 file-referenced 图片请求所需的显式 opt-in。
MESSAGES_FILES_BETA = "files-api-2025-04-14"

UNSUPPORTED_CONTENT = "UNSUPPORTED_CONTENT"

_REASONING_EFFORTS = ("off", "low", "high", "max")


def messages_api_root(base_url: str) -> str:
    """解析 Messages API 根，避免重复显式 provider 版本路径（上游 messagesApiRoot）。

    base 去尾斜杠后，路径已以 ``/v1`` 结尾则原样返回，否则追加 ``/v1``。
    """
    base = base_url.rstrip("/")
    from urllib.parse import urlsplit

    if urlsplit(base).path.endswith("/v1"):
        return base
    return base + "/v1"


def _unsupported(type_: str) -> None:
    raise LlmFailure(UNSUPPORTED_CONTENT, f"DeepSeek Messages cannot represent {type_}")


def tool_input(raw: str) -> dict:
    """历史 tool-call arguments（原始 JSON 字符串）→ tool_use input。

    上游 toolInput：不可解析或非对象数组 → 空对象；durable 内容不变。
    """
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


# ---------- 消息序列化（serialize.ts） ----------

def _assistant_blocks(message: dict) -> list[dict]:
    blocks: list[dict] = []
    for block in message.get("content") or []:
        btype = block.get("type")
        if btype == "text":
            blocks.append({"type": "text", "text": block["text"]})
        elif btype == "reasoning":
            # mini 无 ReplayEnvelope 重放双半（见 protocol.StreamChunk 简化标注），
            # 历史 thinking 不带 signature；上游仅在 replay 元数据可用时才附。
            blocks.append({"type": "thinking", "thinking": block["text"]})
        elif btype == "tool-call":
            blocks.append({"type": "tool_use", "id": block["id"], "name": block["name"],
                           "input": tool_input(block["arguments"])})
        else:
            _unsupported(f"assistant content {btype}")
    return blocks


def _input_blocks(blocks: list, message_index: int, image_parts: dict | None) -> list[dict]:
    """user/tool-result 内容块 → Anthropic input 块（text/image）。

    reasoning/tool-call 块被跳过；file 等其它块类型不可表示 → UNSUPPORTED_CONTENT。
    """
    out: list[dict] = []
    for block_index, block in enumerate(blocks or []):
        btype = block.get("type")
        if btype == "text":
            if block.get("text"):
                out.append({"type": "text", "text": block["text"]})
        elif btype in ("reasoning", "tool-call"):
            continue
        elif btype == "image":
            parts = image_parts.get((message_index, block_index)) if image_parts is not None else None
            if parts is None:
                raise LlmFailure(
                    UNSUPPORTED_CONTENT,
                    "DeepSeek Messages cannot represent image content on this route.")
            out.extend(parts)
        else:
            _unsupported(f"user/tool-result content {btype}")
    return out


def _build_messages(messages: list, *, in_history: bool,
                    image_parts: dict | None) -> tuple[list[dict], str | None]:
    """把 harness 历史折叠为 Anthropic wire 消息。

    @returns (messages, history_system)：history_system 是前导 system 消息文本
      （非 in-history 路由归并到顶层 system 字段；in-history 路由把后续 system
      更新以 role 'system' 留在 messages 中）。
    """
    wire: list[dict] = []
    history_system: str | None = None
    system_updates: list[dict] = []

    def flush_system_updates() -> None:
        nonlocal system_updates
        if not system_updates:
            return
        # Harness 在用户输入前受理 system 更新；Messages 把同一更新放到该
        # user/tool-result 轮之后、下一个 assistant 之前。
        if not wire or wire[-1]["role"] != "user":
            _unsupported("system update without a preceding user or tool-result turn")
        wire.extend(system_updates)
        system_updates = []

    for message_index, message in enumerate(messages):
        role = message.get("role")
        content = message.get("content") or []
        if role == "developer":
            # 延迟工具定义持久化给 V4；provider 加载有意推迟。
            _unsupported("developer message")
        if any(block.get("type") in ("tool-addition", "tool-removal") for block in content):
            _unsupported("tool-change blocks outside developer messages")
        if role == "system":
            texts = [block for block in content if block.get("type") == "text"]
            if len(texts) != len(content):
                _unsupported("non-text system message")
            text = "".join(block["text"] for block in texts)
            if in_history and wire:
                if not text:
                    _unsupported("empty in-history system update")
                system_updates.append({"role": "system", "content": [{"type": "text", "text": text}]})
            else:
                history_system = text
            continue
        if role == "assistant":
            flush_system_updates()
            blocks = _assistant_blocks(message)
        elif role == "tool":
            result: dict = {
                "type": "tool_result",
                "tool_use_id": message.get("toolCallId"),
                "content": _input_blocks(content, message_index, image_parts),
            }
            if message.get("isError") is not None:
                result["is_error"] = bool(message["isError"])
            blocks = [result]
        else:  # user
            blocks = _input_blocks(content, message_index, image_parts)
        if role == "user" and not blocks:
            continue
        wire_role = "user" if role == "tool" else role
        if wire and wire[-1]["role"] == wire_role:
            wire[-1]["content"].extend(blocks)
        else:
            wire.append({"role": wire_role, "content": blocks})
    flush_system_updates()

    pending: set = set()
    for message in wire:
        if message["role"] == "assistant":
            calls = [block for block in message["content"] if block["type"] == "tool_use"]
            pending = {block["id"] for block in calls}
            if len(pending) != len(calls):
                raise LlmFailure(INVALID_REQUEST, "DeepSeek Messages duplicate tool call id")
        elif message["role"] == "user":
            results = [block for block in message["content"] if block["type"] == "tool_result"]
            for result in results:
                if result["tool_use_id"] not in pending:
                    raise LlmFailure(INVALID_REQUEST,
                                     "DeepSeek Messages tool result has no matching call")
                pending.discard(result["tool_use_id"])
            if pending:
                raise LlmFailure(INVALID_REQUEST,
                                 "DeepSeek Messages tool calls need immediate results")
            # tool_result 块排在 user 消息内容的最前（上游同款重排）。
            message["content"] = results + [
                block for block in message["content"] if block["type"] != "tool_result"]
    if pending:
        raise LlmFailure(INVALID_REQUEST, "DeepSeek Messages history ends with unresolved tools")
    return wire, history_system


def serialize_messages(messages: list, *, in_history: bool = False,
                       model: str | None = None, models=()) -> list[dict]:
    """序列化为 Anthropic wire 消息（文本路径；image 未准备即拒绝）。"""
    if model is not None:
        in_history = _in_history(models, model)
    wire, _system = _build_messages(messages, in_history=in_history, image_parts=None)
    return wire


def _in_history(models, model: str) -> bool:
    entry = next((item for item in models if item.id == model), None)
    return entry is not None and entry.systemPromptUpdate == "in-history"


def _access_for(images: dict, ref: dict):
    resolve = images.get("resolveImageAccess")
    if resolve is None:
        return None
    access = resolve(ref)
    if access is None or isinstance(access, dict):
        return access
    return {"readonlyPath": access.readonlyPath}


async def _image_wire_parts(block: dict, images: dict, location: dict) -> list[dict]:
    """一张 durable 图片 → [handle 文本, image source]（上游 input 的 image 分支）。"""
    attachment_id = str(block["attachment"]["attachmentId"])
    version = images["requestImages"].get(attachment_id)
    if version is None:
        raise LlmFailure(INVALID_REQUEST,
                         f"DeepSeek Messages request image {attachment_id} is missing")
    text_part = {
        "type": "text",
        "text": request_image_handle_text(
            block["attachment"], version, _access_for(images, block["attachment"])),
    }
    if images["representation"]["kind"] == "file":
        file_id = await images["representation"]["resolveFileId"](version, block, location)
        image_part = {"type": "image", "source": {"type": "file", "file_id": file_id}}
    else:
        image_part = {"type": "image", "source": {
            "type": "base64",
            "media_type": version["mediaType"],
            "data": base64.b64encode(version["data"]).decode("ascii"),
        }}
    return [text_part, image_part]


def assert_supported_image_roles(messages: list) -> None:
    """拒绝其历史格式无法承载图片输入的角色（上游 prepareImages 的 role 检查）。"""
    for message in messages:
        if message.get("role") not in ("user", "tool") and content_has_image(
                message.get("content") or []):
            raise LlmFailure(
                UNSUPPORTED_CONTENT,
                "DeepSeek Messages supports images only in user messages and tool results, "
                f"not {message.get('role')}.")


def assert_retained_images_fit(messages: list, images: dict) -> None:
    """拒绝保留 occurrence 超过路由预算的请求（携带 IMAGE_OFFLOAD_REQUIRED 计数）。"""
    representation = images["representation"]["kind"]
    from .protocol import IMAGE_OFFLOAD_REQUIRED, LlmImageRequestBudget

    def version_bytes(block: dict) -> int:
        version = images["requestImages"].get(str(block["attachment"]["attachmentId"]))
        return version["bytes"] if version else 0

    offload_images = required_image_offload(
        messages,
        LlmImageRequestBudget(
            representation=representation,
            maxBytes=images.get("maxRequestImageBytes"),
            maxImages=images.get("maxImagesPerRequest"),
            byteQuantum=images.get("byteQuantum"),
            countQuantum=images.get("countQuantum"),
        ),
        version_bytes,
    )
    if offload_images > 0:
        raise LlmFailure(
            IMAGE_OFFLOAD_REQUIRED,
            f"DeepSeek Messages {representation} request images exceed the route budget; "
            f"{offload_images} more oldest occurrence(s) must be offloaded.",
            offload_images=offload_images,
        )


async def resolve_image_parts(messages: list, images: dict) -> tuple[list, dict]:
    """解析一次 image-capable 请求：offload 投影 + 预算断言 + 逐图 wire 化。

    @returns (projected_messages, parts)；parts 以 (message_index, block_index)
      为键，指向该 image 块对应的有序 wire input 列表。
    """
    assert_supported_image_roles(messages)
    assert_retained_images_fit(messages, images)
    projected = project_offloaded_images(
        messages, lambda ref: offloaded_image_text(ref, _access_for(images, ref)))
    parts: dict = {}
    for message_index, message in enumerate(projected):
        image_index = 0
        for block_index, block in enumerate(message.get("content") or []):
            if block.get("type") != "image":
                continue
            image_index += 1
            parts[(message_index, block_index)] = await _image_wire_parts(
                block, images, {"message": message_index + 1, "image": image_index})
    return projected, parts


async def serialize_messages_with_images(messages: list, images: dict, *,
                                         in_history: bool = False,
                                         model: str | None = None,
                                         models=()) -> list[dict]:
    """image-capable 历史的 Anthropic wire 消息（文本路径的 image 版本）。"""
    if model is not None:
        in_history = _in_history(models, model)
    projected, parts = await resolve_image_parts(messages, images)
    wire, _system = _build_messages(projected, in_history=in_history, image_parts=parts)
    return wire


def serialize(messages: list, *, model: str, models=(), system: str | None = None,
              in_history: bool | None = None, reasoning_effort: str | None = None,
              thinking: str | None = None, max_tokens: int | None = None,
              default_max_tokens: int = 256_000,
              temperature: float | None = None, stop: list | None = None,
              tools: list | None = None, purpose: str | None = None,
              image_parts: dict | None = None) -> dict:
    """组装完整 Messages 请求体（上游 serialize）。

    effort 决议：``purpose == 'session-title'`` 强制 off；否则请求级
    reasoning_effort → thinking 禁用则 off，否则 high。thinking 禁用时非 off
    的 effort 被拒绝（UNSUPPORTED_REASONING_EFFORT）。
    """
    if in_history is None:
        in_history = _in_history(models, model)
    if tools is not None and any(tool.get("deferLoading") is True for tool in tools):
        _unsupported("deferred tool loading")
    wire, history_system = _build_messages(
        messages, in_history=in_history, image_parts=image_parts)
    effort = reasoning_effort if reasoning_effort is not None else (
        "off" if thinking == "disabled" else "high")
    if purpose == "session-title":
        effort = "off"
    if effort not in _REASONING_EFFORTS or (thinking == "disabled" and effort != "off"):
        raise LlmFailure(
            UNSUPPORTED_REASONING_EFFORT,
            f"DeepSeek Messages does not support reasoning effort {effort}")
    model_entry = next((entry for entry in models if entry.id == model), None)
    limit = max_tokens
    if limit is None:
        limit = (model_entry.maxTokens
                 if model_entry is not None and model_entry.maxTokens is not None
                 else default_max_tokens)
    system_text = "\n\n".join(part for part in (system, history_system) if part)
    body: dict[str, Any] = {
        "model": model,
        "stream": True,
        "messages": wire,
        "max_tokens": limit,
        "thinking": {"type": "disabled" if effort == "off" else "enabled"},
    }
    if effort != "off":
        body["output_config"] = {"effort": effort}
    if system_text:
        body["system"] = system_text
    if temperature is not None:
        body["temperature"] = temperature
    if stop is not None:
        body["stop_sequences"] = stop
    if tools is not None:
        body["tools"] = [
            {"name": tool["name"], "description": tool.get("description", ""),
             "input_schema": tool.get("parameters", {})}
            for tool in tools
        ]
    return body


# ---------- HTTP / 带内错误映射（transport.ts） ----------

# 上游 error.ts 的正则集（isContextWindowExceededError / isQuotaExceededError）
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


def _is_context_window_exceeded(detail: str) -> bool:
    return bool(
        _STRUCTURED_CONTEXT_OVERFLOW.search(detail)
        or _CONTEXT_LENGTH_WINDOW.search(detail)
        or _TOO_LARGE_FOR_CONTEXT.search(detail)
        or _TOO_LONG_FOR_MODEL.search(detail)
        or _EXCEEDS_MODEL_CONTEXT.search(detail)
    )


def _is_quota_exceeded(detail: str) -> bool:
    return bool(_QUOTA_INSUFFICIENT.search(detail) or _QUOTA_EXCEEDED.search(detail))


def _error_code(status: int | None, detail: str, type_: str = "") -> str:
    """上游 providerError 分类：status + provider type + 规范化 detail。"""
    if status in (401, 403) or type_ in ("authentication_error", "permission_error"):
        return AUTH
    if _is_quota_exceeded(detail) or status == 402:
        return QUOTA
    if status == 429 or type_ == "rate_limit_error":
        return RATE_LIMIT
    if _is_context_window_exceeded(detail):
        return CONTEXT_WINDOW_EXCEEDED
    if status in (400, 413) or type_ == "invalid_request_error":
        return INVALID_REQUEST
    if (status is not None and status >= 500) or type_ in ("api_error", "overloaded_error"):
        return SERVER
    return SERVER if status is None else f"HTTP_{status}"


def _http_error_code(status: int, body: str) -> str:
    """HTTP 错误码映射（保留既有码表；400 上下文判定不做裸子串误判）。"""
    return _error_code(status, body)


def provider_retry_after_ms(value: str | None) -> int | None:
    """Retry-After → 相对毫秒（纯数字秒 ×1000；否则 ISO 8601 / HTTP-date）。"""
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
    """provider 请求跟踪 id（request-id / x-request-id / x-deepseek-request-id）。"""
    if headers is None:
        return None
    value = (headers.get("request-id") or headers.get("x-request-id")
             or headers.get("x-deepseek-request-id"))
    if value is None or len(value) == 0:
        return None
    return str(value)


def _error_detail(raw: Any) -> str:
    """从已解析响应体抽取分类 detail（code + type + message）。"""
    envelope = raw if isinstance(raw, dict) else {}
    error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
    return " ".join(
        field for field in (error.get("code"), error.get("type"), error.get("message"))
        if isinstance(field, str))


def _error_message(raw: Any) -> str | None:
    envelope = raw if isinstance(raw, dict) else {}
    error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
    message = error.get("message")
    return message if isinstance(message, str) else None


def provider_error(raw: Any, status: int | None = None, headers=None) -> LlmFailure:
    """归一化 HTTP 与带内 Messages 错误为 provider 中立失败（上游 providerError）。"""
    envelope = raw if isinstance(raw, dict) else {}
    error = envelope.get("error") if isinstance(envelope.get("error"), dict) else {}
    type_ = error.get("type") if isinstance(error.get("type"), str) else ""
    detail = _error_detail(raw)
    code = _error_code(status, f"{detail} {_error_message(raw) or ''}".strip(), type_)
    message = _error_message(raw) or (
        f"DeepSeek Messages request failed ({status if status is not None else 'stream error'})")
    delay = provider_retry_after_ms(headers.get("retry-after") if headers is not None else None)
    return LlmFailure(
        code, message,
        status=status if status is not None else None,
        provider_retry_after_ms=delay,
        request_id=request_id(headers),
    )


# ---------- SSE 事件派发（sse.ts） ----------

def _object(value: Any, detail: str = "expected a JSON object") -> dict:
    if not isinstance(value, dict):
        raise LlmFailure(MALFORMED_RESPONSE, f"DeepSeek Messages stream: {detail}")
    return value


async def parse_sse_frames(aiter_lines, abort_event=None):
    """解析 SSE 帧为已解码 provider 事件（上游 parseSse）。

    事件只在空行终结时派发，EOF 处的未终止尾部是截断（丢弃）；载荷非 JSON 或
    缺 type → MALFORMED_RESPONSE；带内 ``error`` 事件即 provider 失败。
    """
    data_lines: list[str] = []
    event_name: str | None = None
    async for line in _aiter_raced(aiter_lines, abort_event):
        if line == "":
            if not data_lines:
                event_name = None
                continue
            data = "\n".join(data_lines)
            data_lines = []
            name = event_name
            event_name = None
            try:
                raw = json.loads(data)
            except json.JSONDecodeError:
                raise LlmFailure(
                    MALFORMED_RESPONSE,
                    f"DeepSeek Messages SSE contains invalid JSON: {data[:120]}") from None
            event = _object(raw, "SSE event is not a JSON object")
            if not isinstance(event.get("type"), str) or (
                    name is not None and name != event["type"]):
                raise LlmFailure(MALFORMED_RESPONSE, "DeepSeek Messages SSE event type mismatch")
            if event["type"] == "error":
                raise provider_error(event, None)
            yield event
            continue
        if line.startswith("data:"):
            payload = line[5:]
            if payload.startswith(" "):
                payload = payload[1:]
            data_lines.append(payload)
        elif line.startswith("event:"):
            name = line[6:]
            event_name = name[1:] if name.startswith(" ") else name
        # 其它字段（注释/id:/retry:）跳过


# ---------- 响应翻译（translate.ts） ----------

def _string(value: Any) -> str:
    if not isinstance(value, str):
        raise LlmFailure(MALFORMED_RESPONSE,
                         "DeepSeek Messages expected a string field")
    return value


def _malformed(detail: str) -> None:
    raise LlmFailure(MALFORMED_RESPONSE, f"DeepSeek Messages stream: {detail}")


def _wire_index(event: dict) -> int:
    index = event.get("index")
    if not isinstance(index, int) or isinstance(index, bool) or index < 0:
        _malformed("invalid block index")
    return index


def _update_usage(usage: dict, raw: Any) -> None:
    if raw is None:
        return
    fields = _object(raw)
    keys = {
        "input_tokens": "inputTokens",
        "output_tokens": "outputTokens",
        "cache_read_input_tokens": "cacheReadTokens",
        "cache_creation_input_tokens": "cacheWriteTokens",
    }
    for wire_key, local in keys.items():
        value = fields.get(wire_key)
        if value is None:
            continue
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            _malformed(f"invalid {wire_key}")
        usage[local] = value


def _start_block(event: dict, index: int) -> dict:
    native = _object(event.get("content_block"))
    block_type = native.get("type")
    if block_type == "text":
        content: dict = {"type": "text", "text": _string(native.get("text"))}
    elif block_type == "thinking":
        content = {"type": "reasoning", "text": _string(native.get("thinking"))}
        if native.get("signature") is not None:
            _string(native.get("signature"))
    elif block_type == "tool_use":
        content = {
            "type": "tool-call",
            "id": _string(native.get("id")),
            "name": _string(native.get("name")),
            "arguments": json.dumps(_object(native.get("input")), separators=(",", ":")),
        }
        if not content["id"] or not content["name"]:
            _malformed("empty tool identity")
    else:
        raise LlmFailure(
            UNSUPPORTED_CONTENT,
            f"DeepSeek Messages does not support response block {block_type!r}")
    return {"index": index, "content": content, "closed": False, "json": ""}


def _delta_chunk(block: dict, raw: Any) -> StreamChunk | None:
    delta = _object(raw)
    content = block["content"]
    delta_type = delta.get("type")
    if delta_type == "text_delta" and content["type"] == "text":
        text = _string(delta.get("text"))
        content["text"] += text
        return StreamChunk("text-delta", index=block["index"], text=text)
    if delta_type == "thinking_delta" and content["type"] == "reasoning":
        text = _string(delta.get("thinking"))
        content["text"] += text
        return StreamChunk("reasoning-delta", index=block["index"], text=text)
    if delta_type == "signature_delta" and content["type"] == "reasoning":
        _string(delta.get("signature"))
        return None
    if delta_type == "input_json_delta" and content["type"] == "tool-call":
        fragment = _string(delta.get("partial_json"))
        block["json"] += fragment
        return StreamChunk("tool-call-delta", index=block["index"],
                           id=content["id"], argumentsDelta=fragment)
    _malformed(f"unsupported delta {delta_type} for {content['type']}")
    return None


def _stop_reason(raw: Any) -> dict:
    if raw in ("end_turn", "stop_sequence"):
        return {"kind": "stop"}
    if raw == "tool_use":
        return {"kind": "tool-calls"}
    if raw == "max_tokens":
        return {"kind": "max-tokens"}
    _malformed(f"unsupported stop reason {raw}")
    return {"kind": "stop"}


_CONTENT_EVENTS = (
    "content_block_start", "content_block_delta", "content_block_stop",
    "message_delta", "message_stop",
)


async def translate(events) -> "Any":
    """把已解码 provider 事件翻译为 Harness StreamChunk（上游 translate）。

    保持块顺序与累积 usage；message_stop 是唯一完成点，之后 yield usage 与
    finish。流在 message_stop 之前结束 → STREAM_CLOSED。
    """
    blocks: dict[int, dict] = {}
    usage: dict = {"inputTokens": 0, "outputTokens": 0}
    started = False
    reason: dict | None = None
    async for event in events:
        event_type = event["type"]
        if event_type == "message_start":
            if started:
                _malformed("duplicate message_start")
            message = _object(event.get("message"))
            _update_usage(usage, message.get("usage"))
            started = True
            continue
        if event_type not in _CONTENT_EVENTS:
            # Anthropic 允许附加事件类型；内容事件在下方校验。
            continue
        if not started:
            _malformed("event precedes message_start")
        if event_type == "content_block_start":
            wire_index = _wire_index(event)
            if wire_index in blocks or reason is not None:
                _malformed("block starts after settlement or repeats an index")
            block = _start_block(event, len(blocks))
            blocks[wire_index] = block
            yield StreamChunk("block-start", index=block["index"],
                              blockType=block["content"]["type"])
            if block["content"]["type"] in ("text", "reasoning"):
                if block["content"]["text"]:
                    yield StreamChunk(
                        "text-delta" if block["content"]["type"] == "text" else "reasoning-delta",
                        index=block["index"], text=block["content"]["text"])
            else:
                yield StreamChunk("tool-call-delta", index=block["index"],
                                  id=block["content"]["id"], name=block["content"]["name"],
                                  argumentsDelta="")
        elif event_type in ("content_block_delta", "content_block_stop"):
            block = blocks.get(_wire_index(event))
            if block is None or block["closed"]:
                _malformed("delta/stop without an open block")
            if event_type == "content_block_delta":
                chunk = _delta_chunk(block, event.get("delta"))
                if chunk is not None:
                    yield chunk
            else:
                block["closed"] = True
                if block["content"]["type"] == "tool-call" and block["json"]:
                    block["content"]["arguments"] = block["json"]
                yield StreamChunk("block-end", index=block["index"],
                                  block=dict(block["content"]))
        elif event_type == "message_delta":
            delta = _object(event.get("delta"))
            if delta.get("stop_reason") is not None:
                reason = _stop_reason(delta["stop_reason"])
            if event.get("usage") is not None:
                _update_usage(usage, event["usage"])
        else:  # message_stop
            if reason is None or any(not block["closed"] for block in blocks.values()):
                _malformed("message_stop without settled blocks and stop reason")
            if len(blocks) == 0 and reason["kind"] == "stop":
                raise LlmFailure(EMPTY_RESPONSE, "DeepSeek Messages returned no content")
            # 截断的 tool JSON 保留在流中，由共享 assembler 裁剪。
            if reason["kind"] != "max-tokens":
                for block in blocks.values():
                    if block["content"]["type"] != "tool-call":
                        continue
                    try:
                        parsed = json.loads(block["content"]["arguments"])
                    except (TypeError, ValueError):
                        _malformed("tool input is invalid JSON")
                    if not isinstance(parsed, dict):
                        _malformed("tool input is invalid JSON")
            usage["totalTokens"] = (usage["inputTokens"] + usage["outputTokens"]
                                    + usage.get("cacheReadTokens", 0)
                                    + usage.get("cacheWriteTokens", 0))
            yield StreamChunk("usage", usage=dict(usage))
            yield StreamChunk("finish", reason=reason)
            return
    raise LlmFailure(STREAM_CLOSED, "SSE 流在 message_stop 之前结束，响应不完整")
