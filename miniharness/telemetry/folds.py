"""会话统计 + 用量统计：纯 fold（对照上游 session-stats / token-meter）。

对应真实源码：
  * `packages/session/session-stats/src/projection.ts`（sessionStats 投影单元）
  * `packages/llm/token-meter/src/usage-projection.ts`（tokenUsage 投影单元）
  * `packages/llm/token-meter/src/turn-usage.ts`（deriveTurnTokenUsage）

本模块只做「逐事件折叠 + 视图投影」，不产生会话事件、不做持久化、不感知
web/CLI 载体（L2 纯领域逻辑）。折叠语义完全对齐上游：

  * sessionStats：以 `step/end` 计步（不是 assistant/message——它是 step 生命
    周期权威：completed / failed / cancelled / max-tokens step 各落一条，finalize
    必然落日志）。模型时长 = `step/start` → `assistant/message`；首令牌 = 流内首个
    非空 delta chunk，且在 step 内 `llm/retry` 后依旧存活；decode 跨首令牌 →
    `assistant/message`，仅在同一 step 也上报 outputTokens 时计时；tool 时长 =
    `tool/call` → `tool/result` 按 callId 配对。cancelled step 不组装 message，
    其部分流时长不计入任何时长项（同上游窗口折叠）。
  * tokenUsage：每一条 durable Assistant settlement 贡献流内最后一条 usage 样本
    （assistant/message 优先 data.usage，否则与 attempt 一样只扫流）。`llm/retry-
    started` 关闭替换槽使重试 attempt 累加总量（replace-slot 语义）。桶相等 /
    非相关事件 → 状态原样（对齐上游 Object.is 变更门）。
  * derive_turn_token_usage：一个完整 Turn 的 attempt 生命周期精确归账。任何缺失
    的边界 / 不完整 usage / 不安全计数 / 矛盾 exact total 都使整段披露不可用
    （fail-closed 返回 None），不从样本推断 attempt。

fold 状态是普通 dict（可持久化前提）；wire view 是状态的严格子集（校验八键 /
四键）。逐字段语义与上游 zod schema 完全一致。
"""
from __future__ import annotations

from typing import Any

from ..core.session import derive_event_message
from ..llm.assistant_stream import expand_assistant_stream

__all__ = [
    "derive_turn_token_usage",
    "init_session_stats",
    "fold_session_stats",
    "session_stats_view",
    "init_token_usage",
    "fold_token_usage",
    "token_usage_view",
]


# ---------- 共享小工具 ----------

def _is_positive_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_finite_nonneg(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0


def _ceiling(value: float) -> int:
    return max(0, value)


def _safe_sum(values: list[int]) -> int | None:
    """按上游 safeSum：任一中间和越安全整数界 → undefined。"""
    total = 0
    for value in values:
        total += value
        if total > 2 ** 53 - 1:
            return None
    return total


# ---------- sessionStats：isTokenDelta / firstTokenTime ----------

def _is_token_delta(chunk: dict) -> bool:
    """上游 isTokenDelta（projection.ts:33-43）：非空首令牌 delta。"""
    ctype = chunk.get("type")
    if ctype in ("text-delta", "reasoning-delta"):
        return chunk.get("text", "") != ""
    if ctype == "tool-call-delta":
        return chunk.get("argumentsDelta", "") != "" or chunk.get("name") is not None
    return False


def _first_token_time(stream) -> int | None:
    """上游 firstTokenTime（projection.ts:46-48）：流内首个非空令牌时间戳。"""
    if not stream:
        return None
    for member in expand_assistant_stream(stream):
        if _is_token_delta(member.chunk):
            return member.time
    return None


def _usage_output_tokens(usage: Any) -> int | None:
    """上游 usageOutputTokens（projection.ts:127-131）：合法输出令牌或 None。"""
    if not isinstance(usage, dict):
        return None
    value = usage.get("outputTokens")
    return value if _is_finite_nonneg(value) else None


# ---------- sessionStats fold ----------

def init_session_stats() -> dict:
    """上游 init()（projection.ts:138-150）。"""
    return {
        "turns": 0,
        "steps": 0,
        "llmMs": 0,
        "toolMs": 0,
        "ttftMs": 0,
        "ttftSteps": 0,
        "decodeMs": 0,
        "decodeTokens": 0,
        "lastTurn": None,
        "openStep": None,
        "pendingCalls": {},
    }


def fold_session_stats(state: dict, event: dict) -> dict:
    """逐事件推进 sessionStats fold；无变化事件返回原状态（对齐 apply 语义）。"""
    etype = event["type"]
    data = event["data"]
    try:
        if etype == "step/start":
            return {
                **state,
                "openStep": {
                    "turn": data["turn"], "step": data["step"],
                    "startTime": event["time"], "firstTokenTime": None,
                },
            }
        if etype == "assistant/attempt":
            open_step = state["openStep"]
            if (open_step is None or open_step["turn"] != data.get("turn")
                    or open_step["step"] != data.get("step")):
                return state
            first = _first_token_time(data.get("stream"))
            if open_step["firstTokenTime"] is not None or first is None:
                return state
            return {**state, "openStep": {**open_step, "firstTokenTime": first}}
        if etype == "assistant/message":
            open_step = state["openStep"]
            if (open_step is None or open_step["turn"] != data.get("turn")
                    or open_step["step"] != data.get("step")):
                return state
            first_token = open_step["firstTokenTime"] if open_step["firstTokenTime"] is not None \
                else _first_token_time(data.get("stream"))
            next_state = {
                **state,
                "llmMs": state["llmMs"] + _ceiling(event["time"] - open_step["startTime"]),
                "openStep": None,
            }
            if first_token is not None:
                next_state["ttftMs"] += _ceiling(first_token - open_step["startTime"])
                next_state["ttftSteps"] += 1
                output_tokens = _usage_output_tokens(data.get("usage"))
                if output_tokens is not None:
                    next_state["decodeMs"] += _ceiling(event["time"] - first_token)
                    next_state["decodeTokens"] += output_tokens
            return next_state
        if etype == "tool/call":
            pending = dict(state["pendingCalls"])
            pending[str(data["callId"])] = event["time"]
            return {**state, "pendingCalls": pending}
        if etype == "tool/result":
            call_id = _result_call_id(data)
            if call_id not in state["pendingCalls"]:
                return state
            dispatched = state["pendingCalls"][call_id]
            pending = {k: v for k, v in state["pendingCalls"].items() if k != call_id}
            return {
                **state,
                "toolMs": state["toolMs"] + _ceiling(event["time"] - dispatched),
                "pendingCalls": pending,
            }
        if etype == "step/end":
            return {
                **state,
                "turns": state["turns"] if state["lastTurn"] == data.get("turn") else state["turns"] + 1,
                "steps": state["steps"] + 1,
                "lastTurn": data.get("turn"),
                "openStep": None,
            }
        if etype == "turn/end":
            return state if not state["pendingCalls"] else {**state, "pendingCalls": {}}
    except (KeyError, TypeError, IndexError, ValueError):
        raise
    return state


def _result_call_id(data: dict) -> str:
    """tool/result 的 provider-minted callId（message.source.callId）。

    对齐上游 projection.ts:195-196：result 的 callId 从 message.source 读，
    不受 prototype 属性名 ('constructor', 'toString') 污染——own-key 检查
    回头用 dict 的 in 即显式键查找（不继承）。
    """
    message = derive_event_message({"type": "tool/result", "data": data})
    if message is None:
        raise ValueError("tool/result event carries no message")
    source = message.get("source") or {}
    call_id = source.get("callId")
    if not isinstance(call_id, str):
        raise ValueError("tool/result message.source.callId must be a string")
    return call_id


def session_stats_view(state: dict) -> dict:
    """wire 视图：严格八键（projection.ts:220-232）。"""
    return {
        "turns": state["turns"],
        "steps": state["steps"],
        "llmMs": state["llmMs"],
        "toolMs": state["toolMs"],
        "ttftMs": state["ttftMs"],
        "ttftSteps": state["ttftSteps"],
        "decodeMs": state["decodeMs"],
        "decodeTokens": state["decodeTokens"],
    }


# ---------- tokenUsage fold ----------

def _zero_buckets() -> dict:
    return {"uncachedInputTokens": 0, "outputTokens": 0,
            "cacheReadTokens": 0, "cacheWriteTokens": 0}


def _buckets_from(usage: dict) -> dict:
    return {
        "uncachedInputTokens": usage.get("inputTokens", 0),
        "outputTokens": usage.get("outputTokens", 0),
        "cacheReadTokens": usage.get("cacheReadTokens") or 0,
        "cacheWriteTokens": usage.get("cacheWriteTokens") or 0,
    }


def _buckets_equal(left: dict, right: dict) -> bool:
    return (left["uncachedInputTokens"] == right["uncachedInputTokens"]
            and left["outputTokens"] == right["outputTokens"]
            and left["cacheReadTokens"] == right["cacheReadTokens"]
            and left["cacheWriteTokens"] == right["cacheWriteTokens"])


def _add_replacing(totals: dict, previous: dict | None, next_: dict) -> dict:
    prev = previous or _zero_buckets()
    return {
        "uncachedInputTokens": totals["uncachedInputTokens"] - prev["uncachedInputTokens"] + next_["uncachedInputTokens"],
        "outputTokens": totals["outputTokens"] - prev["outputTokens"] + next_["outputTokens"],
        "cacheReadTokens": totals["cacheReadTokens"] - prev["cacheReadTokens"] + next_["cacheReadTokens"],
        "cacheWriteTokens": totals["cacheWriteTokens"] - prev["cacheWriteTokens"] + next_["cacheWriteTokens"],
    }


def init_token_usage() -> dict:
    return {"totals": _zero_buckets(), "last": None}


def _usage_of(event: dict) -> dict | None:
    """上游 usageOf（usage-projection.ts:82-89）。"""
    data = event["data"]
    if event["type"] == "assistant/message" and data.get("usage") is not None:
        return data["usage"]
    if event["type"] not in ("assistant/message", "assistant/attempt"):
        return None
    last = None
    for member in expand_assistant_stream(data.get("stream")):
        if member.chunk.get("type") == "usage":
            last = member.chunk.get("usage")
    return last


def fold_token_usage(state: dict, event: dict) -> dict:
    """逐事件推进 tokenUsage fold（usage-projection.ts:125-151）。"""
    etype = event["type"]
    data = event["data"]
    if etype == "llm/retry-started":
        last = state["last"]
        if (last is not None and last["turn"] == data.get("turn")
                and last["step"] == data.get("step")):
            return {**state, "last": None}
        return state
    if etype not in ("assistant/message", "assistant/attempt"):
        return state
    sample = _usage_of(event)
    if sample is None:
        return state
    turn, step = data.get("turn"), data.get("step")
    buckets = _buckets_from(sample)
    last = state["last"]
    previous = last["buckets"] if (last is not None and last["turn"] == turn
                                   and last["step"] == step) else None
    if previous is not None and _buckets_equal(previous, buckets):
        return state
    return {
        "totals": _add_replacing(state["totals"], previous, buckets),
        "last": {"turn": turn, "step": step, "buckets": buckets},
    }


def token_usage_view(state: dict) -> dict:
    """wire 视图：四桶 totals（usage-projection.ts:152）。"""
    return dict(state["totals"])


# ---------- deriveTurnTokenUsage ----------

def _is_count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _message_route(message: dict) -> dict | None:
    """上游 messageRoute（turn-usage.ts:72-75）。"""
    source = message.get("source") or {}
    provider, model = source.get("provider") or "", source.get("model") or ""
    return {"provider": provider, "model": model} if provider and model else None


def _stream_usage(stream) -> dict | None:
    sample = None
    for member in expand_assistant_stream(stream):
        if member.chunk.get("type") == "usage":
            sample = member.chunk.get("usage")
    return sample


def _normalize_usage(usage: dict, route: dict | None) -> dict | None:
    """上游 normalizeUsage（turn-usage.ts:85-128）。"""
    input_tokens, output_tokens = usage.get("inputTokens"), usage.get("outputTokens")
    if not _is_count(input_tokens) or not _is_count(output_tokens):
        return None
    cache_read, cache_write = usage.get("cacheReadTokens"), usage.get("cacheWriteTokens")
    if cache_read is not None and not _is_count(cache_read):
        return None
    if cache_write is not None and not _is_count(cache_write):
        return None
    reasoning = usage.get("reasoningTokens")
    if reasoning is not None and (not _is_count(reasoning) or reasoning > output_tokens):
        return None

    known_prompt = _safe_sum([
        input_tokens,
        *([] if cache_read is None else [cache_read]),
        *([] if cache_write is None else [cache_write]),
    ])
    if known_prompt is None:
        return None

    total_tokens = usage.get("totalTokens")
    if total_tokens is not None:
        if not _is_count(total_tokens):
            return None
        exact_prompt = total_tokens - output_tokens
        if not _is_count(exact_prompt) or exact_prompt < known_prompt:
            return None
        if (cache_read is not None and cache_write is not None
                and exact_prompt != known_prompt):
            return None
        exact_total = total_tokens
    else:
        if cache_read is None or cache_write is None:
            return None
        derived = _safe_sum([known_prompt, output_tokens])
        if derived is None:
            return None
        exact_total = derived

    normalized = {
        "inputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": exact_total,
    }
    if cache_read is not None:
        normalized["cacheReadTokens"] = cache_read
    if cache_write is not None:
        normalized["cacheWriteTokens"] = cache_write
    if reasoning is not None:
        normalized["reasoningTokens"] = reasoning
    if route is not None:
        normalized["route"] = route
    return normalized


def _aggregate_attempts(attempts: list[dict]) -> dict | None:
    """上游 aggregateAttempts（turn-usage.ts:130-163）。"""
    if not attempts:
        return None
    input_tokens = _safe_sum([a["inputTokens"] for a in attempts])
    output_tokens = _safe_sum([a["outputTokens"] for a in attempts])
    total_tokens = _safe_sum([a["totalTokens"] for a in attempts])
    if input_tokens is None or output_tokens is None or total_tokens is None:
        return None

    def all_sum(key: str) -> int | None:
        values = [a[key] for a in attempts if key in a]
        if len(values) != len(attempts):
            return None
        return _safe_sum(values) if all(_is_count(v) for v in values) else None

    cache_read = all_sum("cacheReadTokens")
    cache_write = all_sum("cacheWriteTokens")
    reasoning = all_sum("reasoningTokens")

    routes: list[dict] | None = None
    attributed = [a.get("route") for a in attempts]
    if all(r is not None for r in attributed):
        seen: dict[tuple[str, str], dict] = {}
        for route in attributed:
            key = (route["provider"], route["model"])
            seen.setdefault(key, route)
        routes = list(seen.values())

    result = {
        "uncachedInputTokens": input_tokens,
        "outputTokens": output_tokens,
        "totalTokens": total_tokens,
    }
    if cache_read is not None:
        result["cacheReadTokens"] = cache_read
    if cache_write is not None:
        result["cacheWriteTokens"] = cache_write
    if reasoning is not None:
        result["reasoningTokens"] = reasoning
    if routes is not None:
        result["routes"] = routes
    return result


def derive_turn_token_usage(events: list[dict]) -> dict | None:
    """上游 deriveTurnTokenUsage（turn-usage.ts:182-281）完整状态机。"""
    state: dict | None = {"kind": "idle"}
    attempts: list[dict] = []
    turn: int | None = None
    saw_end = False
    invalid = False

    def close_open(route: dict | None = None) -> bool:
        nonlocal state
        if state is None or state["kind"] != "open" or state.get("sample") is None:
            return False
        normalized = _normalize_usage(state["sample"], route)
        if normalized is None:
            return False
        attempts.append(normalized)
        return True

    for event in events:
        if invalid:
            break
        etype = event["type"]
        data = event["data"]
        if etype == "turn/start":
            if turn is not None or state is None or state["kind"] != "idle":
                invalid = True
            else:
                turn = data.get("turn")
            continue
        if turn is None:
            invalid = True
            break
        if etype == "turn/end":
            if data.get("turn") != turn or state is None or state["kind"] != "idle" or saw_end:
                invalid = True
            else:
                saw_end = True
            continue
        if saw_end:
            invalid = True
            break
        if etype == "step/start":
            if data.get("turn") != turn or state is None or state["kind"] != "idle":
                invalid = True
            else:
                state = {"kind": "open", "turn": turn, "step": data.get("step")}
            continue
        if etype == "llm/retry-started":
            if (data.get("turn") != turn or state is None
                    or state["kind"] != "settled" or state["by"] != "retry"
                    or state["turn"] != data.get("turn") or state["step"] != data.get("step")):
                invalid = True
            else:
                state = {"kind": "open", "turn": turn, "step": data.get("step")}
            continue
        if etype == "assistant/attempt":
            if (data.get("turn") != turn or state is None or state["kind"] != "open"
                    or state["turn"] != data.get("turn") or state["step"] != data.get("step")):
                invalid = True
                continue
            sample = state.get("sample")
            for member in expand_assistant_stream(data.get("stream")):
                if member.chunk.get("type") == "usage":
                    sample = member.chunk.get("usage")
            state = {"kind": "open", "turn": turn, "step": data.get("step"),
                     **({} if sample is None else {"sample": sample})}
            if not close_open():
                invalid = True
            elif state is not None:
                state["kind"] = "finishClosed"
            continue
        if etype == "assistant/message":
            if (data.get("turn") != turn or state is None or state["kind"] != "open"
                    or state["turn"] != data.get("turn") or state["step"] != data.get("step")):
                invalid = True
                continue
            sample = data.get("usage")
            if sample is None:
                sample = _stream_usage(data.get("stream"))
            if sample is not None:
                state = {**state, "sample": sample}
            message = data.get("message")
            route = None if not isinstance(message, dict) else _message_route(message)
            if not close_open(route):
                invalid = True
            elif state is not None:
                state = {"kind": "settled", "turn": turn, "step": data.get("step"), "by": "message"}
            continue
        if etype == "llm/retry":
            if (data.get("turn") != turn or state is None or state["kind"] == "idle"
                    or state["turn"] != data.get("turn") or state["step"] != data.get("step")):
                invalid = True
                continue
            if state["kind"] == "settled" or (state["kind"] == "open" and not close_open()):
                invalid = True
            if not invalid:
                state = {"kind": "settled", "turn": turn, "step": data.get("step"), "by": "retry"}
            continue
        if etype == "step/end":
            if (data.get("turn") != turn or state is None or state["kind"] == "idle"
                    or state["turn"] != data.get("turn") or state["step"] != data.get("step")):
                invalid = True
                continue
            if state["kind"] == "open" and not close_open():
                invalid = True
            if not invalid:
                state = {"kind": "idle"}

    if invalid or not saw_end or state is not None and state["kind"] != "idle":
        return None
    return _aggregate_attempts(attempts)