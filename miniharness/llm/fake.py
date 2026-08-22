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

    def __init__(self, tool_call: dict | None = None, final_text: str = "任务完成。",
                 image: dict | None = None, reasoning_effort: str | None = None):
        self._tool = tool_call
        self._text = final_text
        # 教学扩展：附带的 image 块（{attachment: ImageAttachmentRef 形状}），
        # 使 assistant 图片输出路径（ACP readImage → base64 内联）可测。
        self._image = image
        self._reasoning_effort = reasoning_effort
        self.calls = 0

    def resolve_model_info(self) -> dict:
        """假模型声明支持文本与图片输入（教学扩展）。

        上游无对应适配器；mini 以声明能力使 ACP 富媒体在 fake 路径可测
        （支持图片输入才宣称 promptCapabilities.image）。"""
        return {
            "provider": self.provider,
            "model": self.model,
            "input_modalities": ["text", "image"],
        }

    @property
    def reasoning_effort(self) -> str | None:
        return self._reasoning_effort

    async def stream(self, messages, tools, signal=None):
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
            if self._image is not None:
                yield StreamChunk("block-start", index=1, blockType="image")
                yield StreamChunk("block-end", index=1, block={
                    "type": "image", "attachment": self._image,
                })
            yield StreamChunk("finish", reason={"kind": "stop"})