"""默认一次性摘要与检查点框架。

上游对照：packages/compaction/compaction-basic/src/summarizer.ts（COMPACTION_INSTRUCTION /
CHECKPOINT_PREAMBLE / SUMMARY_OPEN_TAG / frameSummary / finishError / summaryText）。

机制（对齐上游 summarizer.ts:111,161-176）：
  * 压缩指令作为重放会话之后**最后一条 user 消息**送达，而不是独立的摘要
    system prompt——让辅助调用成为会话请求的真前缀。mini 简化标注：上游动机是
    复用 provider 的 warm prefix cache（KV cache），mini 无真实 KV cache 语义，
    前缀重放仅为保证摘要输入与对话一致。
  * 摘要输出只取 text 块（图片输出拒绝），为空则失败终局；max-tokens 截断视为失败。
"""
from __future__ import annotations

from ..core.session import create_message, text_block

__all__ = ["CHECKPOINT_PREAMBLE", "COMPACTION_INSTRUCTION", "SUMMARY_CLOSE_TAG",
           "SUMMARY_OPEN_TAG", "frame_summary", "summarize_with_adapter"]

SUMMARY_OPEN_TAG = "<compacted-summary>"
SUMMARY_CLOSE_TAG = "</compacted-summary>"

COMPACTION_INSTRUCTION = "\n".join([
    "You are now acting as a compaction engine for this AI coding assistant. Condense the conversation ABOVE into a structured checkpoint that lets another model resume the work with no loss of essential context.",
    "",
    "Output EXACTLY the Markdown structure below: keep every section, in order. Use terse bullets, not prose paragraphs. Write \"(none)\" for an empty section — never drop a section.",
    "",
    "## Primary Request and Intent",
    "- [the user's original and evolving goals; quote verbatim where the exact wording matters]",
    "",
    "## Key Technical Concepts",
    "- [technologies, frameworks, patterns, and conventions in play]",
    "",
    "## Files and Code",
    "- [exact path: why it matters, key changes or snippets]",
    "",
    "## Errors and Fixes",
    "- [error: how it was resolved, plus any related user feedback]",
    "",
    "## Pending Jobs",
    "- [explicitly requested work not yet completed]",
    "",
    "## Current Work",
    "- [precisely what was in progress at this checkpoint]",
    "",
    "## Next Step",
    "- [the single next action, directly in line with the most recent request, or \"(none)\"]",
    "",
    "## Critical Context",
    "- [decisions and their rationale, constraints, user preferences, open questions, data needed to continue]",
    "",
    "Rules:",
    "- Write concise English engineering prose. Preserve exact file paths, commands, error strings, identifiers, numeric values, function signatures, and syntax fragments.",
    "- Capture user feedback and explicit instructions faithfully, especially corrections.",
    "- Do NOT mention this summarization request or that the context was compacted.",
    "- Output only the checkpoint text: do not call any tool or take any other action.",
    "- If the conversation already contains a <compacted-summary> block, it is a PRIOR checkpoint. Do not copy it forward verbatim: preserve still-true facts, drop stale ones, and merge newer information into a single consolidated summary under the same structure.",
])

CHECKPOINT_PREAMBLE = (
    "This is an automatically generated checkpoint condensing an earlier span of the "
    "conversation to free up context. Treat the captured context as established background "
    "and build on it without restating it. Continue the task directly from the messages "
    "that follow, without acknowledging this checkpoint."
)


def frame_summary(summary: list) -> list:
    """把摘要块包进检查点框架（对齐 upstream frameSummary 的逐块文本包装）。"""
    return [
        {"type": "text", "text": f"{CHECKPOINT_PREAMBLE}\n\n{SUMMARY_OPEN_TAG}"},
        *summary,
        {"type": "text", "text": SUMMARY_CLOSE_TAG},
    ]


def summarize_with_adapter(agent, config: dict, input_: dict) -> dict:
    """跑一次摘要：重放 region 消息 + 压缩指令，经 agent 的适配器流式产出。

    input_: {messages: [...], system?: str, tools?: [...]}（mini 仅 messages
    有值——压缩请求不经 agent/request 信封；system 用 agent.system_prompt）。
    返回 {summary, provider, model, maxTokens?, usage?}。
    """
    from ..llm import BlockAssembler, LlmFailure

    adapter = agent.adapter
    messages = list(input_.get("messages", []))
    messages.append(create_message(
        "user",
        [text_block(COMPACTION_INSTRUCTION)],
        {"kind": "plugin", "plugin": "dsh-compaction-basic"},
    ))
    assembler = BlockAssembler()
    for chunk in adapter.stream(messages, input_.get("tools", [])):
        assembler.push(chunk)
    _raise_on_finish_error(assembler.finish)
    summary = _summary_text(assembler.blocks)
    return {
        "summary": summary,
        "provider": getattr(adapter, "provider", ""),
        "model": getattr(adapter, "model", None),
        "maxTokens": config["maxTokens"],
        **({"usage": assembler.usage} if assembler.usage is not None else {}),
    }


def _raise_on_finish_error(finish: dict | None) -> None:
    """终态映射为 fail-closed 错误（对齐 upstream finishError）。"""
    from ..llm import LlmFailure

    if finish is None or finish.get("kind") in ("stop", "tool-calls"):
        return
    kind = finish.get("kind")
    if kind in ("error", "aborted"):
        failure = finish.get("failure") or {}
        raise LlmFailure(
            failure.get("code", "UNKNOWN"),
            failure.get("message", "摘要流在 finish 处失败"),
            status=failure.get("status"),
            provider_retry_after_ms=failure.get("providerRetryAfterMs"),
            request_id=failure.get("requestId"),
        )
    if kind == "max-tokens":
        raise LlmFailure(
            "MAX_TOKENS",
            "summarization truncated at the token cap (incomplete checkpoint)",
        )


def _summary_text(blocks: list) -> list:
    """只保留 text 块；图片输出拒绝（对齐 upstream summaryText）。"""
    from ..llm import LlmFailure

    if any(b.get("type") == "image" for b in blocks):
        raise LlmFailure("UNSUPPORTED_CONTENT", "compaction summary cannot contain image output")
    summary = [b for b in blocks if b.get("type") == "text"]
    if not any(b.get("text", "").strip() for b in summary):
        raise LlmFailure("EMPTY_RESPONSE", "summarization produced no text summary content")
    return summary