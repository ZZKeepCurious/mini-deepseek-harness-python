"""第 4 章：LLM 流式适配 —— 统一 StreamChunk 协议 + 官方 SSE 适配器。

对应 dsh 真实源码：packages/llm/llm（协议）+ packages/llm/llm-deepseek（适配器）
+ packages/llm/llm-retry（重试，独立模块 retry.py）。

聚合再导出：族内拆分为 protocol / fake / deepseek 三个模块（另有 retry_policy
与 retry 两个独立模块），本包保持旧模块面（全集再导出）。

显式 __all__：星号导入只导出契约名，避免子模块属性泄漏进命名空间（与
core/session 同规约，详见其 __init__ 说明）。
"""
from .protocol import *  # noqa: F401,F403
from .fake import *  # noqa: F401,F403
from .deepseek import *  # noqa: F401,F403
from .assistant_stream import *  # noqa: F401,F403
from .content import *  # noqa: F401,F403

__all__ = [
    "AssistantStreamAccumulator",
    "AUTH",
    "BlockAssembler",
    "CONTEXT_WINDOW_EXCEEDED",
    "DeepSeekAdapter",
    "EMPTY_RESPONSE",
    "FakeLlmAdapter",
    "LlmAdapter",
    "LlmFailure",
    "RATE_LIMIT",
    "REQUEST_ERROR",
    "SERVER",
    "STREAM_CHUNK_KINDS",
    "STREAM_CLOSED",
    "StreamAborted",
    "StreamChunk",
    "TIMEOUT",
    "TimedStreamChunk",
    "TRANSPORT",
    "UNSUPPORTED_CONTENT",
    "content_has_file",
    "content_has_image",
    "expand_assistant_stream",
    "file_handle_text",
    "project_files_to_text",
    "provider_retry_after_ms",
    "request_id",
    "serialize_messages",
    "validate_record",
]