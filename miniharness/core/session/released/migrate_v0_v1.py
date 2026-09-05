"""v0→v1 相邻迁移（上游 session-format-v0-to-v1/src/migration.ts）。

legacy 归一化管线（逐事件顺序固定）：retired 类型拒迁 → turn/start.trigger 丢弃 →
turn/end reason 转换表 → request/header messagePrefix 删除 → steering/message 两形态 →
flat 消息包装（id = `legacy-message:<sessionId>:<seq>`）→ 替换 tool/result 继承被替换
表面首节点消息 id。header 唯一变化 = version 0→1；其余事件恒等。

不移植（Phase B 登记）：51 类型逐字段 payload 语义与跨事件关系状态机——转换所需的
形状半边由 scoped 校验承担，最终防线是迁移产物过现行 v2 restore。
"""
from __future__ import annotations

from typing import Any

from .helpers import SessionFormatUnsupportedMigrationError, is_json_object, unsupported
from .validate import assert_scoped_v1_artifact

__all__ = ["V0_TO_V1", "legacy_message_id"]

_SURFACE_KEYS = ("type", "seq", "time", "data", "ignorable", "sourceEventSeqs", "surfaceOp")


def legacy_message_id(session_id: str, seq: int) -> str:
    """flat legacy 消息的合成身份（上游 legacyMessageId 逐字）。"""
    return f"legacy-message:{session_id}:{seq}"


def _refuse(session_id: str, detail: str, seq: int) -> SessionFormatUnsupportedMigrationError:
    return unsupported(f'session "{session_id}" contains {detail} at seq {seq}')


def _normalize_turn_start(session_id: str, seq: int, data: dict) -> dict:
    if "trigger" not in data:
        return data
    if set(data) != {"turn", "trigger"}:
        raise _refuse(session_id, "malformed pre-react-loop turn/start", seq)
    turn = data["turn"]
    trigger = data["trigger"]
    if isinstance(turn, bool) or not isinstance(turn, int) or turn < 1 \
            or not isinstance(trigger, dict) \
            or not isinstance(trigger.get("kind"), str) or not trigger["kind"]:
        raise _refuse(session_id, "malformed pre-react-loop turn/start", seq)
    return {"turn": turn}


def _normalize_turn_end(data: dict) -> dict:
    reason = data.get("reason")
    if not is_json_object(reason):
        return data
    kind = reason.get("kind")
    if kind in ("completed", "blocked", "max-tokens", "interrupted"):
        return data
    if kind == "aborted":
        if "reason" in reason:
            return data
        return {**data, "reason": {**reason, "reason": {"kind": "legacy"}}}
    if kind == "disposed":
        return {**data, "reason": {"kind": "aborted", "reason": {"kind": "disposed"}}}
    if kind == "error" and "error" not in reason:
        step = reason.get("step")
        if "failure" in reason:
            failure = dict(reason["failure"]) if is_json_object(reason["failure"]) else reason["failure"]
            return {**data, "reason": {"kind": "error", "step": step, "error": failure}}
        error_record: dict[str, Any] = {
            "message": reason.get("message"),
            "code": reason.get("code", "UNKNOWN"),
        }
        return {**data, "reason": {"kind": "error", "step": step, "error": error_record}}
    return data  # 未来 kind：merge-extensible 原样保留


def _normalize_request_header(session_id: str, seq: int, data: dict) -> dict:
    reason = data.get("reason")
    if reason == "fallback":
        raise _refuse(session_id, 'unsupported request/header reason "fallback"', seq)
    header_payload = data.get("header")
    if is_json_object(header_payload) and "messagePrefix" in header_payload:
        if not isinstance(header_payload["messagePrefix"], list):
            raise _refuse(session_id, "malformed request/header messagePrefix", seq)
        stripped = {k: v for k, v in header_payload.items() if k != "messagePrefix"}
        return {**data, "header": stripped}
    return data


def _normalize_steering(event: dict, seq: int, data: dict, session_id: str) -> tuple[str, dict]:
    if set(data) == {"turn", "message"} and is_json_object(data["message"]):
        return "user/message", data["message"]
    if set(data) == {"turn", "content", "source"}:
        return "user/message", {
            "id": legacy_message_id(session_id, seq),
            "role": "user",
            "content": data["content"],
            "source": data["source"],
        }
    _ = event, session_id
    # 形状不合 → 原样保留，交给 disposition 键集拒绝（fail-closed）
    return "steering/message", data


def _normalize_message(event: dict, seq: int, data: dict, session_id: str,
                       message_ids: dict[int, str]) -> tuple[str, dict]:
    etype = event["type"]
    if etype == "user/message":
        if "message" not in data and "id" not in data and "role" not in data \
                and "content" in data and "source" in data:
            data = {**data, "id": legacy_message_id(session_id, seq), "role": "user"}
        message = data.get("id")
        if isinstance(message, str):
            message_ids[seq] = message
        return etype, data
    if etype == "assistant/message" and "message" not in data \
            and "content" in data and "provenance" in data:
        wrapped = {
            "id": legacy_message_id(session_id, seq),
            "role": "assistant",
            "content": data["content"],
            "source": {**data["provenance"], "kind": "model"},
        }
        new_data = {k: v for k, v in data.items() if k not in ("content", "provenance")}
        new_data["message"] = wrapped
        message_ids[seq] = wrapped["id"]
        return etype, new_data
    if etype == "assistant/message" and is_json_object(data.get("message")):
        message_id = data["message"].get("id")
        if isinstance(message_id, str):
            message_ids[seq] = message_id
        return etype, data
    if etype == "tool/result" and "message" not in data \
            and "callId" in data and "content" in data and "isError" in data:
        call_id = data["callId"]
        is_error = data["isError"]
        if not isinstance(call_id, str) or not isinstance(is_error, bool):
            return etype, data  # 形状不合 → disposition 拒（fail-closed）
        surface_op = event.get("surfaceOp")
        if is_json_object(surface_op):
            owner_seq = surface_op.get("start")
            owner_id = message_ids.get(owner_seq) if isinstance(owner_seq, int) else None
            if owner_id is None:
                raise unsupported(
                    f"tool/result {seq} replacement cites a message without identity")
        else:
            owner_id = legacy_message_id(session_id, seq)
        message = {
            "id": owner_id,
            "role": "user",
            "content": [{"type": "tool-result", "toolCallId": call_id,
                         "content": data["content"], "isError": is_error}],
            "source": {"kind": "tool", "callId": call_id},
        }
        new_data = {k: v for k, v in data.items() if k not in ("callId", "content", "isError")}
        new_data["message"] = message
        message_ids[seq] = owner_id
        return etype, new_data
    if etype == "tool/result" and is_json_object(data.get("message")):
        message_id = data["message"].get("id")
        if isinstance(message_id, str):
            message_ids[seq] = message_id
        return etype, data
    return etype, data


def _migrate_header_v0_v1(header: dict) -> dict:
    if header.get("version") != 0:
        raise unsupported("expected format v0 header")
    return {**header, "version": 1}


def _migrate_v0_v1(artifact: dict) -> dict:
    """v0 → v1：legacy 归一化 + scoped 目标校验（header 唯一变化是 version）。"""
    header = artifact["header"]
    session_id = header["id"]
    events = artifact["events"]
    message_ids: dict[int, str] = {}
    migrated: list[dict] = []
    for event in events:
        seq = event["seq"]
        etype = event["type"]
        data = event["data"]
        if etype in ("request/header-delta", "mode/set"):
            raise _refuse(session_id, f"unsupported legacy {etype} event", seq)
        if etype == "turn/start":
            data = _normalize_turn_start(session_id, seq, data)
        elif etype == "turn/end":
            data = _normalize_turn_end(data)
        elif etype == "request/header":
            data = _normalize_request_header(session_id, seq, data)
        elif etype == "steering/message":
            etype, data = _normalize_steering(event, seq, data, session_id)
        else:
            etype, data = _normalize_message(event, seq, data, session_id, message_ids)
        migrated.append({
            "type": etype,
            "seq": seq,
            "time": event["time"],
            "data": data,
            **({"ignorable": True} if event.get("ignorable") is True else {}),
            **({"sourceEventSeqs": list(event["sourceEventSeqs"])}
               if "sourceEventSeqs" in event else {}),
            **({"surfaceOp": event["surfaceOp"]} if "surfaceOp" in event else {}),
        })
    target = {"header": _migrate_header_v0_v1(header),
              "inherited_event_count": artifact["inherited_event_count"],
              "events": migrated}
    assert_scoped_v1_artifact(target)
    return target


V0_TO_V1 = {
    "name": "@deepseek-ai/dsh-session-format-v0-to-v1",
    "from_version": 0,
    "to_version": 1,
    "migrate_header": _migrate_header_v0_v1,
    "migrate": _migrate_v0_v1,
}
