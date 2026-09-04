"""请求侧 file 块投影（上游 packages/llm/llm/src/content.ts:137-201）。

file 是第六类 ContentBlock（alpha.1）：durable verbatim 引用经 attachment 服务
存储；**任何 provider 都不原生接收 file 块**——请求组装无条件把 file（含嵌套
tool-result 内的）投影为确定性 handle 文本（fileHandleText），模型按需用文件
工具读取该路径。本模块是该投影的 mini 等价物：纯函数、无 IO；路径解析由调用
方注入（agent 请求组装处经 attachments 服务 file_host_path）。
"""
from __future__ import annotations

import json

__all__ = [
    "content_has_file",
    "file_handle_text",
    "project_files_to_text",
]


def _quoted(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def content_has_file(content: list) -> bool:
    """内容块是否含 file 块（含嵌套 tool-result 递归，上游 contentHasFile）。"""
    return any(
        block.get("type") == "file"
        or (block.get("type") == "tool-result" and content_has_file(block.get("content") or []))
        for block in content or []
    )


def file_handle_text(ref: dict, readonly_path: str | None) -> str:
    """durable file 引用的模型可见 handle（上游 fileHandleText 逐字文案）：

    命名文件、字节数与摘要前 8 位，并给出按需读取的路径指令——这是 provider
    能收到的唯一 file 表示。
    """
    attachment_id = str(ref.get("attachmentId") or "")
    digest = attachment_id[len("sha256:"):len("sha256:") + 8]
    identity = (f"File {_quoted(str(ref.get('name') or ''))} "
                f"({ref.get('bytes', 0)} bytes, sha256:{digest})")
    if readonly_path is None:
        return (f"[{identity} was uploaded, but the current execution environment cannot "
                "access a readable path. Report that limitation if its contents are needed; "
                "do not claim to have read it.]")
    return (f"[{identity}: verbatim read-only copy saved at {_quoted(readonly_path)}. "
            "Read that path with your file tools when its contents are needed; copy it to a "
            "writable location before modifying it. When delegating file work, include this "
            "saved path in the delegation prompt; only subagents sharing this execution "
            "environment can read it.]")


def _replace_files_with_handles(blocks: list, resolve_path) -> list:
    next_blocks = None
    for index, block in enumerate(blocks or []):
        if block.get("type") == "file":
            if next_blocks is None:
                next_blocks = list(blocks[:index])
            next_blocks.append({
                "type": "text",
                "text": file_handle_text(block.get("attachment") or {}, resolve_path(block.get("attachment") or {})),
            })
            continue
        if block.get("type") == "tool-result":
            content = _replace_files_with_handles(block.get("content") or [], resolve_path)
            if content is not (block.get("content") or []):
                if next_blocks is None:
                    next_blocks = list(blocks[:index])
                replaced = dict(block)
                replaced["content"] = content
                next_blocks.append(replaced)
                continue
        if next_blocks is not None:
            next_blocks.append(block)
    return next_blocks if next_blocks is not None else list(blocks or [])


def project_files_to_text(messages: list[dict], resolve_path) -> list[dict]:
    """把全部 file 历史（含嵌套 tool-result）投影为确定性 handle 文本
    （上游 projectFilesToText：无条件投影，file 永不原生 dispatch）。

    @param messages - 完整请求历史。
    @param resolve_path - (ref) → 执行世界内只读副本路径或 None。
    @returns 无 file 的原列表（未命中时逐字返回原对象）；否则浅拷贝替换消息。
    """
    if not any(content_has_file(message.get("content") or []) for message in messages or []):
        return messages
    projected: list[dict] = []
    for message in messages:
        content = _replace_files_with_handles(message.get("content") or [], resolve_path)
        if content is not (message.get("content") or []):
            replaced = dict(message)
            replaced["content"] = content
            projected.append(replaced)
        else:
            projected.append(message)
    return projected
