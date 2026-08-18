"""MiniHarness：用 Python（stdlib 优先，SSE 传输层用 httpx）从 0 到 1 复现 DeepSeek Harness 的核心契约。

随教程逐步构建：dsh-from-scratch/01~06 章。

包布局按上游家族镜像（docs/architecture.md）：core/（会话/作用域/工具/agent 循环）、
llm/（协议与适配器）、boot/（组合）、cli/（入口）、protocol/（acp/sdk/hooks）、
seams/（沙箱/凭据/子 agent）、preset/、extensions/、interaction/、client/。
顶层再导出是教学面（契约层）；深路径见各家族 __init__。
"""

from .core.session import (
    SESSION_FORMAT_VERSION,
    TOOL_NOT_STARTED,
    TOOL_OUTCOME_UNKNOWN,
    Session,
    create_message,
    derive_messages,
    repair_interrupted_turn,
    reasoning_block,
    text_block,
    tool_call_block,
    tool_result_block,
    turn_balance,
)
from .core.scope import Context, PluginManager
from .core.tools import (
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
from .core.agent_loop.tool_calls import (
    DEFAULT_MAX_PARALLEL_TOOL_CALLS,
    TOOL_ABORTED_BEFORE_DISPATCH,
    ParallelBarrier,
    schedule_tool_calls,
)
from .llm import DeepSeekAdapter, FakeLlmAdapter, LlmAdapter, LlmFailure, StreamChunk
from .llm.retry import apply_retry_planner, recover_llm_failure
from .core.agent_loop.agent import AgentLoop
from .llm.retry_policy import DEFAULT_RETRYABLE_CODES, resolve_retry_policy
from .core.session.persistence import (
    JsonlPersistence,
    SessionPersistence,
    SqlitePersistence,
)
from .boot import apply_patch, boot
from .cli.headless import run_headless, summarize
from .preset.presets import Preset, PresetRoster, builtin_roster
from .client.trajectory import TrajectoryNode, TrajectorySnapshot, fold_events_json, fold_trajectory
from .extensions.dynamic import DynamicPlugin, DynamicPluginRegistry
from .interaction.approval import (
    APPROVAL_OUTCOMES,
    APPROVAL_POLICIES,
    ApprovalService,
    effective_approval_policy,
    has_open_turn,
    set_approval_policy,
)
from .protocol.sdk import (
    JsonRpcLineTransport,
    JsonRpcResponseError,
    PendingRequest,
    SdkRuntime,
)
from .protocol.acp import (
    AcpRequestError,
    AcpServer,
    acp_prompt_to_text,
    invalid_params,
    prompt_has_unsupported_content,
    turn_end_to_stop_reason,
)
from .protocol.hooks import (
    ClaudeCodeBridge,
    matches_matcher,
    matcher_diagnostic,
    merge_hook_outputs,
    parse_claude_code_config,
    parse_hook_output,
    run_hook,
    substitute_command,
)
from .seams.subagent.providers import AcpSubAgentProvider, ForkSubAgentProvider, SdkSubAgentProvider

__version__ = "0.2.0"

__all__ = [
    "AgentLoop",
    "Context",
    "DeepSeekAdapter",
    "FakeLlmAdapter",
    "JsonlPersistence",
    "LlmAdapter",
    "LlmFailure",
    "PluginManager",
    "SESSION_FORMAT_VERSION",
    "Session",
    "SessionPersistence",
    "SqlitePersistence",
    "StreamChunk",
    "TOOL_NOT_STARTED",
    "TOOL_OUTCOME_UNKNOWN",
    "Tool",
    "ToolRegistry",
    "apply_patch",
    "boot",
    "create_message",
    "derive_messages",
    "reasoning_block",
    "repair_interrupted_turn",
    "run_headless",
    "text_block",
    "tool_call_block",
    "tool_result_block",
    "turn_balance",
]