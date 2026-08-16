"""确定性假模型：无需 API key 即可跑通完整回合（测试与联调专用）。

教学扩展：上游无对应适配器（真实 SDK 测试基建在 packages/ 之外）；mini 以
内置 FakeLlmAdapter 保留，语义对齐 StreamChunk 协议。
"""
from __future__ import annotations

import json

from .protocol import LlmAdapter, StreamChunk

__all__ = ["FakeLlmAdapter"]


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