"""第 4 章：LLM 流式适配 —— 统一 StreamChunk 协议 + 官方 SSE 适配器。

对应 dsh 真实源码：packages/llm/llm（协议）+ packages/llm/llm-deepseek（适配器）。

StreamChunk 字段与上游协议对齐（packages/llm/llm/src/types.ts）：
  * block-start     { index, blockType }         # blockType: text | reasoning | tool-call | ...
  * text-delta      { index, text }
  * reasoning-delta { index, text }
  * tool-call-delta { index, id, name?, argumentsDelta }   # argumentsDelta 为增量分片
  * block-end       { index, block }              # block 为组装好的 ContentBlock
  * usage           { usage }                     # 必须在 finish 之前
  * finish          { reason }                    # reason 为 {kind: ...} 对象

协议硬性规定：
  1. 每个 ContentBlock 一对 block-start / block-end；block-end 携带完整块
  2. usage 必须在 finish 之前，finish 之后不再有值
  3. finish reason 是对象：{kind:'stop'|'tool-calls'|'max-tokens'} 或
     {kind:'aborted'|'error', failure}（上游 FinishReasonMap）
  4. 授权/请求/上下文溢出统一为 LlmFailure；上下文溢出编码 CONTEXT_WINDOW_EXCEEDED
"""
from __future__ import annotations

import email.utils
import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any, Iterator

from .retry_policy import resolve_retry_policy
from .session import create_message, text_block, reasoning_block

STREAM_CHUNK_KINDS = frozenset({
    "block-start", "text-delta", "reasoning-delta",
    "tool-call-delta", "block-end", "usage", "finish",
})

# 错误码（上游 llm/src 的 LlmError 词汇）
AUTH = "AUTH"
RATE_LIMIT = "RATE_LIMIT"
CONTEXT_WINDOW_EXCEEDED = "CONTEXT_WINDOW_EXCEEDED"
SERVER = "SERVER"
TIMEOUT = "TIMEOUT"
TRANSPORT = "TRANSPORT"
STREAM_CLOSED = "STREAM_CLOSED"
EMPTY_RESPONSE = "EMPTY_RESPONSE"
REQUEST_ERROR = "REQUEST_ERROR"


class StreamChunk(dict):
    """统一流协议：kind + payload。dict 子类，天然可 JSON 序列化。"""

    def __init__(self, kind: str, **payload: Any):
        if kind not in STREAM_CHUNK_KINDS:
            raise ValueError(f"未知 chunk kind: {kind}")
        super().__init__({"kind": kind, **payload})


class LlmFailure(Exception):
    """统一错误收口：携带上游错误码（AUTH / RATE_LIMIT / CONTEXT_WINDOW_EXCEEDED ...）。

    可选字段对齐上游 LlmError 的 failure facts（llm/src/adapter-failure.ts）：
    status（HTTP 状态）、providerRetryAfterMs（provider 要求的等待毫秒数，
    来自 429 的 Retry-After）、requestId（provider 请求跟踪 id，来自
    x-request-id / x-deepseek-request-id）。这些字段是 llm-retry 恢复决策
    的输入（providerRetryAfterMs 优先于本地退避）。

    简化说明：上游 LlmError 携带 frozen failure 快照并支持 finish
    {kind:'error'|'aborted', failure} 带内失败路径；本实现以异常抛出，
    failure 结构为 {code, message, 可选字段}。
    """

    def __init__(self, code: str, message: str,
                 status: int | None = None,
                 provider_retry_after_ms: int | None = None,
                 request_id: str | None = None):
        super().__init__(message)
        self.code = code
        self.status = status
        self.provider_retry_after_ms = provider_retry_after_ms
        self.request_id = request_id
        failure: dict[str, Any] = {"code": code, "message": message}
        if status is not None:
            failure["status"] = status
        if provider_retry_after_ms is not None:
            failure["providerRetryAfterMs"] = provider_retry_after_ms
        if request_id is not None:
            failure["requestId"] = request_id
        self.failure = failure


class LlmAdapter:
    """接口（Service Definition）：Consumer（agent-loop）只依赖这个协议。

    retry_policy：provider 属地的重试策略（resolve_retry_policy 结果）；
    None 表示该适配器不配置策略（上游未注册 providerRetryPolicy 时重试
    服务直接委派、不重试）。llm-deepseek 默认解析为 normal 默认策略。
    """

    provider: str = "base"
    retry_policy: dict | None = None

    def stream(self, messages: list[dict], tools: list[dict]) -> Iterator[StreamChunk]:
        raise NotImplementedError


class BlockAssembler:
    """按 index 组装 ContentBlock（上游 assembler.ts 的简化版）。

    block-end 携带组装好的块，所以本实现只收集块与终态
    （usage / finish）；流式 UI 可改为逐片转发 delta。
    """

    def __init__(self):
        self.blocks: list[dict] = []
        self.usage: dict | None = None
        self.finish: dict | None = None

    def push(self, chunk: dict) -> None:
        kind = chunk["kind"]
        if kind == "block-end":
            self.blocks.append(chunk["block"])
        elif kind == "usage":
            self.usage = chunk["usage"]
        elif kind == "finish":
            self.finish = chunk["reason"]

    def message(self) -> dict:
        return create_message("assistant", self.blocks, {"kind": "model"})


class FakeLlmAdapter(LlmAdapter):
    """确定性假模型：第一次调用返回一次工具调用，之后返回最终文本。
    无需 API key 即可跑通完整回合，测试与联调专用。"""

    provider = "fake"

    def __init__(self, tool_call: dict | None = None, final_text: str = "任务完成。"):
        self._tool = tool_call
        self._text = final_text
        self.calls = 0

    def stream(self, messages, tools):
        self.calls += 1
        if self._tool and self.calls == 1:
            arguments = self._tool.get("arguments", {})
            arguments_text = json.dumps(arguments, ensure_ascii=False)
            yield StreamChunk("block-start", index=0, blockType="tool-call")
            yield StreamChunk("tool-call-delta", index=0, id="call_0",
                              name=self._tool["name"], argumentsDelta=arguments_text)
            yield StreamChunk("block-end", index=0, block={
                "type": "tool-call", "id": "call_0", "name": self._tool["name"],
                "arguments": arguments_text,
            })
            yield StreamChunk("finish", reason={"kind": "tool-calls"})
        else:
            yield StreamChunk("block-start", index=0, blockType="text")
            yield StreamChunk("text-delta", index=0, text=self._text)
            yield StreamChunk("block-end", index=0, block={
                "type": "text", "text": self._text,
            })
            yield StreamChunk("finish", reason={"kind": "stop"})


# ---------- DeepSeek wire 序列化（llm-deepseek/src/serialize.ts） ----------

def serialize_messages(messages: list[dict]) -> list[dict]:
    """把 harness 消息序列化为 DeepSeek chat-completions wire 消息。

    与上游一致：system → {role:'system'}；assistant 的 text 合并为 content、
    reasoning 仅在带 tool_calls 时作为 reasoning_content 回传、tool-call 块
    转为 tool_calls；user role 消息的文本走 {role:'user'}，每个 tool-result 块
    展开为独立的 {role:'tool', tool_call_id} 消息（空输出用 '(no output)'）。
    """
    wire: list[dict] = []

    def flatten_text(blocks: list) -> str:
        return "".join(b["text"] for b in blocks if b.get("type") == "text")

    for message in messages:
        blocks = message.get("content", [])
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


def _http_error_code(status: int, body: str) -> str:
    """上游 httpErrorCode 映射：401/403→AUTH、429→RATE_LIMIT、
    400 上下文超限→CONTEXT_WINDOW_EXCEEDED、500+→SERVER。"""
    if status in (401, 403):
        return AUTH
    if status == 429:
        return RATE_LIMIT
    if status >= 500:
        return SERVER
    if status == 400 and ("context" in body.lower() or "maximum context" in body.lower()):
        return CONTEXT_WINDOW_EXCEEDED
    return REQUEST_ERROR


def provider_retry_after_ms(value: str | None) -> int | None:
    """上游 providerRetryAfterMs（llm-deepseek/src/adapter.ts 同构）。

    纯数字秒 → ×1000；否则尝试 HTTP-date（RFC 7231）解析为相对毫秒；
    无效/非正 → None（视为未提供）。
    """
    if value is None or len(value.strip()) == 0:
        return None
    text = value.strip()
    if text.isdigit():
        delay = int(text) * 1000
        return delay if delay > 0 else None
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
    """上游 mapFinishReason：stop→stop、tool_calls→tool-calls、length→max-tokens、
    其它→{kind:'error'}。"""
    if reason == "stop":
        return {"kind": "stop"}
    if reason == "tool_calls":
        return {"kind": "tool-calls"}
    if reason == "length":
        return {"kind": "max-tokens"}
    return {"kind": "error", "failure": {"code": reason.upper() if reason else "UNKNOWN", "message": f"provider finish_reason: {reason}"}}


class DeepSeekAdapter(LlmAdapter):
    """DeepSeek 官方 chat API 的 SSE 适配器（纯 stdlib urllib，零依赖）。

    与上游 llm-deepseek 一致：
      * 请求体 stream:true + stream_options.include_usage
      * SSE 必须出现字面 [DONE]，EOF 未到 [DONE] 抛 STREAM_CLOSED（截断响应不可信）
      * finish reason 与 usage 在 [DONE] 之后发射；空响应抛 EMPTY_RESPONSE
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

    def stream(self, messages, tools):
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
            piece = json.loads(data)
            if piece.get("usage"):
                usage = piece["usage"]
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