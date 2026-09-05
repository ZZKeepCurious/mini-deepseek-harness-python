"""released 逐字段 payload 语义（上游 session-format-v0-to-v1/src/payload-validation.ts
逐字移植：51 类型的嵌套成员校验 + 版本门控成员）。

消息措辞与上游逐字一致（member 方言带引号）；`js_stringify` 用紧凑分隔符与 ASCII
直书复刻 TS `JSON.stringify`（标题 framed 文本、retry 键、未知类型诊断共用）。
"""
from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any, Callable

from .helpers import (
    SessionFormatError,
    count,
    exact_keys,
    fail,
    is_json_object,
    js_stringify,
    released_v0_record,
    safe_integer,
)

__all__ = ["assert_released_payload_semantics"]

_UTC_INSTANT_RE = re.compile(
    r"^(?!0000)[0-9]{4}-(?:0[1-9]|1[0-2])-(?:0[1-9]|[12][0-9]|3[01])T"
    r"(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.[0-9]{3}Z$")


def exact_record(value: Any, label: str, required: list[str],
                 optional: list[str] | None = None) -> dict:
    record = released_v0_record(value, label)
    exact_keys(record, required, optional or [], label)
    return record


def string_value(value: Any, label: str) -> None:
    if not isinstance(value, str):
        raise fail(f"{label} must be a string")


def non_empty_string(value: Any, label: str) -> None:
    if not isinstance(value, str) or len(value) == 0:
        raise fail(f"{label} must be a non-empty string")


def boolean_value(value: Any, label: str) -> None:
    if not isinstance(value, bool):
        raise fail(f"{label} must be a boolean")


def safe_integer_value(value: Any, label: str) -> int:
    return safe_integer(value, label)


def count_value(value: Any, label: str) -> int:
    return count(value, label)


def positive_integer_value(value: Any, label: str) -> int:
    result = count_value(value, label)
    if result == 0:
        raise fail(f"{label} must be positive")
    return result


def finite_number_value(value: Any, label: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise fail(f"{label} must be a finite number")
    if isinstance(value, float) and (not math.isfinite(value) or math.copysign(1.0, value) < 0):
        raise fail(f"{label} must be a finite number")
    return value


def _literal_equal(candidate: Any, value: Any) -> bool:
    if isinstance(candidate, bool) or isinstance(value, bool):
        return candidate is value
    if isinstance(candidate, (int, float)) and isinstance(value, (int, float)):
        return candidate == value
    return candidate == value


def _js_literal(member: Any) -> str:
    if member is True:
        return "true"
    if member is False:
        return "false"
    return str(member)


def literal_value(value: Any, allowed: list[Any], label: str) -> None:
    if not any(_literal_equal(candidate, value) for candidate in allowed):
        raise fail(f"{label} must be one of {', '.join(_js_literal(member) for member in allowed)}")


def nullable_value(value: Any, label: str, validate: Callable[[Any, str], Any]) -> None:
    if value is not None:
        validate(value, label)


def array_value(value: Any, label: str, validate: Callable[[Any, str], Any]) -> list[Any]:
    if not isinstance(value, list):
        raise fail(f"{label} must be an array")
    for index, member in enumerate(value):
        validate(member, f"{label}[{index}]")
    return value


def coordinate_pair(data: dict, label: str) -> None:
    count_value(data.get("turn"), f"{label} turn")
    count_value(data.get("step"), f"{label} step")


def earlier_seq(value: Any, event_seq: int, label: str) -> int:
    seq = count_value(value, label)
    if seq >= event_seq:
        raise fail(f"{label} must identify an earlier event")
    return seq


def seq_array(value: Any, event_seq: int, label: str, require_non_empty: bool) -> list[Any]:
    seen: set[int] = set()
    values = array_value(value, label, lambda member, member_label: _seq_member(
        member, member_label, event_seq, label, seen))
    if require_non_empty and len(values) == 0:
        raise fail(f"{label} must be non-empty")
    return values


def _seq_member(member: Any, member_label: str, event_seq: int, label: str,
                seen: set[int]) -> None:
    seq = earlier_seq(member, event_seq, member_label)
    if seq in seen:
        raise fail(f"{label} repeats seq {seq}")
    seen.add(seq)


def llm_failure_value(value: Any, label: str) -> None:
    failure = exact_record(value, label,
                           ["message", "code"],
                           ["status", "providerRetryAfterMs", "requestId"])
    non_empty_string(failure.get("message"), f"{label} message")
    non_empty_string(failure.get("code"), f"{label} code")
    if failure.get("status") is not None:
        status = safe_integer_value(failure.get("status"), f"{label} status")
        if status < 100 or status > 599:
            raise fail(f"{label} status must be 100 through 599")
    if failure.get("providerRetryAfterMs") is not None \
            and finite_number_value(failure.get("providerRetryAfterMs"),
                                    f"{label} providerRetryAfterMs") <= 0:
        raise fail(f"{label} providerRetryAfterMs must be positive")
    if failure.get("requestId") is not None:
        non_empty_string(failure.get("requestId"), f"{label} requestId")


def token_usage_value(value: Any, label: str) -> None:
    usage = exact_record(value, label,
                         ["inputTokens", "outputTokens"],
                         ["totalTokens", "cacheReadTokens", "cacheWriteTokens",
                          "reasoningTokens"])
    for key in usage:
        count_value(usage[key], f"{label} {key}")


def content_blocks_value(value: Any, label: str, version: int) -> None:
    array_value(value, label, lambda member, member_label: content_block_value(
        member, member_label, version))


def content_block_value(value: Any, label: str, version: int) -> None:
    block = released_v0_record(value, label)
    block_type = block.get("type")
    if block_type == "text" or block_type == "reasoning":
        exact_keys(block, ("type", "text"), (), label)
        string_value(block.get("text"), f"{label} text")
        return
    if block_type == "image":
        exact_keys(block, ("type", "attachment"), (), label)
        image_attachment_value(block.get("attachment"), f"{label} attachment")
        return
    if block_type == "tool-call":
        exact_keys(block, ("type", "id", "name", "arguments"), (), label)
        non_empty_string(block.get("id"), f"{label} id")
        non_empty_string(block.get("name"), f"{label} name")
        string_value(block.get("arguments"), f"{label} arguments")
        return
    if block_type == "tool-result":
        exact_keys(block, ("type", "toolCallId", "content"), ("isError",), label)
        non_empty_string(block.get("toolCallId"), f"{label} toolCallId")
        content_blocks_value(block.get("content"), f"{label} content", version)
        if block.get("isError") is not None:
            boolean_value(block.get("isError"), f"{label} isError")
        return
    non_empty_string(block_type, f"{label} type")


def image_attachment_value(value: Any, label: str) -> None:
    attachment = exact_record(value, label,
                              ["attachmentId", "mediaType", "bytes", "width", "height"],
                              ["name", "originalDimensions"])
    non_empty_string(attachment.get("attachmentId"), f"{label} attachmentId")
    literal_value(attachment.get("mediaType"),
                  ["image/png", "image/jpeg", "image/webp", "image/gif"],
                  f"{label} mediaType")
    count_value(attachment.get("bytes"), f"{label} bytes")
    positive_integer_value(attachment.get("width"), f"{label} width")
    positive_integer_value(attachment.get("height"), f"{label} height")
    if attachment.get("name") is not None:
        string_value(attachment.get("name"), f"{label} name")
    if attachment.get("originalDimensions") is not None:
        dimensions = exact_record(attachment.get("originalDimensions"),
                                  f"{label} originalDimensions", ["width", "height"])
        positive_integer_value(dimensions.get("width"), f"{label} original width")
        positive_integer_value(dimensions.get("height"), f"{label} original height")


def message_value(value: Any, label: str, version: int,
                  expected: str | None = None) -> None:
    message = exact_record(value, label, ["id", "role", "content", "source"])
    non_empty_string(message.get("id"), f"{label} id")
    if expected == "assistant":
        role = "assistant"
    elif expected in ("user", "tool"):
        role = "user"
    else:
        role = None
    if role is None:
        literal_value(message.get("role"), ["system", "user", "assistant"], f"{label} role")
    else:
        literal_value(message.get("role"), [role], f"{label} role")
    content_blocks_value(message.get("content"), f"{label} content", version)
    message_source_value(message.get("source"), f"{label} source", version, expected)
    if expected == "tool":
        content = message.get("content")
        block = released_v0_record(content[0], f"{label} tool result") \
            if isinstance(content, list) and len(content) == 1 else None
        source = released_v0_record(message.get("source"), f"{label} source")
        if block is None or block.get("type") != "tool-result" \
                or block.get("toolCallId") != source.get("callId"):
            raise fail(f"{label} must contain exactly one tool-result block")


def message_source_value(value: Any, label: str, version: int,
                         expected: str | None = None) -> None:
    source = released_v0_record(value, label)
    if expected == "assistant" and source.get("kind") != "model":
        raise fail(f"{label} must be model source")
    if expected == "tool" and source.get("kind") != "tool":
        raise fail(f"{label} must be tool source")
    kind = source.get("kind")
    if kind == "user":
        exact_keys(source, ("kind",), ("rpcId", "clientTimeZone"), label)
        if source.get("rpcId") is not None:
            non_empty_string(source.get("rpcId"), f"{label} rpcId")
        if source.get("clientTimeZone") is not None:
            non_empty_string(source.get("clientTimeZone"), f"{label} clientTimeZone")
        return
    if kind == "plugin":
        plugin_source_value(source, label)
        return
    if kind == "model":
        exact_keys(source, ("kind", "provider", "model"), ("replayState",), label)
        non_empty_string(source.get("provider"), f"{label} provider")
        non_empty_string(source.get("model"), f"{label} model")
        return
    if kind == "tool":
        exact_keys(source, ("kind", "callId"), (), label)
        non_empty_string(source.get("callId"), f"{label} callId")
        return
    if kind == "agent-instructions":
        exact_keys(source, ("kind", "form", "changes"),
                   ("baseline", "baselineIdentity"), label)
        literal_value(source.get("form"), ["instructions"], f"{label} form")
        if source.get("baseline") is not None:
            literal_value(source.get("baseline"), [True], f"{label} baseline")
        if source.get("baselineIdentity") is not None:
            non_empty_string(source.get("baselineIdentity"), f"{label} baselineIdentity")
        array_value(source.get("changes"), f"{label} changes", _agent_instruction_change)
        return
    if kind == "session-reference":
        session_reference_source_value(source, label, version)
        return
    if kind == "team-message":
        exact_keys(source, ("kind", "teamId", "messageId", "senderId", "senderName"),
                   (), label)
        for key in ("teamId", "messageId", "senderId"):
            non_empty_string(source.get(key), f"{label} {key}")
        string_value(source.get("senderName"), f"{label} senderName")
        return
    if kind == "goal":
        exact_keys(source, ("kind", "goalId", "revision", "round"), (), label)
        non_empty_string(source.get("goalId"), f"{label} goalId")
        positive_integer_value(source.get("revision"), f"{label} revision")
        positive_integer_value(source.get("round"), f"{label} round")
        return
    if kind == "skill-invocation":
        exact_keys(source, ("kind", "name", "form"), (), label)
        non_empty_string(source.get("name"), f"{label} name")
        literal_value(source.get("form"), ["instructions"], f"{label} form")
        return
    if kind == "skill-catalog":
        exact_keys(source, ("kind", "form", "entries"), ("update",), label)
        literal_value(source.get("form"), ["catalog"], f"{label} form")
        if source.get("update") is not None:
            literal_value(source.get("update"), [True], f"{label} update")
        array_value(source.get("entries"), f"{label} entries", _skill_catalog_entry)
        return
    if kind in ("coordinator", "subagent-report"):
        exact_keys(source, ("kind", "form", "senderSessionId"), (), label)
        literal_value(source.get("form"), ["relay"], f"{label} form")
        non_empty_string(source.get("senderSessionId"), f"{label} senderSessionId")
        return
    if kind == "subagent-settled":
        exact_keys(source, ("kind", "form", "summary", "senderSessionId"), (), label)
        literal_value(source.get("form"), ["notice"], f"{label} form")
        string_value(source.get("summary"), f"{label} summary")
        non_empty_string(source.get("senderSessionId"), f"{label} senderSessionId")
        return
    if kind == "webhook":
        exact_keys(source, ("kind", "provider", "source", "deliveryId", "ruleId",
                            "form", "summary"), (), label)
        for key in ("provider", "source", "deliveryId", "ruleId"):
            non_empty_string(source.get(key), f"{label} {key}")
        literal_value(source.get("form"), ["notice"], f"{label} form")
        string_value(source.get("summary"), f"{label} summary")
        return
    non_empty_string(kind, f"{label} kind")


def _agent_instruction_change(member: Any, member_label: str) -> None:
    change = exact_record(member, member_label, ["action", "scope", "path"], ["digest"])
    literal_value(change.get("action"), ["set", "replace", "remove"], f"{member_label} action")
    string_value(change.get("scope"), f"{member_label} scope")
    string_value(change.get("path"), f"{member_label} path")
    if change.get("digest") is not None:
        string_value(change.get("digest"), f"{member_label} digest")


def _skill_catalog_entry(member: Any, member_label: str) -> None:
    entry = exact_record(member, member_label, ["name", "description"])
    non_empty_string(entry.get("name"), f"{member_label} name")
    string_value(entry.get("description"), f"{member_label} description")


def plugin_source_value(source: dict, label: str) -> None:
    optional = ["form", "sections", "summary"]
    if source.get("plugin") == "compact":
        optional.append("compactionId")
        optional.append("sourceCommandId")
    exact_keys(source, ("kind", "plugin"), tuple(optional), label)
    non_empty_string(source.get("plugin"), f"{label} plugin")
    if source.get("plugin") == "compact":
        non_empty_string(source.get("compactionId"), f"{label} compactionId")
        if source.get("sourceCommandId") is not None:
            non_empty_string(source.get("sourceCommandId"), f"{label} sourceCommandId")
    form = source.get("form")
    if form is None:
        return
    literal_value(form, ["instructions", "catalog", "snapshot", "notice", "relay",
                         "recall"], f"{label} form")
    if form == "snapshot":
        array_value(source.get("sections"), f"{label} sections", _plugin_snapshot_section)
    elif source.get("sections") is not None:
        raise fail(f"{label} sections require snapshot form")
    if form == "notice":
        string_value(source.get("summary"), f"{label} summary")
    elif source.get("summary") is not None:
        raise fail(f"{label} summary requires notice form")


def _plugin_snapshot_section(member: Any, member_label: str) -> None:
    section = exact_record(member, member_label, ["name", "text"])
    non_empty_string(section.get("name"), f"{member_label} name")
    string_value(section.get("text"), f"{member_label} text")


def session_reference_source_value(source: dict, label: str, version: int) -> None:
    exact_keys(source, ("kind", "form", "version", "references"), (), label)
    literal_value(source.get("form"), ["recall"], f"{label} form")
    literal_value(source.get("version"), [1], f"{label} version")
    expected_input_index = [0]
    session_ids: set[str] = set()
    required = [
        "sessionId", "label", "capturedThroughSeq", "compacted", "originalMessages",
        "retainedMessages", "omittedMessages", "omittedBytes", "truncated", "inputIndex",
    ]
    optional = ["capturedFormatVersion"] if version >= 1 else []

    def check_reference(member: Any, member_label: str) -> None:
        _session_reference(member, member_label, version, label, session_ids, required,
                           optional, expected_input_index[0])
        expected_input_index[0] += 1

    references = array_value(source.get("references"), f"{label} references", check_reference)
    if len(references) == 0:
        raise fail(f"{label} references must be non-empty")


def _session_reference(member: Any, member_label: str, version: int, label: str,
                       session_ids: set[str], required: list[str], optional: list[str],
                       expected_input_index: int) -> None:
    reference = exact_record(member, member_label, required, optional)
    non_empty_string(reference.get("sessionId"), f"{member_label} sessionId")
    string_value(reference.get("label"), f"{member_label} label")
    if reference.get("capturedThroughSeq") is not None:
        count_value(reference.get("capturedThroughSeq"), f"{member_label} capturedThroughSeq")
    if reference.get("capturedFormatVersion") is not None:
        captured_version = count_value(
            reference.get("capturedFormatVersion"), f"{member_label} capturedFormatVersion")
        if captured_version < 1 or captured_version > version:
            raise fail(f"{member_label} capturedFormatVersion must be between 1 and {version}")
    boolean_value(reference.get("compacted"), f"{member_label} compacted")
    original = count_value(reference.get("originalMessages"), f"{member_label} originalMessages")
    retained = count_value(reference.get("retainedMessages"), f"{member_label} retainedMessages")
    omitted = count_value(reference.get("omittedMessages"), f"{member_label} omittedMessages")
    omitted_bytes = count_value(reference.get("omittedBytes"), f"{member_label} omittedBytes")
    input_index = count_value(reference.get("inputIndex"), f"{member_label} inputIndex")
    truncated = reference.get("truncated")
    boolean_value(truncated, f"{member_label} truncated")
    if retained > original or omitted != original - retained:
        raise fail(f"{member_label} message counts are inconsistent")
    if truncated != (omitted > 0 or omitted_bytes > 0):
        raise fail(f"{member_label} truncated disagrees with omitted content")
    if input_index != expected_input_index:
        raise fail(f"{label} inputIndex must match reference position")
    session_id = reference.get("sessionId")
    if session_id in session_ids:
        raise fail(f"{label} repeats sessionId {session_id}")
    session_ids.add(session_id)


def stream_chunk_value(value: Any, label: str) -> None:
    chunk = released_v0_record(value, label)
    ctype = chunk.get("type")
    if ctype == "block-start":
        exact_keys(chunk, ("type", "index", "blockType"), (), label)
        count_value(chunk.get("index"), f"{label} index")
        non_empty_string(chunk.get("blockType"), f"{label} blockType")
        return
    if ctype in ("text-delta", "reasoning-delta"):
        exact_keys(chunk, ("type", "index", "text"), (), label)
        count_value(chunk.get("index"), f"{label} index")
        string_value(chunk.get("text"), f"{label} text")
        return
    if ctype == "tool-call-delta":
        exact_keys(chunk, ("type", "index", "id", "argumentsDelta"), ("name",), label)
        count_value(chunk.get("index"), f"{label} index")
        non_empty_string(chunk.get("id"), f"{label} id")
        if chunk.get("name") is not None:
            string_value(chunk.get("name"), f"{label} name")
        string_value(chunk.get("argumentsDelta"), f"{label} argumentsDelta")
        return
    if ctype == "block-end":
        exact_keys(chunk, ("type", "index", "block"), (), label)
        count_value(chunk.get("index"), f"{label} index")
        content_block_value(chunk.get("block"), f"{label} block", 1)
        return
    if ctype == "usage":
        exact_keys(chunk, ("type", "usage"), (), label)
        token_usage_value(chunk.get("usage"), f"{label} usage")
        return
    if ctype == "finish":
        exact_keys(chunk, ("type", "reason"), ("replayState",), label)
        finish_reason_value(chunk.get("reason"), f"{label} reason")
        if chunk.get("replayState") is not None:
            replay_envelope_value(chunk.get("replayState"), f"{label} replayState")
        return
    raise fail(f"{label} has unknown stream chunk type {js_stringify(ctype)}")


def finish_reason_value(value: Any, label: str) -> None:
    reason = released_v0_record(value, label)
    kind = reason.get("kind")
    if kind in ("aborted", "error"):
        exact_keys(reason, ("kind", "failure"), (), label)
        llm_failure_value(reason.get("failure"), f"{label} failure")
        return
    if kind in ("stop", "tool-calls", "max-tokens"):
        exact_keys(reason, ("kind",), (), label)
    non_empty_string(kind, f"{label} kind")


def replay_envelope_value(value: Any, label: str) -> None:
    replay = exact_record(value, label, ["response"], ["blocks"])
    if replay.get("blocks") is not None and not isinstance(replay.get("blocks"), list):
        raise fail(f"{label} blocks must be an array")


def turn_end_reason_value(value: Any, label: str) -> None:
    reason = released_v0_record(value, label)
    kind = reason.get("kind")
    if kind in ("completed", "blocked", "max-tokens", "interrupted"):
        exact_keys(reason, ("kind",), (), label)
        return
    if kind == "aborted":
        exact_keys(reason, ("kind", "reason"), (), label)
        cause = released_v0_record(reason.get("reason"), f"{label} abort cause")
        if cause.get("kind") == "hook":
            exact_keys(cause, ("kind", "reason"), (), f"{label} abort cause")
            string_value(cause.get("reason"), f"{label} abort reason")
        else:
            exact_keys(cause, ("kind",), (), f"{label} abort cause")
            literal_value(cause.get("kind"), ["user", "parent", "disposed", "legacy"],
                          f"{label} abort kind")
        return
    if kind == "error":
        exact_keys(reason, ("kind", "error"), (), label)
        llm_failure_value(reason.get("error"), f"{label} error")
        return
    non_empty_string(kind, f"{label} kind")


def request_header_value(value: Any, label: str) -> None:
    header = exact_record(value, label, ["config"],
                          ["adapterDefaults", "system", "tools"])
    config = exact_record(header.get("config"), f"{label} config",
                          ["provider", "model"],
                          ["reasoningEffort", "temperature", "maxTokens", "stop"])
    non_empty_string(config.get("provider"), f"{label} provider")
    non_empty_string(config.get("model"), f"{label} model")
    if config.get("reasoningEffort") is not None:
        non_empty_string(config.get("reasoningEffort"), f"{label} reasoningEffort")
    if config.get("temperature") is not None:
        finite_number_value(config.get("temperature"), f"{label} temperature")
    if config.get("maxTokens") is not None:
        positive_integer_value(config.get("maxTokens"), f"{label} maxTokens")
    if config.get("stop") is not None:
        array_value(config.get("stop"), f"{label} stop", string_value)
    if header.get("adapterDefaults") is not None:
        defaults = exact_record(header.get("adapterDefaults"), f"{label} adapterDefaults",
                                [], ["reasoningEffort", "maxTokens"])
        for key, marker in defaults.items():
            literal_value(marker, [True], f"{label} adapterDefaults {key}")
            if key not in config:
                raise fail(f"{label} adapter default {key} lacks config value")
    if header.get("system") is not None:
        string_value(header.get("system"), f"{label} system")
    if header.get("tools") is not None:
        array_value(header.get("tools"), f"{label} tools", tool_schema_value)


def tool_schema_value(value: Any, label: str) -> None:
    schema = exact_record(value, label, ["name", "description", "parameters"])
    non_empty_string(schema.get("name"), f"{label} name")
    string_value(schema.get("description"), f"{label} description")
    released_v0_record(schema.get("parameters"), f"{label} parameters")


def shadowed_value(data: dict, event_seq: int, label: str) -> None:
    range_ = exact_record(data.get("shadowedRange"), f"{label} shadowedRange",
                          ["start", "end"])
    start = earlier_seq(range_.get("start"), event_seq, f"{label} shadowedRange start")
    end = earlier_seq(range_.get("end"), event_seq, f"{label} shadowedRange end")
    seqs = seq_array(data.get("shadowedSeqs"), event_seq, f"{label} shadowedSeqs", True)
    if len(seqs) == 0 or seqs[0] != start or seqs[-1] != end:
        raise fail(f"{label} shadowedRange must match shadowedSeqs endpoints")
    count_value(data.get("shadowedTokenCount"), f"{label} shadowedTokenCount")


def goal_change_value(data: dict, label: str) -> None:
    literal_value(data.get("kind"), ["goal/change"], f"{label} kind")
    literal_value(data.get("version"), [1], f"{label} version")
    if data.get("operation") == "clear":
        exact_keys(data, ("kind", "version", "operation", "cleared", "clearedAt"),
                   (), f"{label} data")
        goal_ref_value(data.get("cleared"), f"{label} cleared")
        count_value(data.get("clearedAt"), f"{label} clearedAt")
        return
    exact_keys(data,
               ("kind", "version", "operation", "goal", "roundsStarted", "createdAt",
                "updatedAt"),
               (), f"{label} data")
    literal_value(data.get("operation"),
                  ["create", "edit", "pause", "resume", "complete", "block"],
                  f"{label} operation")
    goal_snapshot_value(data.get("goal"), f"{label} goal")
    count_value(data.get("roundsStarted"), f"{label} roundsStarted")
    count_value(data.get("createdAt"), f"{label} createdAt")
    count_value(data.get("updatedAt"), f"{label} updatedAt")


def goal_ref_value(value: Any, label: str) -> None:
    ref = exact_record(value, label, ["id", "revision"])
    non_empty_string(ref.get("id"), f"{label} id")
    positive_integer_value(ref.get("revision"), f"{label} revision")


def goal_snapshot_value(value: Any, label: str) -> None:
    goal = exact_record(value, label,
                        ["id", "revision", "objective", "phase", "maxGoalRounds"],
                        ["blockedReason"])
    non_empty_string(goal.get("id"), f"{label} id")
    positive_integer_value(goal.get("revision"), f"{label} revision")
    non_empty_string(goal.get("objective"), f"{label} objective")
    literal_value(goal.get("phase"), ["active", "paused", "blocked", "complete"],
                  f"{label} phase")
    positive_integer_value(goal.get("maxGoalRounds"), f"{label} maxGoalRounds")
    if goal.get("phase") == "blocked":
        reason = exact_record(goal.get("blockedReason"), f"{label} blockedReason",
                              ["code", "message"])
        non_empty_string(reason.get("code"), f"{label} blocked code")
        non_empty_string(reason.get("message"), f"{label} blocked message")
    elif goal.get("blockedReason") is not None:
        raise fail(f"{label} blockedReason requires blocked phase")


def schedule_change_value(data: dict, label: str) -> None:
    literal_value(data.get("version"), [1], f"{label} version")
    if data.get("operation") == "create":
        exact_keys(data, ("version", "operation", "schedule"), (), f"{label} data")
        schedule_record_value(data.get("schedule"), f"{label} schedule")
        return
    exact_keys(data, ("version", "operation", "id"),
               ["acceptedAt"] if data.get("operation") == "dispatch" else [],
               f"{label} data")
    literal_value(data.get("operation"), ["delete", "dispatch"], f"{label} operation")
    schedule_id_value(data.get("id"), f"{label} id")
    if data.get("acceptedAt") is not None:
        instant_value(data.get("acceptedAt"), f"{label} acceptedAt")


def schedule_record_value(value: Any, label: str) -> None:
    record = released_v0_record(value, label)
    kind = record.get("kind")
    if kind == "after":
        exact_keys(record, ("id", "kind", "prompt", "afterSeconds", "scheduledAt"),
                   (), label)
        positive_integer_value(record.get("afterSeconds"), f"{label} afterSeconds")
    elif kind == "at":
        exact_keys(record, ("id", "kind", "prompt", "scheduledAt"), (), label)
    elif kind == "every":
        exact_keys(record, ("id", "kind", "prompt", "everySeconds", "scheduledAt"),
                   (), label)
        seconds = positive_integer_value(record.get("everySeconds"), f"{label} everySeconds")
        if seconds < 300:
            raise fail(f"{label} everySeconds must be at least 300")
    else:
        raise fail(f"{label} has unknown schedule kind")
    schedule_id_value(record.get("id"), f"{label} id")
    non_empty_string(record.get("prompt"), f"{label} prompt")
    instant_value(record.get("scheduledAt"), f"{label} scheduledAt")


def schedule_id_value(value: Any, label: str) -> None:
    non_empty_string(value, label)
    if value.strip() != value:
        raise fail(f"{label} must not have surrounding whitespace")


def instant_value(value: Any, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, str) \
            or not _UTC_INSTANT_RE.match(value):
        raise fail(f"{label} must be a canonical UTC instant")
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError:
        raise fail(f"{label} must be a canonical UTC instant") from None
    canonical = parsed.strftime("%Y-%m-%dT%H:%M:%S") + f".{parsed.microsecond // 1000:03d}Z"
    if canonical != value:
        raise fail(f"{label} must be a canonical UTC instant")


def title_source_value(value: Any, label: str) -> None:
    source = released_v0_record(value, label)
    if source.get("kind") == "provider":
        exact_keys(source, ("kind", "provider"), ("model",), label)
        non_empty_string(source.get("provider"), f"{label} provider")
        if source.get("model") is not None:
            model_route_value(source.get("model"), f"{label} model")
        return
    exact_keys(source, ("kind",), (), label)
    literal_value(source.get("kind"), ["fallback", "user"], f"{label} kind")


def model_route_value(value: Any, label: str) -> None:
    route = exact_record(value, label, ["provider", "model"])
    non_empty_string(route.get("provider"), f"{label} provider")
    non_empty_string(route.get("model"), f"{label} model")


def subagent_descriptor_value(data: dict, label: str) -> None:
    literal_value(data.get("version"), [3], f"{label} version")
    non_empty_string(data.get("provider"), f"{label} provider")
    if data.get("mode") == "one-shot":
        exact_keys(data, ("mode", "version", "provider"), ("label",), f"{label} data")
        if data.get("label") is not None:
            string_value(data.get("label"), f"{label} label")
        return
    literal_value(data.get("mode"), ["continuable"], f"{label} mode")
    non_empty_string(data.get("label"), f"{label} label")
    for key in ("agentProvider", "agentModel", "agentReasoningEffort", "persona"):
        if data.get(key) is not None:
            non_empty_string(data.get(key), f"{label} {key}")
    if (data.get("agentProvider") is None) != (data.get("agentModel") is None):
        raise fail(f"{label} agentProvider and agentModel must be paired")
    if data.get("toolFilter") is not None:
        filter_ = exact_record(data.get("toolFilter"), f"{label} toolFilter",
                               [], ["allow", "deny"])
        if filter_.get("allow") is None and filter_.get("deny") is None:
            raise fail(f"{label} toolFilter requires allow or deny")
        if filter_.get("allow") is not None:
            array_value(filter_.get("allow"), f"{label} allow", non_empty_string)
        if filter_.get("deny") is not None:
            array_value(filter_.get("deny"), f"{label} deny", non_empty_string)


def allowed_models_value(value: Any, label: str) -> None:
    seen: set[str] = set()
    routes = array_value(value, label, lambda member, member_label: _allowed_route(
        member, member_label, label, seen))
    if len(routes) == 0:
        raise fail(f"{label} must be non-empty")


def _allowed_route(member: Any, member_label: str, label: str, seen: set[str]) -> None:
    route = exact_record(member, member_label, ["provider", "model"])
    non_empty_string(route.get("provider"), f"{member_label} provider")
    non_empty_string(route.get("model"), f"{member_label} model")
    key = f'{route.get("provider")}\0{route.get("model")}'
    if key in seen:
        raise fail(f"{label} repeats route {key}")
    seen.add(key)


def team_selector(data: dict, label: str) -> None:
    literal_value(data.get("version"), [1], f"{label} version")
    non_empty_string(data.get("teamId"), f"{label} teamId")


def team_member_value(value: Any, label: str) -> None:
    member = exact_record(value, label,
                          ["id", "name", "description", "provider", "context", "phase"],
                          ["error"])
    non_empty_string(member.get("id"), f"{label} id")
    string_value(member.get("name"), f"{label} name")
    string_value(member.get("description"), f"{label} description")
    string_value(member.get("provider"), f"{label} provider")
    literal_value(member.get("context"), ["fresh", "fork"], f"{label} context")
    literal_value(member.get("phase"), ["provisioning", "active", "failed"],
                  f"{label} phase")
    if member.get("error") is not None:
        string_value(member.get("error"), f"{label} error")


def team_task_value(value: Any, label: str) -> None:
    task = exact_record(value, label,
                        ["id", "revision", "subject", "description", "status",
                         "blockedBy", "writeScopes"],
                        ["ownerId"])
    non_empty_string(task.get("id"), f"{label} id")
    positive_integer_value(task.get("revision"), f"{label} revision")
    string_value(task.get("subject"), f"{label} subject")
    string_value(task.get("description"), f"{label} description")
    literal_value(task.get("status"),
                  ["pending", "in_progress", "completed", "deleted"],
                  f"{label} status")
    if task.get("ownerId") is not None:
        non_empty_string(task.get("ownerId"), f"{label} ownerId")
    array_value(task.get("blockedBy"), f"{label} blockedBy", non_empty_string)
    array_value(task.get("writeScopes"), f"{label} writeScopes", string_value)


def team_message_value(value: Any, label: str, version: int) -> None:
    message = exact_record(value, label,
                           ["id", "senderId", "senderName", "targetId", "delivery",
                            "content"])
    for key in ("id", "senderId", "targetId"):
        non_empty_string(message.get(key), f"{label} {key}")
    string_value(message.get("senderName"), f"{label} senderName")
    literal_value(message.get("delivery"), ["quiet", "wakeup"], f"{label} delivery")
    content_blocks_value(message.get("content"), f"{label} content", version)


def workflow_identity(data: dict, label: str) -> None:
    non_empty_string(data.get("runId"), f"{label} runId")
    positive_integer_value(data.get("seq"), f"{label} seq")


def deep_seek_search_body_value(value: Any, label: str) -> None:
    body = exact_record(value, label, ["model", "max_tokens", "messages", "tools"])
    non_empty_string(body.get("model"), f"{label} model")
    positive_integer_value(body.get("max_tokens"), f"{label} max_tokens")
    messages = array_value(body.get("messages"), f"{label} messages",
                           _deep_seek_search_message)
    if len(messages) != 1:
        raise fail(f"{label} messages must contain one user message")
    tools = array_value(body.get("tools"), f"{label} tools", _deep_seek_search_tool)
    if len(tools) != 1:
        raise fail(f"{label} tools must contain one web search tool")


def _deep_seek_search_message(member: Any, member_label: str) -> None:
    message = exact_record(member, member_label, ["role", "content"])
    literal_value(message.get("role"), ["user"], f"{member_label} role")
    content = array_value(message.get("content"), f"{member_label} content",
                          _deep_seek_search_text_block)
    if len(content) != 1:
        raise fail(f"{member_label} content must contain one text block")


def _deep_seek_search_text_block(block: Any, block_label: str) -> None:
    text = exact_record(block, block_label, ["type", "text"])
    literal_value(text.get("type"), ["text"], f"{block_label} type")
    string_value(text.get("text"), f"{block_label} text")


def _deep_seek_search_tool(member: Any, member_label: str) -> None:
    tool = exact_record(member, member_label, ["type", "name", "max_uses"])
    literal_value(tool.get("type"), ["web_search_20250305"], f"{member_label} type")
    literal_value(tool.get("name"), ["web_search"], f"{member_label} name")
    positive_integer_value(tool.get("max_uses"), f"{member_label} max_uses")


def assert_released_payload_semantics(event: dict, version: int) -> None:
    """一个已知事件的嵌套 payload 语义（上游 assertReleasedPayloadSemantics）。"""
    data = released_v0_record(event.get("data"), f"{event.get('type')} {event.get('seq')} data")
    label = f"{event.get('type')} {event.get('seq')}"
    etype = event.get("type")
    if etype == "agent-preset/selected":
        string_value(data.get("agentPreset"), f"{label} agentPreset")
    elif etype == "agent/inbox/spliced":
        literal_value(data.get("target"), ["next-turn", "next-step"], f"{label} target")
        count_value(data.get("start"), f"{label} start")
        if data.get("removedCount") is not None:
            count_value(data.get("removedCount"), f"{label} removedCount")
        array_value(data.get("inserted"), f"{label} inserted",
                    lambda member, _label: message_value(
                        member, f"{label} inserted message", version, "user"))
        if data.get("outcome") is not None:
            literal_value(data.get("outcome"), ["canceled"], f"{label} outcome")
    elif etype == "approval/asked":
        non_empty_string(data.get("id"), f"{label} id")
        non_empty_string(data.get("toolName"), f"{label} toolName")
        if data.get("callId") is not None:
            non_empty_string(data.get("callId"), f"{label} callId")
        if data.get("reason") is not None:
            string_value(data.get("reason"), f"{label} reason")
    elif etype == "approval/decided":
        non_empty_string(data.get("id"), f"{label} id")
        literal_value(data.get("outcome"),
                      ["allowed-once", "rejected", "cancelled", "unavailable"],
                      f"{label} outcome")
    elif etype == "approval/policy":
        literal_value(data.get("policy"), ["ask", "never"], f"{label} policy")
        if data.get("source") is not None:
            literal_value(data.get("source"), ["delegation"], f"{label} source")
    elif etype == "assistant/chunk":
        coordinate_pair(data, label)
        stream_chunk_value(data.get("chunk"), f"{label} chunk")
    elif etype == "assistant/message":
        coordinate_pair(data, label)
        message_value(data.get("message"), f"{label} message", version, "assistant")
        if data.get("usage") is not None:
            token_usage_value(data.get("usage"), f"{label} usage")
        if data.get("interrupted") is not None:
            literal_value(data.get("interrupted"), [True], f"{label} interrupted")
    elif etype == "command/done":
        non_empty_string(data.get("commandId"), f"{label} commandId")
        literal_value(data.get("kind"), ["success", "error"], f"{label} kind")
        if data.get("text") is not None:
            string_value(data.get("text"), f"{label} text")
        if data.get("sourceEventSeq") is not None:
            earlier_seq(data.get("sourceEventSeq"), event.get("seq"), f"{label} sourceEventSeq")
    elif etype == "command/run":
        non_empty_string(data.get("commandId"), f"{label} commandId")
        non_empty_string(data.get("name"), f"{label} name")
        if data.get("args") is not None:
            string_value(data.get("args"), f"{label} args")
        source = exact_record(data.get("source"), f"{label} source", ["kind"])
        literal_value(source.get("kind"), ["user"], f"{label} source kind")
    elif etype in ("compaction/start", "compaction/end"):
        non_empty_string(data.get("compactionId"), f"{label} compactionId")
        if data.get("sourceCommandId") is not None:
            non_empty_string(data.get("sourceCommandId"), f"{label} sourceCommandId")
        nullable_value(data.get("turn"), f"{label} turn", count_value)
        if data.get("error") is not None:
            string_value(data.get("error"), f"{label} error")
    elif etype == "compaction/prune":
        shadowed_value(data, event.get("seq"), label)
    elif etype == "compaction/summary":
        if data.get("llmStreamCall") is True and data.get("rawOutput") is None:
            raise fail(f"{label} llmStreamCall requires rawOutput")
        non_empty_string(data.get("compactionId"), f"{label} compactionId")
        if data.get("sourceCommandId") is not None:
            non_empty_string(data.get("sourceCommandId"), f"{label} sourceCommandId")
        content_blocks_value(data.get("summary"), f"{label} summary", version)
        shadowed_value(data, event.get("seq"), label)
        non_empty_string(data.get("provider"), f"{label} provider")
        non_empty_string(data.get("model"), f"{label} model")
        if data.get("maxTokens") is not None:
            count_value(data.get("maxTokens"), f"{label} maxTokens")
        if data.get("usage") is not None:
            token_usage_value(data.get("usage"), f"{label} usage")
        if data.get("rawOutput") is not None:
            content_blocks_value(data.get("rawOutput"), f"{label} rawOutput", version)
        if data.get("llmStreamCall") is not None:
            literal_value(data.get("llmStreamCall"), [True], f"{label} llmStreamCall")
    elif etype == "feedback/record":
        non_empty_string(data.get("text"), f"{label} text")
    elif etype == "goal/change":
        goal_change_value(data, label)
    elif etype == "hook/invoked":
        count_value(data.get("turn"), f"{label} turn")
        non_empty_string(data.get("point"), f"{label} point")
        literal_value(data.get("dialect"), ["claude-code", "codex"], f"{label} dialect")
        if data.get("matcher") is not None:
            string_value(data.get("matcher"), f"{label} matcher")
        non_empty_string(data.get("handlerId"), f"{label} handlerId")
    elif etype == "hook/result":
        count_value(data.get("turn"), f"{label} turn")
        non_empty_string(data.get("point"), f"{label} point")
        non_empty_string(data.get("handlerId"), f"{label} handlerId")
        non_empty_string(data.get("decision"), f"{label} decision")
        if data.get("exitCode") is not None:
            safe_integer_value(data.get("exitCode"), f"{label} exitCode")
        if data.get("stderrSummary") is not None:
            string_value(data.get("stderrSummary"), f"{label} stderrSummary")
        if finite_number_value(data.get("durationMs"), f"{label} durationMs") < 0:
            raise fail(f"{label} durationMs must be non-negative")
    elif etype == "llm/retry":
        non_empty_string(data.get("retryId"), f"{label} retryId")
        coordinate_pair(data, label)
        non_empty_string(data.get("provider"), f"{label} provider")
        literal_value(data.get("mode"), ["normal", "always"], f"{label} mode")
        non_empty_string(data.get("policyKey"), f"{label} policyKey")
        positive_integer_value(data.get("retry"), f"{label} retry")
        if data.get("mode") == "normal":
            max_retries = positive_integer_value(data.get("maxRetries"), f"{label} maxRetries")
            if data.get("retry") > max_retries:
                raise fail(f"{label} retry exceeds maxRetries")
        elif data.get("maxRetries") is not None:
            raise fail(f"{label} always mode must omit maxRetries")
        delay_ms = finite_number_value(data.get("delayMs"), f"{label} delayMs")
        if delay_ms < 0:
            raise fail(f"{label} delayMs must be non-negative")
        if delay_ms > 2147483647:
            raise fail(f"{label} delayMs exceeds the timer range")
        llm_failure_value(data.get("failure"), f"{label} failure")
    elif etype == "llm/retry-started":
        non_empty_string(data.get("retryId"), f"{label} retryId")
        coordinate_pair(data, label)
        positive_integer_value(data.get("retry"), f"{label} retry")
    elif etype == "model/selection":
        non_empty_string(data.get("provider"), f"{label} provider")
        non_empty_string(data.get("model"), f"{label} model")
        if data.get("reasoningEffort") is not None:
            non_empty_string(data.get("reasoningEffort"), f"{label} reasoningEffort")
    elif etype == "permission/preset":
        non_empty_string(data.get("preset"), f"{label} preset")
    elif etype == "plan/mode":
        boolean_value(data.get("active"), f"{label} active")
    elif etype == "request/context":
        non_empty_string(data.get("provider"), f"{label} provider")
        non_empty_string(data.get("model"), f"{label} model")
        if data.get("contextWindow") is not None:
            positive_integer_value(data.get("contextWindow"), f"{label} contextWindow")
    elif etype == "request/header":
        request_header_value(data.get("header"), f"{label} header")
        literal_value(data.get("reason"), ["initial", "resume", "change", "series"],
                      f"{label} reason")
        if data.get("startsSeries") is not None:
            literal_value(data.get("startsSeries"), [True], f"{label} startsSeries")
    elif etype == "sandbox/mode":
        literal_value(data.get("mode"),
                      ["read-only", "workspace-write", "danger-full-access"],
                      f"{label} mode")
        if data.get("source") is not None:
            literal_value(data.get("source"), ["delegation"], f"{label} source")
    elif etype == "schedule/change":
        schedule_change_value(data, label)
    elif etype == "session-log-deepseek/delivery-accepted":
        accepted_version = 0 if data.get("sessionFormatVersion") is None \
            else count_value(data.get("sessionFormatVersion"), f"{label} sessionFormatVersion")
        if accepted_version != version:
            return
        non_empty_string(data.get("sessionId"), f"{label} sessionId")
        earlier_seq(data.get("throughSeq"), event.get("seq"), f"{label} throughSeq")
    elif etype == "session/end-seed":
        pass
    elif etype == "session/title":
        non_empty_string(data.get("title"), f"{label} title")
        seq_array(data.get("messageSeqs"), event.get("seq"), f"{label} messageSeqs", False)
        title_source_value(data.get("source"), f"{label} source")
    elif etype == "session/title-llm-request":
        non_empty_string(data.get("titleProvider"), f"{label} titleProvider")
        seq_array(data.get("messageSeqs"), event.get("seq"), f"{label} messageSeqs", True)
        model_route_value(data.get("route"), f"{label} route")
        string_value(data.get("system"), f"{label} system")
        array_value(data.get("messages"), f"{label} messages",
                    lambda member, _label: message_value(
                        member, f"{label} message", version))
        positive_integer_value(data.get("maxTokens"), f"{label} maxTokens")
    elif etype in ("step/end", "step/start"):
        coordinate_pair(data, label)
    elif etype == "subagent/descriptor":
        subagent_descriptor_value(data, label)
    elif etype == "subagent/model-selection-policy":
        allowed_models_value(data.get("allowedModels"), f"{label} allowedModels")
    elif etype == "team/member":
        team_selector(data, label)
        team_member_value(data.get("member"), f"{label} member")
    elif etype == "team/message/delivered":
        team_selector(data, label)
        non_empty_string(data.get("messageId"), f"{label} messageId")
        non_empty_string(data.get("targetId"), f"{label} targetId")
    elif etype == "team/message/queued":
        team_selector(data, label)
        team_message_value(data.get("message"), f"{label} message", version)
    elif etype == "team/task":
        team_selector(data, label)
        team_task_value(data.get("task"), f"{label} task")
    elif etype == "todo/write":
        array_value(data.get("todos"), f"{label} todos", _todo_item)
    elif etype == "tool-workflow/agent-end":
        workflow_identity(data, label)
        literal_value(data.get("outcome"), ["completed", "failed", "cancelled"],
                      f"{label} outcome")
    elif etype == "tool-workflow/agent-start":
        workflow_identity(data, label)
        string_value(data.get("label"), f"{label} label")
        if data.get("phase") is not None:
            string_value(data.get("phase"), f"{label} phase")
        non_empty_string(data.get("childId"), f"{label} childId")
    elif etype == "tool-workflow/run-end":
        non_empty_string(data.get("runId"), f"{label} runId")
        literal_value(data.get("stopReason"), ["completed", "cancelled", "error"],
                      f"{label} stopReason")
    elif etype == "tool-workflow/run-start":
        non_empty_string(data.get("runId"), f"{label} runId")
        non_empty_string(data.get("name"), f"{label} name")
    elif etype == "tool/call":
        coordinate_pair(data, label)
        non_empty_string(data.get("callId"), f"{label} callId")
        non_empty_string(data.get("name"), f"{label} name")
        string_value(data.get("arguments"), f"{label} arguments")
    elif etype in ("tool/code-dispatch", "tool/code-dispatch-start"):
        non_empty_string(data.get("rootCallId"), f"{label} rootCallId")
        non_empty_string(data.get("parentCallId"), f"{label} parentCallId")
        non_empty_string(data.get("subCallId"), f"{label} subCallId")
        non_empty_string(data.get("name"), f"{label} name")
        if etype == "tool/code-dispatch":
            boolean_value(data.get("isError"), f"{label} isError")
            content_blocks_value(data.get("content"), f"{label} content", version)
    elif etype == "tool/result":
        coordinate_pair(data, label)
        message_value(data.get("message"), f"{label} message", version, "tool")
        if data.get("error") is not None:
            error = exact_record(data.get("error"), f"{label} error", ["name", "code"])
            non_empty_string(error.get("name"), f"{label} error name")
            non_empty_string(error.get("code"), f"{label} error code")
    elif etype == "turn/end":
        count_value(data.get("turn"), f"{label} turn")
        turn_end_reason_value(data.get("reason"), f"{label} reason")
    elif etype == "turn/start":
        count_value(data.get("turn"), f"{label} turn")
    elif etype == "user/message":
        message_value(data, label, version, "user")
    elif etype == "web/deepseek-search-llm-request":
        non_empty_string(data.get("endpoint"), f"{label} endpoint")
        non_empty_string(data.get("apiVersion"), f"{label} apiVersion")
        deep_seek_search_body_value(data.get("body"), f"{label} body")
    else:
        raise fail(f"released payload validator is missing event {js_stringify(etype)}")


def _todo_item(member: Any, item_label: str) -> None:
    item = exact_record(member, item_label, ["content", "status"])
    string_value(item.get("content"), f"{item_label} content")
    literal_value(item.get("status"), ["pending", "in_progress", "completed"],
                  f"{item_label} status")