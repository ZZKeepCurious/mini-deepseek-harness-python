"""v0→v1 相邻迁移（上游 session-format-v0-to-v1/src/migration.ts 逐字移植）。

legacy 归一化管线（逐事件固定序，normalizeReleasedV0Events）：
  assertSupportedLegacyType（retired 类型拒迁 + request/header fallback 拒迁）→
  normalizeLegacyTurnStart（trigger 剥离，顺序 = exactKeys → count → record → malformed）→
  normalizeLegacyTurnEnd（reason 转换表 + normalizeLegacyErrorReason）→
  normalizeLegacyRequestHeader（messagePrefix 删除）→ normalizeLegacySteering（两形态，
  形状不合 exactKeys 拒绝，绝不原样透传）→ normalizeLegacyMessage（flat 包装 / token
  tool/result 替换继承被替换表面首节点 id）→ assertReleasedEventPayload(0)（每事件）。

header 唯一变化 = version 0→1。归一化 v0 过 assert_normalized_released_v0_artifact，
目标 snapshot `released v0-to-v1 target` 后过 assert_released_v1_artifact。
消息措辞逐字：session id 用 JSON.stringify 引号（js_stringify）；assertHeaderVersion
是 SessionFormatError；tool/result replacement 缺失身份同样是 SessionFormatError。
"""
from __future__ import annotations

from typing import Any

from .helpers import (
    count,
    exact_keys,
    fail,
    is_json_object,
    js_stringify,
    released_v0_record,
    snapshot_json,
    unsupported,
)
from .validate import (
    assert_normalized_released_v0_artifact,
    assert_released_event_payload,
    assert_released_v0_source_artifact,
    assert_released_v1_artifact,
)

__all__ = ["V0_TO_V1", "legacy_message_id"]

_SURFACE_KEYS = ("type", "seq", "time", "data", "ignorable", "sourceEventSeqs", "surfaceOp")


def legacy_message_id(session_id: str, seq: int) -> str:
    """flat legacy 消息的合成身份（上游 legacyMessageId 逐字）。"""
    return f"legacy-message:{session_id}:{seq}"


def _refuse(session_id: str, detail: str, seq: int):
    """legacy 不可迁移（unsupported）。sessionId 走 JSON.stringify 引号。"""
    return unsupported(f"session {js_stringify(session_id)} contains {detail} at seq {seq}")


def _malformed_legacy(session_id: str, etype: str, seq: int):
    """malformed pre-react-loop 事件（SessionFormatError）。"""
    return fail(
        f"session {js_stringify(session_id)} contains malformed pre-react-loop {etype} at seq {seq}")


def _assert_supported_legacy_type(event: dict, session_id: str) -> None:
    if event["type"] in ("request/header-delta", "mode/set"):
        raise _refuse(session_id, f"unsupported legacy {event['type']} event", event["seq"])
    if event["type"] == "request/header":
        data = released_v0_record(event["data"], f"request/header {event['seq']} data")
        if data.get("reason") == "fallback":
            raise _refuse(session_id,
                          'unsupported request/header reason "fallback"', event["seq"])


def _normalize_turn_start(event: dict, session_id: str) -> dict:
    if event["type"] != "turn/start":
        return event
    data = released_v0_record(event["data"], f"turn/start {event['seq']} data")
    if "trigger" not in data:
        return event
    exact_keys(data, ("turn", "trigger"), (), f"turn/start {event['seq']} data")
    turn = count(data["turn"], f"turn/start {event['seq']} turn")
    trigger = released_v0_record(data["trigger"], f"turn/start {event['seq']} trigger")
    if turn < 1 or not isinstance(trigger.get("kind"), str) or not trigger["kind"]:
        raise _malformed_legacy(session_id, "turn/start", event["seq"])
    return {**event, "data": {"turn": turn}}


def _normalize_turn_end(event: dict, session_id: str) -> dict:
    if event["type"] != "turn/end":
        return event
    data = released_v0_record(event["data"], f"turn/end {event['seq']} data")
    exact_keys(data, ("turn", "reason"), (), f"turn/end {event['seq']} data")
    turn = count(data["turn"], f"turn/end {event['seq']} turn")
    if turn < 1:
        raise _malformed_legacy(session_id, "turn/end", event["seq"])
    reason = released_v0_record(data["reason"], f"turn/end {event['seq']} reason")
    if not isinstance(reason.get("kind"), str):
        raise _malformed_legacy(session_id, "turn/end", event["seq"])
    kind = reason["kind"]
    if kind in ("completed", "blocked", "max-tokens", "interrupted"):
        exact_keys(reason, ("kind",), (), f"turn/end {event['seq']} reason")
        return event
    if kind == "aborted":
        if "reason" in reason:
            return event
        exact_keys(reason, ("kind",), (), f"turn/end {event['seq']} reason")
        current = {"kind": "aborted", "reason": {"kind": "legacy"}}
    elif kind == "disposed":
        exact_keys(reason, ("kind",), (), f"turn/end {event['seq']} reason")
        current = {"kind": "aborted", "reason": {"kind": "disposed"}}
    elif kind == "error":
        if "error" in reason:
            return event
        current = _normalize_legacy_error_reason(reason, event["seq"], session_id)
    else:
        return event
    return {**event, "data": {**data, "reason": current}}


def _normalize_legacy_error_reason(reason: dict, seq: int, session_id: str) -> dict:
    count(reason.get("step"), f"turn/end {seq} error step")
    failure = reason.get("failure")
    if failure is not None:
        exact_keys(reason, ("kind", "step", "failure"), (), f"turn/end {seq} reason")
        record = released_v0_record(failure, f"turn/end {seq} failure")
        exact_keys(record, ("message", "code"),
                   ("status", "providerRetryAfterMs", "requestId"), f"turn/end {seq} failure")
        if not isinstance(record.get("message"), str) \
                or not isinstance(record.get("code"), str):
            raise _malformed_legacy(session_id, "turn/end", seq)
        return {"kind": "error", "error": record}
    exact_keys(reason, ("kind", "step", "message"), ("code",), f"turn/end {seq} reason")
    if not isinstance(reason.get("message"), str) \
            or (reason.get("code") is not None
                and not isinstance(reason.get("code"), str)):
        raise _malformed_legacy(session_id, "turn/end", seq)
    return {
        "kind": "error",
        "error": {
            "message": reason["message"],
            "code": reason["code"] if isinstance(reason.get("code"), str) else "UNKNOWN",
        },
    }


def _normalize_request_header(event: dict, session_id: str) -> dict:
    if event["type"] != "request/header":
        return event
    data = released_v0_record(event["data"], f"request/header {event['seq']} data")
    header = released_v0_record(data["header"], f"request/header {event['seq']} header")
    if "messagePrefix" not in header:
        return event
    if not isinstance(header["messagePrefix"], list):
        raise fail(
            f"session {js_stringify(session_id)} contains malformed request/header "
            f"messagePrefix at seq {event['seq']}")
    current_header = {k: v for k, v in header.items() if k != "messagePrefix"}
    return {**event, "data": {**data, "header": current_header}}


def _normalize_steering(event: dict, session_id: str) -> dict:
    if event["type"] != "steering/message":
        return event
    data = released_v0_record(event["data"], f"steering/message {event['seq']} data")
    wrapped = data.get("message")
    if wrapped is not None:
        exact_keys(data, ("turn", "message"), (),
                   f"steering/message {event['seq']} data")
        count(data["turn"], f"steering/message {event['seq']} turn")
        return {**event, "type": "user/message", "data": wrapped}
    exact_keys(data, ("turn", "content", "source"), (),
               f"steering/message {event['seq']} data")
    count(data["turn"], f"steering/message {event['seq']} turn")
    message = {k: v for k, v in data.items() if k != "turn"}
    return {
        **event,
        "type": "user/message",
        "data": {
            **message,
            "id": legacy_message_id(session_id, event["seq"]),
            "role": "user",
        },
    }


def _replacement_start(event: dict):
    operation = event.get("surfaceOp")
    if operation is None or not is_json_object(operation) or operation.get("op") != "replace":
        return None
    return operation["start"]


def _message_id(event: dict):
    data = released_v0_record(event["data"], f"{event['type']} {event['seq']} data")
    message = data if event["type"] == "user/message" \
        else (data.get("message") if is_json_object(data.get("message")) else None)
    return message.get("id") if is_json_object(message)\
        and isinstance(message.get("id"), str) else None


def _normalize_message(event: dict, session_id: str,
                       message_ids: dict[int, str]) -> dict:
    data = released_v0_record(event["data"], f"{event['type']} {event['seq']} data")
    etype = event["type"]
    if etype == "user/message":
        if "id" in data or "role" in data or "message" in data \
                or "content" not in data or "source" not in data:
            return event
        return {**event, "data": {**data,
                                  "id": legacy_message_id(session_id, event["seq"]),
                                  "role": "user"}}
    if etype == "assistant/message":
        if "message" in data or "content" not in data or "provenance" not in data:
            return event
        source = released_v0_record(data["provenance"],
                                    f"assistant/message {event['seq']} provenance")
        event_data = {k: v for k, v in data.items() if k not in ("content", "provenance")}
        return {
            **event,
            "data": {
                **event_data,
                "message": {
                    "id": legacy_message_id(session_id, event["seq"]),
                    "role": "assistant",
                    "content": data["content"],
                    "source": {**source, "kind": "model"},
                },
            },
        }
    if etype == "tool/result":
        if "message" in data or "callId" not in data or "content" not in data \
                or "isError" not in data:
            return event
        call_id = data["callId"]
        is_error = data["isError"]
        content = data["content"]
        if not isinstance(call_id, str) or not isinstance(is_error, bool) \
                or content is None:
            return event
        inherited_id = _replacement_start(event)
        owner_id = legacy_message_id(session_id, event["seq"]) \
            if inherited_id is None else message_ids.get(inherited_id)
        if owner_id is None:
            raise fail(f"tool/result {event['seq']} replacement cites a message without identity")
        event_data = {k: v for k, v in data.items()
                      if k not in ("callId", "content", "isError")}
        return {
            **event,
            "data": {
                **event_data,
                "message": {
                    "id": owner_id,
                    "role": "user",
                    "content": [{"type": "tool-result", "toolCallId": call_id,
                                 "content": content, "isError": is_error}],
                    "source": {"kind": "tool", "callId": call_id},
                },
            },
        }
    return event


def _migrate_header_v0_v1(header: dict) -> dict:
    if header.get("version") != 0:
        raise fail("expected format v0 header")
    return {**header, "version": 1}


def _migrate_v0_v1(artifact: dict) -> dict:
    """v0 → v1（上游 migrate 同序）：源冻结 → 逐事件归一化 + payload 校验 →
    归一化 v0 全量校验 → 目标 snapshot → v1 精确校验。header 唯一变化是 version。"""
    header = artifact["header"]
    session_id = header["id"]
    events = artifact["events"]
    assert_released_v0_source_artifact(artifact)
    message_ids: dict[int, str] = {}
    migrated: list[dict] = []
    for event in events:
        seq = event["seq"]
        etype = event["type"]
        _assert_supported_legacy_type(event, session_id)
        message = _normalize_turn_start(event, session_id)
        message = _normalize_turn_end(message, session_id)
        message = _normalize_request_header(message, session_id)
        message = _normalize_steering(message, session_id)
        message = _normalize_message(message, session_id, message_ids)
        assert_released_event_payload(message, 0)
        migrated.append({
            "type": message["type"],
            "seq": message["seq"],
            "time": message["time"],
            "data": message["data"],
            **({"ignorable": True} if message.get("ignorable") is True else {}),
            **({"sourceEventSeqs": list(message["sourceEventSeqs"])}
               if "sourceEventSeqs" in message else {}),
            **({"surfaceOp": message["surfaceOp"]} if "surfaceOp" in message else {}),
        })
        message_id = _message_id(message)
        if message_id is not None:
            message_ids[seq] = message_id
    assert_normalized_released_v0_artifact(
        {"header": header, "inherited_event_count": artifact["inherited_event_count"],
         "events": migrated})
    target = snapshot_json(
        {"header": {**header, "version": 1},
         "inherited_event_count": artifact["inherited_event_count"],
         "events": migrated},
        "released v0-to-v1 target")
    assert_released_v1_artifact(target)
    return target


V0_TO_V1 = {
    "name": "@deepseek-ai/dsh-session-format-v0-to-v1",
    "from_version": 0,
    "to_version": 1,
    "migrate_header": _migrate_header_v0_v1,
    "migrate": _migrate_v0_v1,
}