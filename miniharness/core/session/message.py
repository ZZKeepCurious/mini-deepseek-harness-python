"""消息模型：{id, role, content: ContentBlock[], source} 与 ContentBlock 构造。

上游对照：packages/llm/llm/src/message.ts + types.ts（已核实）。mini 保留在会话域
而非 llm 包：core/session 为 L0 不允许依赖 llm（简化标注，见 WRITING-STYLE §4.1）。
"""
from __future__ import annotations

import uuid

__all__ = [
    "create_message",
    "developer_message",
    "file_block",
    "image_block",
    "reasoning_block",
    "text_block",
    "tool_addition_block",
    "tool_call_block",
    "tool_removal_block",
    "tool_result_message",
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


def tool_result_message(tool_call_id: str, content: list, is_error: bool = False) -> dict:
    """构造 V4 tool/result 消息：role `'tool'` + 平铺 content + 顶层字段。

    对齐上游 ToolResultMessage（llm/llm/src/message.ts）：role `'tool'`、
    顶层 `toolCallId`、可选 `isError`，content 是直接的 ContentBlock[]（V4 前
    是 user 角色包裹一个 `tool-result` 块）。source 由调用方补齐
    `{kind:'tool', callId}`（与顶层 toolCallId 一致）。
    """
    message = create_message("tool", content, {"kind": "tool", "callId": tool_call_id})
    message["toolCallId"] = tool_call_id
    if is_error:
        message["isError"] = True
    return message


def developer_message(content: list, source: dict | None = None) -> dict:
    """构造 developer 角色消息（上游 DeveloperMessage，V4 surface 事件载荷）。"""
    return create_message("developer", content, source or {"kind": "tool-registry"})


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


def file_block(attachment: dict) -> dict:
    """file 块：verbatim 文件引用（alpha.1 第六类 ContentBlock）。

    对齐上游 FileBlock：{type:'file', attachment: FileAttachmentRef}
    （llm/llm/src/types.ts）。FileAttachmentRef = {attachmentId（原样字节
    sha256）, name, bytes}；请求组装经 project_files_to_text 无条件投影为
    handle 文本——file 永不原生 dispatch（llm/content.ts:137-201）。
    """
    return {"type": "file", "attachment": attachment}


def tool_call_block(call_id: str, name: str, arguments: str) -> dict:
    """tool-call 块：arguments 是模型产出的原始 JSON 字符串（不解析）。"""
    return {"type": "tool-call", "id": call_id, "name": name, "arguments": arguments}


def tool_addition_block(tool_name: str) -> dict:
    """V4 工具新增块（上游 ToolAdditionBlock）：{type:'tool-addition', toolName}。

    只出现在 developer/message；`tool` 内联定义被禁止（定义由 headerSeq
    指向的 request/header 承载）。
    """
    return {"type": "tool-addition", "toolName": tool_name}


def tool_removal_block(tool_name: str) -> dict:
    """V4 工具移除块（上游 ToolRemovalBlock）：{type:'tool-removal', toolName}。"""
    return {"type": "tool-removal", "toolName": tool_name}