"""file-reference：`@file` 提及语法与发现 seam（对齐 packages/context/file-reference）。

供宿主 UI 共用：`active_at_token`/`format_file_mention` 语法纯函数 + `FileReferenceService`
服务定义（`ctx.fileReferences`）。文件内容留在面向模型的 `read` 工具之后——本 seam 只产路径候选。
"""
from __future__ import annotations

import re

from ..core.scope import Context, Service

__all__ = [
    "FILE_REFERENCE_PROMPT",
    "FileReferenceService",
    "active_at_token",
    "format_file_mention",
]

#: 面向模型的路径引用指引（index.ts:17 逐字）。
FILE_REFERENCE_PROMPT = (
    "Tokens prefixed with @ are workspace paths the user explicitly referenced, "
    "relative to the workspace root. A trailing slash marks a directory: list it when "
    "its contents matter. Anything else is a file: use the read tool when its contents "
    "are needed, and do not claim to have inspected it before reading. @\"...\" quotes "
    "a path containing spaces.")

_QUOTED_TOKEN = re.compile(r'(?:^|\s)(@"([^"]*))$')
_PLAIN_TOKEN = re.compile(r'(?:^|\s)(@([^\s]*))$')
_UNSAFE_MENTION = re.compile('[\u0000-\u001f\u007f-\u009f"]')
_WHITESPACE = re.compile(r"\s")


def active_at_token(line: str, cursor_col: int) -> dict | None:
    """在光标处提取 `@path` 或 `@"path with spaces` 词元（grammar.ts:26-35）。

    其它词元内部的 `@`（如邮箱）不是补全触发点。
    """
    before = line[:cursor_col]
    quoted = _QUOTED_TOKEN.search(before)
    if quoted is not None and quoted.group(1) is not None and quoted.group(2) is not None:
        return {"prefix": quoted.group(1), "query": quoted.group(2), "quoted": True}
    plain = _PLAIN_TOKEN.search(before)
    if plain is None or plain.group(1) is None or plain.group(2) is None:
        return None
    return {"prefix": plain.group(1), "query": plain.group(2), "quoted": False}


def format_file_mention(candidate: dict, preserve_quote: bool) -> str | None:
    """把一个选中的路径格式化为提示词文本（grammar.ts:45-55）。

    含空白用 `@"path"`；目录保留尾随斜杠并让引号保持打开以便继续下钻。
    """
    path = f"{candidate['path']}/" if candidate.get("kind") == "directory" else candidate["path"]
    if _UNSAFE_MENTION.search(path) is not None:
        return None
    quoted = preserve_quote or _WHITESPACE.search(path) is not None
    if not quoted:
        return f"@{path}"
    if candidate.get("kind") == "directory":
        return f'@"{path}'
    return f'@"{path}"'


class FileReferenceService(Service):
    """可取消的文件引用发现能力（index.ts:26-43）。"""

    provide = "fileReferences"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "fileReferences")

    def list(self, agent, query: str, signal=None) -> list:
        """列出某个 agent 工作目录下的文件/目录候选（子类实现）。"""
        raise NotImplementedError
