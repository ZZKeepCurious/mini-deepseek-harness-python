"""released 事件词表与 payload 成员处置表（上游 session-format-v0-to-v1/src/dispositions.ts
逐字转录 + session-format-v1-to-v2/src/dispositions.ts 的 v2 修订）。

结构：`{required, optional, opaque}`——required/optional 为顶层成员闭集；
opaque 成员（`tool/result.meta`、`tool/code-dispatch{,-start}.arguments`）只做无损
JSON 检查、内部数字不解释为 Session 序号。
"""
from __future__ import annotations

__all__ = [
    "RELEASED_V0_EVENT_DISPOSITIONS",
    "RELEASED_V0_EVENT_TYPES",
    "RELEASED_V2_EVENT_DISPOSITIONS",
    "RELEASED_V2_EVENT_TYPES",
]


def _disposition(required: list[str], optional: list[str] | None = None,
                 opaque: list[str] | None = None) -> dict:
    return {
        "required": list(required),
        "optional": list(optional or []),
        "opaque": list(opaque or []),
    }


#: released-v0 事件 × payload 成员闭集（dispositions.ts:37-126 逐条转录）。
RELEASED_V0_EVENT_DISPOSITIONS: dict[str, dict] = {
    "agent-preset/selected": _disposition(["agentPreset"]),
    "agent/inbox/spliced": _disposition(
        ["target", "start", "inserted"], ["removedCount", "outcome"]),
    "approval/asked": _disposition(["id", "toolName"], ["callId", "reason"]),
    "approval/decided": _disposition(["id", "outcome"]),
    "approval/policy": _disposition(["policy"], ["source"]),
    "assistant/chunk": _disposition(["turn", "step", "chunk"]),
    "assistant/message": _disposition(
        ["turn", "step", "message"], ["usage", "interrupted"]),
    "command/done": _disposition(["commandId", "kind"], ["text", "sourceEventSeq"]),
    "command/run": _disposition(["commandId", "name", "source"], ["args"]),
    "compaction/end": _disposition(["compactionId", "turn"], ["sourceCommandId", "error"]),
    "compaction/prune": _disposition(["shadowedRange", "shadowedSeqs", "shadowedTokenCount"]),
    "compaction/start": _disposition(["compactionId", "turn"], ["sourceCommandId"]),
    "compaction/summary": _disposition(
        ["compactionId", "summary", "shadowedRange", "shadowedSeqs",
         "shadowedTokenCount", "provider", "model"],
        ["sourceCommandId", "maxTokens", "usage", "rawOutput", "llmStreamCall"]),
    "feedback/record": _disposition(["text"]),
    "goal/change": _disposition(
        ["kind", "version", "operation"],
        ["goal", "roundsStarted", "createdAt", "updatedAt", "cleared", "clearedAt"]),
    "hook/invoked": _disposition(["turn", "point", "dialect", "handlerId"], ["matcher"]),
    "hook/result": _disposition(
        ["turn", "point", "handlerId", "decision", "durationMs"],
        ["exitCode", "stderrSummary"]),
    "llm/retry": _disposition(
        ["retryId", "turn", "step", "provider", "mode", "policyKey", "retry",
         "delayMs", "failure"],
        ["maxRetries"]),
    "llm/retry-started": _disposition(["retryId", "turn", "step", "retry"]),
    "model/selection": _disposition(["provider", "model"], ["reasoningEffort"]),
    "permission/preset": _disposition(["preset"]),
    "plan/mode": _disposition(["active"]),
    "request/context": _disposition(["provider", "model"], ["contextWindow"]),
    "request/header": _disposition(["header", "reason"], ["startsSeries"]),
    "sandbox/mode": _disposition(["mode"], ["source"]),
    "schedule/change": _disposition(["version", "operation"], ["schedule", "id", "acceptedAt"]),
    "session-log-deepseek/delivery-accepted": _disposition(["sessionId", "throughSeq"]),
    "session/end-seed": _disposition([]),
    "session/title": _disposition(["title", "messageSeqs", "source"]),
    "session/title-llm-request": _disposition(
        ["titleProvider", "messageSeqs", "route", "system", "messages", "maxTokens"]),
    "step/end": _disposition(["turn", "step"]),
    "step/start": _disposition(["turn", "step"]),
    "subagent/descriptor": _disposition(
        ["mode", "version", "provider"],
        ["label", "agentProvider", "agentModel", "agentReasoningEffort",
         "persona", "toolFilter"]),
    "subagent/model-selection-policy": _disposition(["allowedModels"]),
    "team/member": _disposition(["version", "teamId", "member"]),
    "team/message/delivered": _disposition(["version", "teamId", "messageId", "targetId"]),
    "team/message/queued": _disposition(["version", "teamId", "message"]),
    "team/task": _disposition(["version", "teamId", "task"]),
    "todo/write": _disposition(["todos"]),
    "tool-workflow/agent-end": _disposition(["runId", "seq", "outcome"]),
    "tool-workflow/agent-start": _disposition(["runId", "seq", "label", "childId"], ["phase"]),
    "tool-workflow/run-end": _disposition(["runId", "stopReason"]),
    "tool-workflow/run-start": _disposition(["runId", "name"]),
    "tool/call": _disposition(["turn", "step", "callId", "name", "arguments"]),
    "tool/code-dispatch": _disposition(
        ["rootCallId", "parentCallId", "subCallId", "name", "arguments", "isError", "content"],
        [], ["arguments"]),
    "tool/code-dispatch-start": _disposition(
        ["rootCallId", "parentCallId", "subCallId", "name", "arguments"],
        [], ["arguments"]),
    "tool/result": _disposition(
        ["turn", "step", "message"], ["error", "meta"], ["meta"]),
    "turn/end": _disposition(["turn", "reason"]),
    "turn/start": _disposition(["turn"]),
    "user/message": _disposition(["role", "id", "content", "source"]),
    "web/deepseek-search-llm-request": _disposition(["endpoint", "apiVersion", "body"]),
}

#: released-v0 稳定词表（en localeCompare 排序 = Python sorted 等价）。
RELEASED_V0_EVENT_TYPES: list[str] = sorted(RELEASED_V0_EVENT_DISPOSITIONS)


def _v2_table() -> dict[str, dict]:
    """v2 修订表：v0 全表剔除 4 项 + 4 项重定义（v1-to-v2/dispositions.ts:7-29）。"""
    removed = {"assistant/chunk", "assistant/message",
               "session-log-deepseek/delivery-accepted", "session/end-seed"}
    table = {name: disp for name, disp in RELEASED_V0_EVENT_DISPOSITIONS.items()
             if name not in removed}
    table["assistant/attempt"] = _disposition(["turn", "step", "stream"])
    table["assistant/message"] = _disposition(
        ["turn", "step", "message", "stream"], ["usage", "interrupted"])
    table["session-log-deepseek/delivery-accepted"] = _disposition(
        ["sessionId", "throughSeq"], ["sessionFormatVersion"])
    table["session/end-seed"] = _disposition([], ["inherited"])
    return table


#: released-v2 事件 × payload 成员闭集。
RELEASED_V2_EVENT_DISPOSITIONS: dict[str, dict] = _v2_table()

#: released-v2 稳定词表。
RELEASED_V2_EVENT_TYPES: list[str] = sorted(RELEASED_V2_EVENT_DISPOSITIONS)
