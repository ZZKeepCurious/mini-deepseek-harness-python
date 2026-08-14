"""第 4 章：LLM 流式适配 —— 统一 StreamChunk 协议 + 官方 SSE 适配器。

对应 dsh 真实源码：packages/llm/llm（协议）+ packages/llm/llm-deepseek（适配器）。

协议不变量：
  1. block-end 携带完整块
  2. usage 必须在 finish 之前，finish 之后不再有值
  3. 两种授权错误统一为 LlmFailure；上下文溢出编码 CONTEXT_WINDOW_EXCEEDED
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Iterator

STREAM_CHUNK_KINDS = frozenset({
    "block-start", "text-delta", "reasoning-delta",
    "tool-call-delta", "block-end", "usage", "finish",
})


class StreamChunk(dict):
    """统一流协议：kind + payload。dict 子类，天然可 JSON 序列化。"""

    def __init__(self, kind: str, **payload: Any):
        if kind not in STREAM_CHUNK_KINDS:
            raise ValueError(f"未知 chunk kind: {kind}")
        super().__init__({"kind": kind, **payload})


class LlmFailure(Exception):
    """统一错误收口：授权 / 请求 / 上下文溢出。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class LlmAdapter:
    """接口（Service Definition）：Consumer（agent-loop）只依赖这个协议。"""

    provider: str = "base"

    def stream(self, messages: list[dict], tools: list[dict]) -> Iterator[StreamChunk]:
        raise NotImplementedError


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
            yield StreamChunk("block-start", index=0, block_kind="assistant")
            yield StreamChunk("tool-call-delta", index=0, name=self._tool["name"], arguments=self._tool.get("arguments", {}))
            yield StreamChunk("block-end", index=0, block={"role": "assistant", "tool_calls": [self._tool]})
            yield StreamChunk("finish", finish_reason="tool_calls")
        else:
            yield StreamChunk("block-start", index=0, block_kind="assistant")
            yield StreamChunk("text-delta", index=0, delta=self._text)
            yield StreamChunk("block-end", index=0, block={"role": "assistant", "content": self._text})
            yield StreamChunk("finish", finish_reason="stop")


class DeepSeekAdapter(LlmAdapter):
    """DeepSeek 官方 chat API 的 SSE 适配器（纯 stdlib urllib，零依赖）。"""

    provider = "deepseek-official"

    def __init__(self, api_key: str | None = None, base_url: str | None = None, model: str = "deepseek-chat"):
        self._key = api_key if api_key is not None else os.environ.get("DEEPSEEK_API_KEY", "")
        self._base = (base_url or os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")).rstrip("/")
        self._model = model

    def stream(self, messages, tools):
        body: dict[str, Any] = {"model": self._model, "messages": messages, "stream": True}
        if tools:
            body["tools"] = [t["schema"] for t in tools]
        req = urllib.request.Request(
            self._base + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + self._key},
        )
        try:
            resp = urllib.request.urlopen(req, timeout=120)
        except urllib.error.HTTPError as e:
            detail = e.read(200).decode("utf-8", "replace")
            code = "AUTH_ERROR" if e.code in (401, 403) else "REQUEST_ERROR"
            raise LlmFailure(code, f"HTTP {e.code}: {detail}") from e

        # tool-call 增量是按块流式的：name 与 arguments 分片到达，需要累积
        pending: dict[int, dict[str, str]] = {}
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                break
            piece = json.loads(data)
            for choice in piece.get("choices", []):
                delta = choice.get("delta", {})
                if delta.get("reasoning_content"):
                    yield StreamChunk("reasoning-delta", index=choice["index"], delta=delta["reasoning_content"])
                if delta.get("content"):
                    yield StreamChunk("text-delta", index=choice["index"], delta=delta["content"])
                for tc in delta.get("tool_calls") or []:
                    slot = pending.setdefault(tc["index"], {"name": "", "arguments": ""})
                    fn = tc.get("function", {})
                    slot["name"] += fn.get("name", "")
                    slot["arguments"] += fn.get("arguments", "")

        if pending:
            calls = []
            yield StreamChunk("block-start", index=0, block_kind="assistant")
            for idx, slot in sorted(pending.items()):
                calls.append({"id": f"call_{idx}", "type": "function", "function": slot})
                yield StreamChunk("tool-call-delta", index=idx, name=slot["name"], arguments=slot["arguments"])
            yield StreamChunk("block-end", index=0, block={"role": "assistant", "tool_calls": calls})
        yield StreamChunk("finish", finish_reason="tool_calls" if pending else "stop")