"""事件类型词汇表：已知类型全集、surface 事件、崩溃恢复错误码。

上游对照：packages/core/session/src/known-event-types.ts（事件类型全集）+ types.ts
（SurfaceEventType）+ repair.ts（TOOL_NOT_STARTED / TOOL_OUTCOME_UNKNOWN）。
"""
from __future__ import annotations

__all__ = [
    "KNOWN_TYPES",
    "SESSION_FORMAT_VERSION",
    "SURFACE_TYPES",
    "TOOL_NOT_STARTED",
    "TOOL_OUTCOME_UNKNOWN",
]

SESSION_FORMAT_VERSION = 0

KNOWN_TYPES = frozenset({
    "turn/start", "turn/end", "step/start", "step/end",
    "user/message", "assistant/message", "assistant/chunk",
    "tool/call", "tool/result",
    # 请求信封（上游 agent-loop/src/agent.ts SessionEventMap，log-only 非
    # surface）：request/header 存 canonical 快照 {header:{config,
    # adapterDefaults?, system?, tools?}, reason}，attempt/重试不重复落；
    # request/context {provider, model, contextWindow?} 在 provider/model/
    # contextWindow 变化时追加
    "request/header", "request/context", "session/end-seed",
    # Inbox 变更（上游 agent/src/inbox.ts SessionEventMap，log-only 非 surface：
    # 每次入队/认领/清除的 splice 形状 {target, start, removedCount?, inserted,
    # outcome?}，冷恢复重放重建 live 状态；认领不写 outcome，丢弃式删除写
    # 'canceled'）
    "agent/inbox/spliced",
    # 审批审计（上游 user-approval/src/index.ts SessionEventMap，log-only 非 surface）
    "approval/asked", "approval/decided", "approval/policy",
    # 钩子审计（上游 hook-protocol/src/types.ts SessionEventMap，log-only 非 surface）
    "hook/invoked", "hook/result",
    # LLM 重试审计（上游 llm-retry/src/index.ts SessionEventMap，log-only 非 surface）
    "llm/retry", "llm/retry-started",
    # 上下文压缩（上游 compaction/compaction/src/types.ts SessionEventMap，
    # 三个事件 log-only 非 surface；surface 变更是随后带 replace surfaceOp 的
    # user/message 检查点）
    "compaction/start", "compaction/summary", "compaction/end",
    # 计划模式（上游 plan/plan-mode/src/index.ts SessionEventMap，
    # log-only 非 surface、整值替换：{active: boolean}，最后一条胜出）
    "plan/mode",
    # 斜杠命令生命周期配对（上游 interaction/commands/src/types.ts
    # SessionEventMap：command/run {commandId, name, args?, source} +
    # command/done {commandId, kind, text?, sourceEventSeq?}，log-only 非 surface）
    "command/run", "command/done",
    # 目标域变更（上游 goal/goal/src/domain.ts SessionEventMap：
    # goal/change 全快照或 clear 墓碑，version 1，log-only 非 surface）
    "goal/change",
    # 可继续子代理描述符（上游 subagent/subagent/src/descriptor.ts
    # SessionEventMap：model-hidden、log-only 非 surface、首条权威，
    # version 2；冷恢复据此重建子会话组合）
    "subagent/descriptor",
})

# 只有这三种事件产生模型消息，可带 surfaceOp（上游 types.ts SurfaceEventType）
SURFACE_TYPES = frozenset({"user/message", "assistant/message", "tool/result"})

# 崩溃恢复码（上游 session/src/repair.ts）
TOOL_NOT_STARTED = "TOOL_NOT_STARTED"
TOOL_OUTCOME_UNKNOWN = "TOOL_OUTCOME_UNKNOWN"