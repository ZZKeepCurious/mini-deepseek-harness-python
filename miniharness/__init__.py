"""MiniHarness：用纯 Python（stdlib only）从 0 到 1 复现 DeepSeek Harness 的核心契约。

随教程逐步构建：dsh-from-scratch/01~06 章。
"""

from .session import (
    Session,
    deep_freeze,
    derive_messages,
    is_json_safe,
    repair_interrupted_turn,
    turn_balance,
)
from .bus import Context, PluginManager
from .tools import Tool, ToolExec, ToolRegistry, ToolResult, run_pipeline, validate_schema
from .llm import DeepSeekAdapter, FakeLlmAdapter, LlmAdapter, LlmFailure, StreamChunk
from .loop import AgentLoop
from .persistence import (
    JsonlPersistence,
    SessionPersistence,
    SqlitePersistence,
    balanced_after_replay,
    load_events_checked,
    repair_and_replay,
)
from .boot import apply_patch, boot
from . import seams

__version__ = "0.1.0"

__all__ = [
    "AgentLoop",
    "Context",
    "DeepSeekAdapter",
    "FakeLlmAdapter",
    "JsonlPersistence",
    "LlmAdapter",
    "LlmFailure",
    "PluginManager",
    "Session",
    "SessionPersistence",
    "SqlitePersistence",
    "StreamChunk",
    "Tool",
    "ToolExec",
    "ToolRegistry",
    "ToolResult",
    "apply_patch",
    "balanced_after_replay",
    "boot",
    "deep_freeze",
    "derive_messages",
    "is_json_safe",
    "load_events_checked",
    "repair_and_replay",
    "repair_interrupted_turn",
    "run_pipeline",
    "seams",
    "turn_balance",
    "validate_schema",
]