"""DeepSeek 官方 chat API 的 wire 序列化与 SSE 适配器。

对应 dsh 真实源码：packages/llm/llm-deepseek/src（adapter.ts + serialize.ts +
sse.ts + translate.ts）。

  * 请求体 stream:true + stream_options.include_usage
  * SSE 必须出现字面 [DONE]，EOF 未到 [DONE] 抛 STREAM_CLOSED（截断响应不可信）
  * finish reason 与 usage 在 [DONE] 之后发射；空响应抛 EMPTY_RESPONSE
  * 错误映射：401/403→AUTH、quota 措辞→QUOTA、429→RATE_LIMIT、
    400 上下文超限→CONTEXT_WINDOW_EXCEEDED（否则 INVALID_REQUEST）、500+→SERVER、
    其余→HTTP_<status>；LlmError facts（status / providerRetryAfterMs / requestId）
"""
from __future__ import annotations

import email.utils
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any

from ..core.session import reasoning_block
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
    TIMEOUT,
    TRANSPORT,
    LlmAdapter,
    LlmFailure,
    StreamChunk,
    _aiter_from_thread,
)
from .retry_policy import resolve_retry_policy

__all__ = ["DeepSeekAdapter", "provider_retry_after_ms", "request_id", "serialize_messages"]


# ---------- DeepSeek wire 序列化（llm-deepseek/src/serialize.ts） ----------

UNSUPPORTED_CONTENT = "UNSUPPORTED_CONTENT"


def content_has_image(blocks: list) -> bool:
    """内容块是否含 image 块（上游 contentHasImage，message.ts 同语义）。"""
    return any(b.get("type") == "image" for b in blocks or [])


def serialize_messages(messages: list[dict]) -> list[dict]:
    """把 harness 消息序列化为 DeepSeek chat-completions wire 消息。

    与上游一致：system → {role:'system'}；assistant 的 text 合并为 content、
    reasoning 仅在带 tool_calls 时作为 reasoning_content 回传、tool-call 块
    转为 tool_calls；user role 消息的文本走 {role:'user'}，每个 tool-result 块
    展开为独立的 {role:'tool', tool_call_id} 消息（空输出用 '(no output)'）。

    image 块显式拒绝（上游 serialize.ts assertTextOnly → UNSUPPORTED_CONTENT）：
    此 wire 路由是纯文本，静默丢弃会丢失图片内容。
    """
    wire: list[dict] = []

    def flatten_text(blocks: list) -> str:
        return "".join(b["text"] for b in blocks if b.get("type") == "text")

    for message in messages:
        blocks = message.get("content", [])
        if content_has_image(blocks):
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
                **({"reasoning_content": reasoning} if tool_calls and reasoning else {}),
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
    """上游 httpErrorCode 映射（llm-deepseek/src/adapter.ts:138-149）：
    401/403→AUTH；quota 措辞（任意状态，先于 429）→QUOTA；429→RATE_LIMIT；
    400 上下文超限→CONTEXT_WINDOW_EXCEEDED、否则→INVALID_REQUEST；
    ≥500→SERVER；其余→HTTP_<status>。

    上下文/quota 判定复刻上游 error.ts 正则集（isContextWindowExceededError /
    isQuotaExceededError），避免裸子串误判。
    """
    if status in (401, 403):
        return AUTH
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
    """上游 mapUsage（translate.ts 同构）：TokenUsage 子集。

    inputTokens = prompt_tokens - cacheReadTokens；cacheReadTokens 来自
    prompt_cache_hit_tokens；reasoningTokens 来自
    completion_tokens_details.reasoning_tokens。
    """
    cache_read = usage.get("prompt_cache_hit_tokens")
    input_tokens = int(usage.get("prompt_tokens") or 0) - int(cache_read or 0)
    mapped = {"inputTokens": input_tokens,
              "outputTokens": int(usage.get("completion_tokens") or 0)}
    if cache_read:
        mapped["cacheReadTokens"] = int(cache_read)
    details = usage.get("completion_tokens_details") or {}
    if details.get("reasoning_tokens"):
        mapped["reasoningTokens"] = int(details["reasoning_tokens"])
    return mapped


class DeepSeekAdapter(LlmAdapter):
    """DeepSeek 官方 chat API 的 SSE 适配器（纯 stdlib urllib，零依赖）。

    与上游 llm-deepseek 一致：
      * 请求体 stream:true + stream_options.include_usage
      * SSE 必须出现字面 [DONE]，EOF 未到 [DONE] 抛 STREAM_CLOSED（截断响应不可信）
      * finish reason 与 usage 在 [DONE] 之后发射；空响应抛 EMPTY_RESPONSE

    简化标注（2026-08-17 上游 rc.7 审核）：上游推理 effort 提供 off/low/high/max
    四档（adapter.ts REASONING_EFFORTS，rc.7 新增 low）；mini 请求参数
    不承载 reasoningEffort（无 connector 配置面，默认走 provider 侧行为），
    仅接收响应侧 reasoning_content 与 usage.reasoningTokens。语义影响：无
    请求侧契约（mini 不宣称配置面）。见 AGENTS.md 简化清单。
    """

    provider = "deepseek-official"

    def __init__(self, api_key: str | None = None, base_url: str | None = None,
                 model: str = "deepseek-chat", max_tokens: int | None = None,
                 retry_policy: dict | None = None):
        self._key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
        self._base = (base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self._model = model
        self._max_tokens = max_tokens
        # 上游 llm-deepseek：retryPolicy 省略即 normal 默认（resolveRetryPolicy(undefined)）
        self.retry_policy = resolve_retry_policy(retry_policy, "llm-deepseek: retryPolicy")

    @property
    def model(self) -> str | None:
        return self._model

    def resolve_model_info(self) -> dict:
        """DeepSeek chat-completions 适配器只支持文本输入。

        对齐上游 adapter.ts resolveModelInfo：inputModalities 恒为 ['text']
        （serialize.ts assertTextOnly 拒绝 image 块的原因）。mini 无模型
        catalog，仅承载能力声明（教学简化）。
        """
        return {
            "provider": self.provider,
            "model": self._model,
            "input_modalities": ["text"],
        }

    async def stream(self, messages, tools, signal=None):
        """async 迭代器（对齐上游 async stream）：SSE 读取搬进 executor 线程，
        经 asyncio.Queue 桥接逐 chunk 产出；signal.aborted/.event 置位即中止
        （协作式取消，对齐上游 signal 携带进 stream 的语义）。

        简化标注：阻塞读取线程无法被强制中断（urlopen 120s 超时兜底），
        取消后 producer 线程继续排空至自然结束；流桥在无数据窗口内为
        事件驱动（abort_event 竞速），非轮询。
        """
        abort_event = getattr(signal, "event", None) if signal is not None else None
        body = self._build_body(messages, tools)
        async for chunk in _aiter_from_thread(lambda: self._iter_chunks(body), abort_event):
            yield chunk

    def _build_body(self, messages, tools) -> dict:
        body: dict[str, Any] = {
            "model": self._model,
            "messages": serialize_messages(messages),
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
        return body

    def _iter_chunks(self, body: dict):
        """同步 SSE 请求 + 解析（阻塞；经 _aiter_from_thread 桥接给 async stream）。"""
        req = urllib.request.Request(
            self._base + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._key},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            detail = e.read(500).decode("utf-8", "replace")
            code = _http_error_code(e.code, detail)
            raise LlmFailure(code, f"HTTP {e.code}: {detail}",
                             status=e.code,
                             provider_retry_after_ms=provider_retry_after_ms(
                                 e.headers.get("Retry-After")),
                             request_id=request_id(e.headers)) from e
        except urllib.error.URLError as e:
            # socket 超时被 urlopen 包装进 URLError.reason；上游区分 TIMEOUT
            if isinstance(e.reason, TimeoutError):
                raise LlmFailure(TIMEOUT, "请求超时") from e
            raise LlmFailure(TRANSPORT, f"网络错误: {e.reason}") from e
        except TimeoutError as e:
            raise LlmFailure(TIMEOUT, "请求超时") from e

        texts: dict[int, str] = {}
        reasonings: dict[int, str] = {}
        pending: dict[int, dict[str, str]] = {}
        usage: dict | None = None
        finish_reason: str | None = None
        saw_done = False
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
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
                    slot = pending.setdefault(tc["index"], {"id": "", "name": "", "arguments": ""})
                    fn = tc.get("function", {})
                    slot["id"] = tc.get("id") or slot["id"]
                    slot["name"] += fn.get("name", "")
                    slot["arguments"] += fn.get("arguments", "")

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
            call_id = slot["id"] or f"call_{idx}"
            yield StreamChunk("block-start", index=idx, blockType="tool-call")
            yield StreamChunk("tool-call-delta", index=idx, id=call_id,
                              name=slot["name"], argumentsDelta=slot["arguments"])
            yield StreamChunk("block-end", index=idx, block={
                "type": "tool-call", "id": call_id, "name": slot["name"],
                "arguments": slot["arguments"],
            })
        if not emitted:
            raise LlmFailure(EMPTY_RESPONSE, "模型返回了空响应（无文本、无推理、无工具调用）")
        if usage is not None:
            yield StreamChunk("usage", usage=usage)
        yield StreamChunk("finish", reason=_map_finish_reason(finish_reason))