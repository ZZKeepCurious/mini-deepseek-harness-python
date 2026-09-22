"""context 组：请求上下文插件（对齐 packages/context）。

成员：`time_context`（当前时间/浏览器时区/步间耗时）、`tmux_context`（tmux 方位）、
`file_reference` + `file_reference_local`（`@file` 提及语法与本地补全）、
`session_reference`（其它会话的有界只读快照）、`agent_instructions`（AGENTS.md/CLAUDE.md
工作区指令）。除 agent_instructions 外均 opt-in；注入内容为 user 角色消息，进入会话历史、
可持久化/重放/压缩。
"""

from .agent_instructions import (
    apply_agent_instructions,
    install_agent_instructions,
    load_baseline_instruction_set,
    render_agent_instruction_set,
    resolve_config as resolve_instruction_config,
)
from .file_reference import FILE_REFERENCE_PROMPT, FileReferenceService, active_at_token, format_file_mention
from .file_reference_local import LocalFileReferenceService, WorkspaceFileSearch, install_file_reference_local
from .session_reference import (
    SessionReferenceResolver,
    decode_session_reference_uri,
    encode_session_reference_uri,
    format_session_reference_mention,
    install_session_reference,
    parse_session_reference_text,
)
from .time_context import apply_time_context, install_time_context
from .tmux_context import apply_tmux_context, install_tmux_context

__all__ = [
    "FILE_REFERENCE_PROMPT",
    "FileReferenceService",
    "LocalFileReferenceService",
    "SessionReferenceResolver",
    "WorkspaceFileSearch",
    "active_at_token",
    "apply_agent_instructions",
    "apply_time_context",
    "apply_tmux_context",
    "decode_session_reference_uri",
    "encode_session_reference_uri",
    "format_file_mention",
    "format_session_reference_mention",
    "install_agent_instructions",
    "install_file_reference_local",
    "install_session_reference",
    "install_time_context",
    "install_tmux_context",
    "load_baseline_instruction_set",
    "parse_session_reference_text",
    "render_agent_instruction_set",
    "resolve_instruction_config",
]
