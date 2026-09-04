"""web 流传输：typert Remote 流 wire 语法（对齐 `packages/api/gateway/src/stream-protocol.ts`）。

纯语法层：零第三方依赖、零 asyncio、零 HTTP。职责 = WebSocket mux 帧与
`$events` 结果封包的**逐字段校验与投影**，判定标准：消息能否在两型判别之间
无损往返。物理载体（WebSocket/HTTP 路由）在 `web/mux.py`、`web/server.py`。

已核实的契约（alpha.1，逐条对应上游 stream-protocol.ts + stream-server.ts +
gateway index.ts）：
  * 单一路径 `/api/remote.mux`（REMOTE_STREAM_MUX_PATH）承载所有 Remote 流。
  * 浏览器→宿主文本帧两型：`open`（streamId/endpoint/payload —— 判别字段
    必在，未知键被 schemastery 投影丢弃而非报错）、`cancel`（streamId + 类型）。
    streamId/endpoint 非空字符串。二进制消息是协议错误（close 1003）；
    JSON/形状错误 → close 1008。
  * 宿主→浏览器帧三型：`item`（streamId + 可选 value）/ `end` / `error`
    （error = {code, message, details}，恒对象）。
  * 事件流端点 `$events`：payload 必须恰为 `{args:{}}`（空 args 对象）；首帧
    `{type:'ready', clientId, host:{home}}`（host.home 是宿主 home，仅用于前端
    缩写路径显示）。下游帧：`emit`（{event, args:数组}）/ `waterfall`
    （{event, eventId, agentId, request:对象}）/ `cancel`（{eventId}）。
  * 结果端点 `$events/result`：payload 恰 `{args:{clientId, eventId, outcome}}`；
    outcome 三型：`next` / `result`（可选 JSON value）/ `rejected`
    （error = {name, message, code?, details?}，details 须无损 JSON）。
  * 无损 JSON（isRemoteJsonValue）：有限数、无 -0、键为字符串的纯 dict/数组、
    无循环、无装饰（subclass）/稀疏。
"""
from __future__ import annotations

import json
from typing import Any

__all__ = [
    "REMOTE_STREAM_MUX_PATH",
    "REMOTE_EVENT_STREAM_ENDPOINT",
    "REMOTE_EVENT_RESULT_ENDPOINT",
    "DEFAULT_WEBSOCKET_HEARTBEAT_INTERVAL_MS",
    "StreamProtocolError",
    "is_remote_json_value",
    "is_remote_event_id",
    "is_remote_event_client_id",
    "is_remote_event_agent_id",
    "parse_remote_stream_client_message",
    "parse_remote_stream_server_message",
    "parse_remote_event_result_payload",
]

#: 承载所有 typert Remote 流的 WebSocket 路径（REMOTE_STREAM_MUX_PATH）
REMOTE_STREAM_MUX_PATH = "/api/remote.mux"

#: 网关内转发事件流端点（REMOTE_EVENT_STREAM_ENDPOINT）
REMOTE_EVENT_STREAM_ENDPOINT = "$events"

#: 返回一个客户端 Remote 事件结果的 unary 端点（REMOTE_EVENT_RESULT_ENDPOINT）
REMOTE_EVENT_RESULT_ENDPOINT = "$events/result"

#: WebSocket Ping 间隔缺省（gateway Config `websocketHeartbeatIntervalMs`
#: @default 2000，index.ts；miss 2 次即 terminate，stream-server.ts
#: MAX_MISSED_HEARTBEATS=2）。mini 的实际 Ping 由 launcher 的 transport 级
#: uvicorn 选项承载（ws_ping_interval=2 / ws_ping_timeout=4）。
DEFAULT_WEBSOCKET_HEARTBEAT_INTERVAL_MS = 2_000


class StreamProtocolError(ValueError):
    """wire 语法校验失败（载体层捕获后按位置 close/prepare 拒绝）。"""


def _exact_keys(value: dict, expected: tuple[str, ...]) -> bool:
    return set(value) == set(expected)


def _subset_keys(value: dict, allowed: tuple[str, ...]) -> bool:
    return set(value).issubset(set(allowed))


def _is_plain_record(value: Any) -> bool:
    # 拒绝 dict 子类（装饰）：对齐 isRemoteJsonValue 的"无装饰纯对象"语义
    return type(value) is dict


def _valid_id(value: Any) -> bool:
    return isinstance(value, str) and len(value) > 0


def is_remote_event_id(value: Any) -> bool:
    """非空字符串 Remote 事件相关 id（isRemoteEventId）。"""
    return _valid_id(value)


def is_remote_event_client_id(value: Any) -> bool:
    """非空字符串客户端事件代次 id（isRemoteEventClientId）。"""
    return _valid_id(value)


def is_remote_event_agent_id(value: Any) -> bool:
    """非空字符串 Agent 身份（isRemoteEventAgentId）。"""
    return _valid_id(value)


def is_remote_json_value(value: Any) -> bool:
    """无损 JSON 判定（isRemoteJsonValue）：可跨 JSON 传输而不丢失/强制。"""
    return _visit_json(value, set())


def _visit_json(value: Any, ancestors: set) -> bool:
    if value is None or isinstance(value, (str, bool)):
        return True
    if isinstance(value, int) and not isinstance(value, bool):
        return True
    if isinstance(value, float):
        return _finite(value)
    if type(value) is not dict and type(value) is not list:
        return False
    identity = id(value)
    if identity in ancestors:
        return False
    ancestors.add(identity)
    try:
        if type(value) is list:
            for index, item in enumerate(value):
                if not _visit_json(item, ancestors):
                    return False
            return True
        for key, item in value.items():
            if not isinstance(key, str):
                return False
            if not _visit_json(item, ancestors):
                return False
        return True
    finally:
        ancestors.discard(identity)


def _finite(value: int | float) -> bool:
    from math import copysign, isfinite

    if not isfinite(value):
        return False
    return not (value == 0 and copysign(1.0, float(value)) < 0)


def parse_remote_stream_client_message(text: str) -> dict:
    """解析校验一条浏览器→宿主文本消息（parseRemoteStreamClientMessage）。"""
    return _parse_message(text, _validate_client)

def _validate_client(value: dict) -> dict:
    """unknown 键被 schemastery 投影丢弃；判别字段缺失/类型错才拒绝。"""
    kind = value.get("type")
    if kind == "cancel":
        if isinstance(value.get("streamId"), str) and value["streamId"]:
            return {"type": "cancel", "streamId": value["streamId"]}
        raise StreamProtocolError("api gateway: invalid Remote stream client message")
    if kind == "open":
        if (isinstance(value.get("streamId"), str) and value["streamId"]
                and isinstance(value.get("endpoint"), str) and value["endpoint"]
                and "payload" in value):
            return {"type": "open", "streamId": value["streamId"],
                    "endpoint": value["endpoint"], "payload": value["payload"]}
        raise StreamProtocolError("api gateway: invalid Remote stream client message")
    raise StreamProtocolError("api gateway: invalid Remote stream client message")


def parse_remote_stream_server_message(text: str) -> dict:
    """解析校验一条宿主→浏览器文本消息（parseRemoteStreamServerMessage）。"""
    return _parse_message(text, _validate_server)

def _validate_server(value: dict) -> dict:
    kind = value.get("type")
    if kind == "item":
        if isinstance(value.get("streamId"), str) and value["streamId"]:
            return value
        raise StreamProtocolError("api gateway: invalid Remote stream server message")
    if kind == "end":
        if isinstance(value.get("streamId"), str) and value["streamId"]:
            return value
        raise StreamProtocolError("api gateway: invalid Remote stream server message")
    if kind == "error":
        error = value.get("error")
        if (isinstance(value.get("streamId"), str) and value["streamId"]
                and isinstance(error, dict) and not isinstance(error, list)
                and _subset_keys(error, ("code", "message", "details"))
                and isinstance(error.get("code"), str)
                and isinstance(error.get("message"), str)
                and "details" in error
                and isinstance(error.get("details"), dict)):
            return value
        raise StreamProtocolError("api gateway: invalid Remote stream server message")
    raise StreamProtocolError("api gateway: invalid Remote stream server message")


def parse_remote_event_result_payload(payload: Any) -> dict:
    """解析 `$events/result` 的 payload：`{args: <结果封包>}` 逐字段投影。

    对应 gateway index.ts parseRemoteEventResultPayload + stream-protocol.ts
    parseRemoteEventResult：返回 {clientId, eventId, outcome} 三种合法形态。
    未知键被 schemastery 投影丢弃（判别字段存在即可）。
    """
    if (not _is_plain_record(payload)
            or "args" not in payload
            or not _is_plain_record(payload["args"])):
        raise StreamProtocolError(
            "typert gateway: Remote event result requires exactly one plain-object args field")
    return _parse_event_result(payload["args"])


def _parse_event_result(value: Any) -> dict:
    if (not _is_plain_record(value)
            or "clientId" not in value or "eventId" not in value or "outcome" not in value
            or not is_remote_event_client_id(value.get("clientId"))
            or not is_remote_event_id(value.get("eventId"))
            or not _is_plain_record(value.get("outcome"))):
        raise StreamProtocolError("api gateway: invalid Remote event result")
    outcome = value["outcome"]
    result = {"clientId": value["clientId"], "eventId": value["eventId"],
              "outcome": _parse_event_outcome(outcome)}
    return result


def _parse_event_outcome(value: Any) -> dict:
    if not _is_plain_record(value):
        raise StreamProtocolError("api gateway: invalid Remote event result")
    kind = value.get("kind")
    if kind == "next" and "value" not in value and "error" not in value:
        return {"kind": "next"}
    if kind == "result" and "error" not in value:
        if "value" in value and not is_remote_json_value(value["value"]):
            raise StreamProtocolError("api gateway: invalid Remote event result")
        if "value" in value:
            return {"kind": "result", "value": value["value"]}
        return {"kind": "result"}
    if kind == "rejected" and "value" not in value:
        return {"kind": "rejected",
                "error": _parse_event_rejection(value.get("error"))}
    raise StreamProtocolError("api gateway: invalid Remote event result")


def _parse_event_rejection(value: Any) -> dict:
    if (not _is_plain_record(value)
            or not _subset_keys(value, ("name", "message", "code", "details"))
            or not isinstance(value.get("name"), str) or not value["name"]
            or not isinstance(value.get("message"), str)):
        raise StreamProtocolError("api gateway: invalid Remote event rejection")
    code = value.get("code")
    details = value.get("details")
    if code is not None and not isinstance(code, str):
        raise StreamProtocolError("api gateway: invalid Remote event rejection")
    if details is not None and not is_remote_json_value(details):
        raise StreamProtocolError("api gateway: invalid Remote event rejection")
    result = {"name": value["name"], "message": value["message"]}
    if isinstance(code, str):
        result["code"] = code
    if "details" in value:
        result["details"] = details
    return result


def _parse_message(text: str, validate) -> dict:
    try:
        decoded = json.loads(text)
    except Exception as error:  # noqa: BLE001 - JSON 解析失败按语法错误上报
        raise StreamProtocolError("api gateway: Remote stream message is not JSON") from error
    if not isinstance(decoded, dict):
        raise StreamProtocolError("api gateway: Remote stream message must be an object")
    return validate(decoded)