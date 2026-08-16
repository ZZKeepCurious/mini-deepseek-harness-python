"""LLM 流式适配协议：统一 StreamChunk + 错误收口 + 适配器接口 + 块组装。

对应 dsh 真实源码：packages/llm/llm/src（types.ts + adapter.ts + adapter-failure.ts
+ assembler.ts）。

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

from typing import Any, Iterator

from ..core.session import create_message

__all__ = [
    "AUTH",
    "BlockAssembler",
    "CONTEXT_WINDOW_EXCEEDED",
    "EMPTY_RESPONSE",
    "LlmAdapter",
    "LlmFailure",
    "RATE_LIMIT",
    "REQUEST_ERROR",
    "SERVER",
    "STREAM_CHUNK_KINDS",
    "STREAM_CLOSED",
    "StreamChunk",
    "TIMEOUT",
    "TRANSPORT",
]

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