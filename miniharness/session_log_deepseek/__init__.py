"""官方 DeepSeek 请求的增量 session-log 贡献（对齐 packages/session/session-log-deepseek）。

把 pending 事件序列化进一个带字节上限的 `dsh_session_log` 请求字段：选**最长的
pending 前缀**，其序列化字段（含 `throughSeq` 位宽）不超过 `maxBytes`；首个事件
单独超限（或无法序列化）时返回 `None`（不携带字段）并告警，水位保持不变。
被接受的 `throughSeq` 经 `session-log-deepseek/delivery-accepted` 事件回写日志，
重启恢复据此保守重传不确定尾巴。

**装配点（载体）**：上游把 `dsh_session_log` 经 `deepseekLlmApiExtensions` 注册表注入
DeepSeek 请求；mini 无该注册表，故本模块暴露纯函数 `prepare`/`prepare_field`/`accept`
供宿主在请求组装 seam 调用（触发条件：引入 LLM 请求扩展注册表时接线）。
"""
from __future__ import annotations

import math
import weakref
from collections.abc import Mapping
from typing import Any, Callable

from ..core.session.json import thaw
from ..core.session.types import KNOWN_TYPES, SESSION_FORMAT_VERSION

__all__ = [
    "DEFAULT_MAX_BYTES",
    "accepted_through",
    "fold_accepted_through",
    "is_enabled",
    "json_bytes",
    "prepare",
    "prepare_field",
    "resolve_config",
    "wire_event",
    "wire_header",
    "wire_surface_op",
]

#: 单个 `dsh_session_log` 字段的默认上限（UTF-8 字节）。
DEFAULT_MAX_BYTES = 8 * 1024 * 1024

#: 这些 surface 事件可携带 sourceEventSeqs（assistant/message 内嵌源流，除外）。
_SOURCE_SURFACE_TYPES = frozenset({
    "developer/message", "system/message", "user/message", "tool/result",
})

#: 水位折叠缓存（模块级 ambient，按 Session 键控；对齐上游 acceptanceFolds）。
_acceptance_folds: "weakref.WeakKeyDictionary[Any, dict]" = weakref.WeakKeyDictionary()


def resolve_config(config: dict | None) -> dict:
    """校验并填充配置（enabled 缺省 true，maxBytes 缺省 8 MiB、最小 1）。"""
    config = config or {}
    enabled = config.get("enabled", True)
    max_bytes = config.get("maxBytes", DEFAULT_MAX_BYTES)
    if (not isinstance(max_bytes, int) or isinstance(max_bytes, bool) or max_bytes < 1):
        raise ValueError("session-log-deepseek: maxBytes must be a positive integer")
    return {"enabled": enabled, "maxBytes": max_bytes}


def is_enabled(config: Mapping[str, Any]) -> bool:
    """每次请求现读 enabled（对齐上游 `Volatile<boolean>` 的 `config.enabled.get()`）。"""
    enabled = config.get("enabled", True)
    getter = getattr(enabled, "get", None)
    if callable(getter):
        return bool(getter())
    if callable(enabled):
        return bool(enabled())
    return bool(enabled)


def json_bytes(value: Any) -> float:
    """一个值的 JSON 文本 UTF-8 字节数；序列化失败返回 `math.inf`（超任何上限）。"""
    try:
        text = _json_text(value)
    except (TypeError, ValueError):
        # 无法序列化的值无法被任何请求承载。
        return math.inf
    return len(text.encode("utf-8"))


def _json_text(value: Any) -> str:
    import json
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def wire_surface_op(op: Any) -> Any:
    if op == "append":
        return "append"
    return {"op": "replace", "startSeq": int(op["startSeq"]), "endSeq": int(op["endSeq"])}


def wire_header(session: Any, session_format_version: int = SESSION_FORMAT_VERSION) -> dict:
    """把逻辑会话元数据翻译为外部请求字段（对齐上游 wireHeader）。"""
    meta = getattr(session, "meta", {}) or {}
    header: dict = {
        "version": session_format_version,
        "id": session.session_id,
        "createdAt": int(session.created_at),
    }
    if meta.get("cwd") is not None:
        header["cwd"] = meta["cwd"]
    if meta.get("parentSession") is not None:
        header["parentSession"] = meta["parentSession"]
    if getattr(session, "is_seeded", False):
        header["seedLength"] = int(session.inherited_event_count)
    if meta.get("origin") is not None:
        header["origin"] = meta["origin"]
    if meta.get("delegationDepth") is not None:
        header["delegationDepth"] = meta["delegationDepth"]
    if meta.get("agentPreset") is not None:
        header["agentPreset"] = meta["agentPreset"]
    return header


def wire_event(event: Mapping[str, Any]) -> dict:
    """把一条 canonical 事件翻译为原始 JSON 请求字段（对齐上游 wireEvent）。"""
    common: dict = {
        "seq": int(event["seq"]),
        "time": int(event["time"]),
        "data": thaw(event.get("data")),
    }
    if event.get("ignorable") is not None:
        common["ignorable"] = event["ignorable"]
    etype = event["type"]
    if etype in _SOURCE_SURFACE_TYPES:
        result = {
            **common, "type": etype,
            "surfaceOp": wire_surface_op(event.get("surfaceOp")),
        }
        sources = event.get("sourceEventSeqs")
        if sources is not None:
            result["sourceEventSeqs"] = [int(seq) for seq in sources]
        return result
    if etype == "assistant/message":
        return {**common, "type": etype, "surfaceOp": wire_surface_op(event.get("surfaceOp"))}
    if etype not in KNOWN_TYPES and event.get("ignorable") is True:
        result = {**common, "type": etype, "ignorable": True}
        if "surfaceOp" in event:
            result["surfaceOp"] = thaw(event["surfaceOp"])
        if "sourceEventSeqs" in event:
            result["sourceEventSeqs"] = thaw(event["sourceEventSeqs"])
        return result
    return {**common, "type": etype}


def fold_accepted_through(
    events: list[Mapping[str, Any]],
    session_id: str,
    session_format_version: int,
    through_seq: int = -1,
) -> int:
    """折叠一批事件的交付确认水位（对齐上游 acceptedThrough 的逐条校验）。

    只认与当前会话、当前格式代一致的 `delivery-accepted` 事件；畸形载荷直接
    fail loud（不静默跳过）。
    """
    for event in events:
        if event.get("type") != "session-log-deepseek/delivery-accepted":
            continue
        seq = event.get("seq")
        data = event.get("data")
        if not isinstance(data, Mapping):
            raise RuntimeError(
                f"session-log-deepseek: malformed acceptance watermark at seq {seq}")
        accepted_format = data.get("sessionFormatVersion", 0)
        if (not isinstance(accepted_format, int) or isinstance(accepted_format, bool)
                or accepted_format < 0):
            raise RuntimeError(
                f"session-log-deepseek: malformed acceptance format version at seq {seq}")
        if accepted_format != session_format_version:
            continue
        accepted_session = data.get("sessionId")
        accepted_seq = data.get("throughSeq")
        if (not isinstance(accepted_session, str) or accepted_session == ""
                or not isinstance(accepted_seq, int) or isinstance(accepted_seq, bool)
                or accepted_seq < 0 or not isinstance(seq, int) or accepted_seq >= seq):
            raise RuntimeError(
                f"session-log-deepseek: malformed acceptance watermark at seq {seq}")
        if accepted_session != session_id:
            continue
        if accepted_seq > through_seq:
            through_seq = accepted_seq
    return through_seq


def accepted_through(session: Any, session_format_version: int = SESSION_FORMAT_VERSION) -> int:
    """本会话当前格式代的最高确认序号（无确认则 -1）。只折叠缓存之后的新事件。"""
    previous = _acceptance_folds.get(session)
    through_seq = previous["throughSeq"] if previous is not None else -1
    scanned = previous["scannedEvents"] if previous is not None else 0
    length = session.seq
    events = []
    for index in range(scanned, length):
        try:
            event = session.event_at(index)
        except IndexError:
            raise RuntimeError(
                f"session-log-deepseek: missing event {index} below captured length {length}"
            ) from None
        events.append(event)
    through_seq = fold_accepted_through(
        events, session.session_id, session_format_version, through_seq)
    _acceptance_folds[session] = {"scannedEvents": length, "throughSeq": through_seq}
    return through_seq


def prepare_field(
    session: Any,
    config: dict | None,
    *,
    logger: Any = None,
    after_seq: int | None = None,
    session_format_version: int = SESSION_FORMAT_VERSION,
) -> dict | None:
    """每个请求现读 `enabled` 的入口：禁用则不带字段；否则走字节前缀算法。"""
    if not is_enabled(config or {}):
        return None
    resolved = resolve_config(config)
    return prepare(
        session, resolved["maxBytes"], session_format_version=session_format_version,
        logger=logger, after_seq=after_seq)


def prepare(
    session: Any,
    max_bytes: int,
    *,
    session_format_version: int = SESSION_FORMAT_VERSION,
    logger: Any = None,
    after_seq: int | None = None,
) -> dict | None:
    """构造本请求的 `dsh_session_log` 字段（最长可行 pending 前缀）。

    @returns: `{"value": <字段>, "accept": <回调>}`，或 `None`（不携带字段）。
    """
    resolved_after = accepted_through(session, session_format_version) \
        if after_seq is None else after_seq
    envelope = {
        "version": 1,
        "sessionFormatVersion": session_format_version,
        "session": wire_header(session, session_format_version),
        "afterSeq": int(resolved_after),
    }
    pending = list(session.snapshot_events(resolved_after + 1))
    # 字段字节（不含 events 与 throughSeq 位宽）；各候选前缀自行累加。
    base = json_bytes({**envelope, "throughSeq": 0, "events": []}) - 1
    events: list[dict] = []
    candidate_bytes: float = 0.0
    for event in pending:
        wire = wire_event(event)
        nxt = base + (0 if not events else 1) + json_bytes(wire)
        candidate_bytes = nxt + len(str(event["seq"]))
        if candidate_bytes > max_bytes:
            break
        base = nxt
        events.append(wire)
    if not events:
        first = pending[0] if pending else None
        if first is not None:
            _warn_oversized(logger, session, first, candidate_bytes, max_bytes)
        return None
    through_seq = pending[len(events) - 1]["seq"]
    value = {**envelope, "throughSeq": int(through_seq), "events": events}

    def accept() -> None:
        session.append("session-log-deepseek/delivery-accepted", {
            "sessionId": session.session_id,
            "sessionFormatVersion": session_format_version,
            "throughSeq": through_seq,
        })

    return {"value": value, "accept": accept}


def _warn_oversized(logger: Any, session: Any, first: Mapping[str, Any],
                    candidate_bytes: float, max_bytes: int) -> None:
    if logger is None:
        return
    seq = str(first["seq"])
    if math.isfinite(candidate_bytes):
        logger.warn(
            f'session-log-deepseek: event {seq} of session "{session.session_id}" needs a '
            f'{int(candidate_bytes)}-byte dsh_session_log field, above maxBytes {max_bytes}; '
            f"this session's upload stays at event {seq} until maxBytes admits it")
    else:
        logger.warn(
            f'session-log-deepseek: event {seq} of session "{session.session_id}" is too large '
            f"to serialize into a dsh_session_log field; this session's upload stays at event {seq}")
