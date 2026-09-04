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

TokenUsage（usage 载荷）字段对齐上游 llm/src/types.ts:127-149：
  inputTokens / outputTokens（必填，disjoint 计数）+ 可选 cacheReadTokens /
  cacheWriteTokens / reasoningTokens / totalTokens（精确整调用总数，仅当
  供应商聚合 prompt/output 计数有效且自洽时提供，否则省略）。

协议硬性规定：
  1. 每个 ContentBlock 一对 block-start / block-end；block-end 携带完整块
  2. usage 必须在 finish 之前，finish 之后不再有值
  3. finish reason 是对象：{kind:'stop'|'tool-calls'|'max-tokens'} 或
     {kind:'aborted'|'error', failure}（上游 FinishReasonMap）
  4. 授权/请求/上下文溢出统一为 LlmFailure；上下文溢出编码 CONTEXT_WINDOW_EXCEEDED
"""
from __future__ import annotations

import asyncio
from typing import Any, AsyncIterator

from ..core.session import create_message, reasoning_block

__all__ = [
    "AUTH",
    "BlockAssembler",
    "CONTEXT_WINDOW_EXCEEDED",
    "EMPTY_RESPONSE",
    "INVALID_REQUEST",
    "LlmAdapter",
    "LlmFailure",
    "MALFORMED_RESPONSE",
    "QUOTA",
    "RATE_LIMIT",
    "REQUEST_ERROR",
    "SERVER",
    "STREAM_CHUNK_KINDS",
    "STREAM_CLOSED",
    "StreamAborted",
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
QUOTA = "QUOTA"
INVALID_REQUEST = "INVALID_REQUEST"
SERVER = "SERVER"
TIMEOUT = "TIMEOUT"
TRANSPORT = "TRANSPORT"
STREAM_CLOSED = "STREAM_CLOSED"
EMPTY_RESPONSE = "EMPTY_RESPONSE"
MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
REQUEST_ERROR = "REQUEST_ERROR"   # mini 教学扩展：非 4xx/5xx 归类的兜底码（上游无此常量）


class StreamChunk(dict):
    """统一流协议：判别键 `type` + payload（对齐上游 StreamChunk，llm/src/types.ts）。

    dict 子类，天然可 JSON 序列化；构造函数参数名保留 kind 以匹配
    STREAM_CHUNK_KINDS 词汇，但落盘键与上游一致为 "type"。

    简化标注（2026-08-17 上游 rc.7 审核）：上游 finish chunk 可携带
    replayState（ReplayEnvelope：{response, blocks?}，响应级 + 按块适配器
    私有元数据，assembler 在 max-tokens 裁剪 tool-call 时同步裁剪 blocks，
    条目数与块数不符则整体丢弃）。mini 未复现 ReplayEnvelope——mini 流为
    同步 stdlib 载体，无重放双半（replay fidelity 的适配器私有元数据无消费
    方），故 finish chunk 不含 replayState 字段。语义影响：无（该字段对
    mini 可见契约为空）。见 AGENTS.md 简化清单。
    """

    def __init__(self, kind: str, **payload: Any):
        if kind not in STREAM_CHUNK_KINDS:
            raise ValueError(f"未知 chunk kind: {kind}")
        super().__init__({"type": kind, **payload})


class StreamAborted(Exception):
    """协作式取消哨兵：适配器流在 abort 事件置位时抛出的中止标记。

    对齐上游"stream 抛 AbortError 中止迭代"语义（agent-loop 逐 await 点
    检查 signal）：agent 的 step 捕获后按回合中止处理——定稿可安全落盘的
    前缀（interruptedBlocks）后以 aborted 闭合。
    不是 asyncio.CancelledError——它只是自定义标记，避免与真实任务取消混淆。
    """


async def _aiter_raced(aiter_: AsyncIterator, abort_event=None):
    """把异步迭代器与 abort 事件竞速，abort 置位即在下次取块前抛 StreamAborted。

    对齐上游 AbortSignal：一经置位即中止迭代（下一次取块判负即抛），
    调用方按回合中止语义处理（定稿前缀后 aborted 闭合）。适配器在自身
    async-with 中消费本迭代器——抛错退出即关闭连接，无遗留线程/资源。
    """
    if abort_event is None:
        async for item in aiter_:
            yield item
        return
    while True:
        get_task = asyncio.ensure_future(aiter_.__anext__())
        abort_task = asyncio.ensure_future(abort_event.wait())
        done, _pending = await asyncio.wait(
            {get_task, abort_task}, return_when=asyncio.FIRST_COMPLETED)
        # abort 粘滞优先：若 get 与 abort 同一批完成（数据送达与 abort.set 撞在
        # 同一轮询批次），也视为取消——上游 AbortSignal 一经置位即停。
        if abort_event.is_set():
            get_task.cancel()
            raise StreamAborted("LLM 流被取消")
        abort_task.cancel()
        try:
            yield get_task.result()
        except StopAsyncIteration:
            return


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

    模型能力：resolve_model_info 返回 {provider, model, input_modalities}；
    缺省 input_modalities 视为 ['text']（对齐上游 resolveModelInfo：未声明
    时假设纯文本）。ACP 富媒体据此判定图片输入支持（supportsAcpImagePrompts
    同语义，见 packages/acp/acp/src/content.ts）。
    """

    provider: str = "base"
    retry_policy: dict | None = None
    model: str | None = None

    async def stream(self, messages: list[dict], tools: list[dict],
                     signal: Any | None = None) -> AsyncIterator[StreamChunk]:
        """async 迭代器：逐 chunk 产出（对齐上游 async stream 迭代器）。

        signal 可选：协作式取消信号（_AbortProxy 形态，含 .aborted/.event）；
        置位后流应在下一次取块前中止（抛 StreamAborted）。迷你适配器可忽略。
        """
        raise NotImplementedError

    def resolve_model_info(self) -> dict:
        """模型能力声明：{provider, model, input_modalities}。

        教学扩展：上游 resolveModelInfo 是 llm 服务方法（按 provider/model
        解析 catalog）；mini 以适配器实例方法承载（无 catalog，简化标注）。
        input_modalities 含 'image' 才宣称支持图片输入。
        """
        return {
            "provider": self.provider,
            "model": self.model,
            "input_modalities": ["text"],
        }


class BlockAssembler:
    """按 index 组装 ContentBlock（上游 assembler.ts 的简化版）。

    block-end 携带组装好的块，所以正常完成路径只收集闭合块与终态
    （usage / finish）；流式 UI 可改为逐片转发 delta。同时按流序追踪
    open 块的增量累积（上游 partials/order 同构），供取消时定稿前缀。

    对齐上游 assembler.ts:136-148：finish.kind == 'max-tokens' 时过滤
    tool-call 块（"cannot be executed safely"，未完成的调用不可执行）。
    """

    def __init__(self):
        self.blocks: list[dict] = []
        self.usage: dict | None = None
        self.finish: dict | None = None
        self._replay_state: dict | None = None
        self._order: list[int] = []         # index 首次出现序（上游 order）
        self._open: dict[int, dict] = {}    # 未闭合块：{blockType, text?}
        self._closed: dict[int, dict] = {}  # index → 已闭合块

    @property
    def replay_state(self) -> dict | None:
        """finish chunk 携带的 ReplayEnvelope（上游 assembler.replayState）。

        mini 流载体无重放双半，finish 恒不带 replayState → 恒 None；保留属性以
        对齐 V2 assertCurrentAssistantStream 的 source.replayState 比对。
        """
        return self._replay_state

    def _mark_seen(self, index: int) -> None:
        if index not in self._open and index not in self._closed:
            self._order.append(index)

    def _ensure_open(self, index: int, default_type: str) -> dict:
        entry = self._open.get(index)
        if entry is None:
            self._mark_seen(index)
            entry = {"blockType": default_type}
            self._open[index] = entry
        return entry

    def push(self, chunk: dict) -> None:
        kind = chunk["type"]
        if kind == "block-start":
            self._mark_seen(chunk["index"])
            self._open[chunk["index"]] = {"blockType": chunk.get("blockType")}
        elif kind == "text-delta":
            entry = self._ensure_open(chunk["index"], "text")
            entry["text"] = entry.get("text", "") + chunk.get("text", "")
        elif kind == "reasoning-delta":
            entry = self._ensure_open(chunk["index"], "reasoning")
            entry["text"] = entry.get("text", "") + chunk.get("text", "")
        elif kind == "block-end":
            self._mark_seen(chunk["index"])
            self._open.pop(chunk["index"], None)
            block = chunk["block"]
            self.blocks.append(block)
            self._closed[chunk["index"]] = block
        elif kind == "usage":
            self.usage = chunk["usage"]
        elif kind == "finish":
            self.finish = chunk["reason"]
            self._replay_state = chunk.get("replayState")

    def message(self, source: dict | None = None) -> dict:
        blocks = self.blocks
        if self.finish is not None and self.finish.get("kind") == "max-tokens":
            # 上游：max-tokens 下模型产出的 tool-call 不可安全执行，丢弃
            blocks = [b for b in blocks if b.get("type") != "tool-call"]
        return create_message("assistant", blocks, source or {"kind": "model"})

    def interrupted_blocks(self) -> list[dict]:
        """被中断的流可安全定稿的前缀（上游 assembler.ts interruptedBlocks）。

        按流序收集 closed/open 的 text/reasoning 块（open 块从累积增量装配）；
        tool-call 一律丢弃——中断先于派发，保留一个未派发的调用需要伪造结果；
        未知类型的 open 块同样丢弃；空白内容块不可见，不保留。
        """
        kept: list[dict] = []
        for index in self._order:
            entry = self._open.get(index)
            if entry is not None:
                block_type = entry.get("blockType")
                if block_type not in ("text", "reasoning"):
                    continue
                text = entry.get("text", "")
                block = (reasoning_block(text) if block_type == "reasoning"
                         else {"type": "text", "text": text})
            else:
                block = self._closed.get(index)
                if block is None or block.get("type") not in ("text", "reasoning"):
                    continue
            if not isinstance(block.get("text"), str) or block["text"].strip() == "":
                continue
            kept.append(block)
        return kept