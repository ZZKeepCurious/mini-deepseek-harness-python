"""消息模型：{id, role, content: ContentBlock[], source} 与 ContentBlock 构造。

上游对照：packages/llm/llm/src/message.ts + types.ts（已核实）。mini 保留在会话域
而非 llm 包：core/session 为 L0 不允许依赖 llm（简化标注，见 WRITING-STYLE §4.1）。
"""
from __future__ import annotations

import uuid

__all__ = [
    "create_message",
    "image_block",
    "reasoning_block",
    "text_block",
    "tool_call_block",
    "tool_result_block",
]


def create_message(role: str, content: list, source: dict | None = None) -> dict:
    """构造带稳定 id 的消息：{id, role, content: ContentBlock[], source}。

    消息在落日志时由 Session.append 冻结；此处保持普通 dict/list，
    以便适配器序列化与 wire 传输（冻结是日志边界的职责）。
    """
    return {
        "id": str(uuid.uuid4()),
        "role": role,
        "content": list(content),
        "source": source or {"kind": role},
    }


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def reasoning_block(text: str) -> dict:
    return {"type": "reasoning", "text": text}


def image_block(attachment: dict) -> dict:
    """image 块：引用不可变附件（ImageAttachmentRef 形状）。

    对齐上游 ImageBlock：{type:'image', attachment: ImageAttachmentRef}
    （llm/llm/src/types.ts）。attachment 是持久化引用（sha256 内容寻址），
    由 attachment 服务的 save_images 产出；本构造不持有原始字节。
    """
    return {"type": "image", "attachment": attachment}


def tool_call_block(call_id: str, name: str, arguments: str) -> dict:
    """tool-call 块：arguments 是模型产出的原始 JSON 字符串（不解析）。"""
    return {"type": "tool-call", "id": call_id, "name": name, "arguments": arguments}


def tool_result_block(tool_call_id: str, content: list, is_error: bool = False) -> dict:
    block = {"type": "tool-result", "toolCallId": tool_call_id, "content": list(content)}
    if is_error:
        block["isError"] = True
    return block