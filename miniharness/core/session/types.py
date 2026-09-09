"""事件类型词汇表：已知类型全集、surface 事件、崩溃恢复错误码。

上游对照：packages/core/session/src/known-event-types.ts（事件类型全集）+ types.ts
（SurfaceEventType）+ repair.ts（TOOL_NOT_STARTED / TOOL_OUTCOME_UNKNOWN）。

V3（上游 dsh-v0.1.5-alpha.1，SESSION_FORMAT_VERSION 2→3）：system prompt 成为
surface node 0（新 surface 事件 `system/message`，第 4 种 surface 类型）；
`tool/code-dispatch{,-start}` 持久词汇改名 `tool/ptc-dispatch{,-start}`；新增
feedback/message-put|delete 反馈域事件（上游 feedback/message-feedback 包）。
"""
from __future__ import annotations

__all__ = [
    "KNOWN_TYPES",
    "SESSION_FORMAT_VERSION",
    "SURFACE_TYPES",
    "TOOL_NOT_STARTED",
    "TOOL_OUTCOME_UNKNOWN",
]

SESSION_FORMAT_VERSION = 3

KNOWN_TYPES = frozenset({
    "turn/start", "turn/end", "step/start", "step/end",
    "system/message",
    "user/message", "assistant/message", "assistant/attempt",
    "tool/call", "tool/result",
    # 请求信封（上游 agent-loop/src/agent.ts SessionEventMap，log-only 非
    # surface）：request/header 存 canonical 快照 {header:{config,
    # adapterDefaults?, tools?}, reason}——V3 起 header 不再携带 system（系统
    # 提示词是派生历史 = surface node 0 的 system/message 事件）；
    # request/context {provider, model, contextWindow?, systemPromptUpdate?}
    # 在任一字段变化时追加
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
    # tool-result 裁剪影子计价（上游 compaction-tool-result-pruner：
    # ``compaction/prune`` 是 log-only 非 surface 的 shadow-price 事件，
    # 紧邻其后带 replace surfaceOp 的 tool/result 替换）
    "compaction/prune",
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
    # 沙箱模式切换（上游 sandbox/sandbox-policy/src/session-mode.ts
    # SessionEventMap：{mode, source?: 'delegation'}，log-only 非 surface、
    # 整值替换最后一条胜出——effective = fold(events) ?? 部署默认）
    "sandbox/mode",
    # PTC 派发审计（上游 core/tools/src/ptc.ts SessionEventMap，log-only 非
    # surface；V3 由 tool/code-dispatch{,-start} 改名，payload 不变；mini 不
    # 产出（无 PTC 运行时），仅为读侧词汇一致性登记）
    "tool/ptc-dispatch", "tool/ptc-dispatch-start",
    # 消息反馈（上游 feedback/message-feedback 包 SessionEventMap，log-only 非
    # surface：put = {sessionId, item:{messageId, rating, version, createdAt,
    # updatedAt, note?}}；delete = {sessionId, messageId}；mini 不产出，登记可读）
    "feedback/message-put", "feedback/message-delete",
    # Agent Teams 实验（上游 experimental/agent-team/src/journal.ts
    # SessionEventMap，四个事件 log-only 非 surface、version 2、存于
    # Team Lead 会话日志）：team/member 全量投标 {member}；
    # team/task 全量投标 {task}；team/message/queued {message}；
    # team/message/delivered {messageId, targetId}
    "team/member", "team/task", "team/message/queued", "team/message/delivered",
})

# 只有这四种事件产生模型消息，可带 surfaceOp（上游 types.ts SurfaceEventType；
# V3 新增 system/message：系统提示词是 surface node 0 的派生历史）
SURFACE_TYPES = frozenset({
    "system/message", "user/message", "assistant/message", "tool/result",
})

# 崩溃恢复码（上游 session/src/repair.ts）
TOOL_NOT_STARTED = "TOOL_NOT_STARTED"
TOOL_OUTCOME_UNKNOWN = "TOOL_OUTCOME_UNKNOWN"