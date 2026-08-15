"""MiniHarness：用纯 Python（stdlib only）从 0 到 1 复现 DeepSeek Harness 的核心契约。

随教程逐步构建：dsh-from-scratch/01~06 章。
"""

from .session import (
    SESSION_FORMAT_VERSION,
    TOOL_NOT_STARTED,
    TOOL_OUTCOME_UNKNOWN,
    Session,
    create_message,
    deep_freeze,
    derive_messages,
    is_json_safe,
    repair_interrupted_turn,
    reasoning_block,
    text_block,
    tool_call_block,
    tool_result_block,
    turn_balance,
)
from .bus import Context, PluginManager
from .tools import (
    Tool,
    ToolExec,
    ToolRegistry,
    ToolResult,
    execution_mode,
    pipeline_async_body,
    pipeline_policy_async,
    run_pipeline,
    run_pipeline_async,
    validate_schema,
)
from .scheduler import (
    DEFAULT_MAX_PARALLEL_TOOL_CALLS,
    TOOL_ABORTED_BEFORE_DISPATCH,
    ParallelBarrier,
    schedule_tool_calls,
)
from .llm import DeepSeekAdapter, FakeLlmAdapter, LlmAdapter, LlmFailure, StreamChunk
from .llm_retry import apply_retry_planner, recover_llm_failure
from .loop import AgentLoop
from .retry_policy import DEFAULT_RETRYABLE_CODES, resolve_retry_policy
from .persistence import (
    JsonlPersistence,
    SessionPersistence,
    SqlitePersistence,
    balanced_after_replay,
    load_events_checked,
    repair_and_replay,
)
from .boot import apply_patch, boot
from .headless import run_headless, summarize
from .presets import Preset, PresetRoster, builtin_roster
from .trajectory import TrajectoryNode, TrajectorySnapshot, fold_events_json, fold_trajectory
from .dynamic import DynamicPlugin, DynamicPluginRegistry
from .approval import (
    APPROVAL_OUTCOMES,
    APPROVAL_POLICIES,
    ApprovalService,
    effective_approval_policy,
    has_open_turn,
    set_approval_policy,
)
from .sdk_protocol import (
    JsonRpcLineTransport,
    JsonRpcResponseError,
    PendingRequest,
    SdkRuntime,
)
from .acp import (
    AcpRequestError,
    AcpServer,
    acp_prompt_to_text,
    invalid_params,
    prompt_has_unsupported_content,
    turn_end_to_stop_reason,
)
from .hooks import (
    ClaudeCodeBridge,
    matches_matcher,
    matcher_diagnostic,
    merge_hook_outputs,
    parse_claude_code_config,
    parse_hook_output,
    run_hook,
    substitute_command,
)
from . import credentials_local, sandbox_local, seams, subagent_providers

__version__ = "0.2.0"

__all__ = [
    "APPROVAL_OUTCOMES",
    "APPROVAL_POLICIES",
    "AcpRequestError",
    "AcpServer",
    "AgentLoop",
    "ApprovalService",
    "ClaudeCodeBridge",
    "Context",
    "DEFAULT_MAX_PARALLEL_TOOL_CALLS",
    "DEFAULT_RETRYABLE_CODES",
    "DeepSeekAdapter",
    "DynamicPlugin",
    "DynamicPluginRegistry",
    "FakeLlmAdapter",
    "JsonlPersistence",
    "JsonRpcLineTransport",
    "JsonRpcResponseError",
    "LlmAdapter",
    "LlmFailure",
    "PendingRequest",
    "ParallelBarrier",
    "PluginManager",
    "Preset",
    "PresetRoster",
    "SESSION_FORMAT_VERSION",
    "Session",
    "SessionPersistence",
    "SdkRuntime",
    "SqlitePersistence",
    "StreamChunk",
    "TOOL_ABORTED_BEFORE_DISPATCH",
    "TOOL_NOT_STARTED",
    "TOOL_OUTCOME_UNKNOWN",
    "Tool",
    "ToolExec",
    "ToolRegistry",
    "ToolResult",
    "TrajectoryNode",
    "TrajectorySnapshot",
    "apply_patch",
    "apply_retry_planner",
    "acp_prompt_to_text",
    "balanced_after_replay",
    "boot",
    "builtin_roster",
    "create_message",
    "credentials_local",
    "deep_freeze",
    "derive_messages",
    "effective_approval_policy",
    "execution_mode",
    "fold_events_json",
    "fold_trajectory",
    "has_open_turn",
    "invalid_params",
    "is_json_safe",
    "load_events_checked",
    "pipeline_async_body",
    "pipeline_policy_async",
    "prompt_has_unsupported_content",
    "reasoning_block",
    "recover_llm_failure",
    "repair_and_replay",
    "repair_interrupted_turn",
    "resolve_retry_policy",
    "run_headless",
    "run_pipeline",
    "run_pipeline_async",
    "sandbox_local",
    "schedule_tool_calls",
    "seams",
    "set_approval_policy",
    "subagent_providers",
    "summarize",
    "text_block",
    "tool_call_block",
    "tool_result_block",
    "turn_balance",
    "turn_end_to_stop_reason",
    "validate_schema",
]