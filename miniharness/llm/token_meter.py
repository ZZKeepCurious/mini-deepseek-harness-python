"""token 计量：单一 replay-aware 计量服务（请求压力 + surface 定价）。

上游对照：packages/llm/token-meter/src（index.ts + estimate.ts + surface-fold.ts）。
服务自身不产生任何会话事件，对既有事件流做增量 fold（logRevision = consumedEvents），
提供请求压力与 surface 快照两个测量面。

已核实事实（上游 index.ts）：
  * usage 折入锚：最新成功请求的 usage（assistant/message.usage，经 chunk 早样本
    重装的 provider 输出）仅在其 canonical 信封与当前 request/header 一致且总数
    ≥ 该请求的完整启发式锚价时复用（baseline.kind='usage'），否则整体启发式重估
    （baseline.kind='estimated'）；锚定后 surface 增量 delta 追加进 totalTokens。
  * estimate（estimate.ts）：4 字符/token 固定密度 + 每块 4 token 结构开销 +
    每消息 4 token 角色开销。

mini 简化标注：
  * 上游 EpochHeader 含 system/tools（可估 header 开销）；mini 的 request/header
    形状为 {header:{config, system?, tools?},reason}（见 AGENTS.md 差异清单），
    estimate_header 定价 system + tools 启发式开销（config 不计价，同上游）。
  * 上游 usage 锚同时计入 cacheRead/cacheWrite；mini 的 TokenUsage 归一同样
    携带 inputTokens/outputTokens/cacheReadTokens/cacheWriteTokens，此处逐项相加。
"""
from __future__ import annotations

import json
from types import MappingProxyType
from typing import Any

from ..core.session import SURFACE_TYPES, derive_event_message
from .protocol import BlockAssembler

__all__ = ["BLOCK_OVERHEAD", "CHARS_PER_TOKEN", "ROLE_OVERHEAD", "TokenMeter",
           "estimate_content", "estimate_header", "estimate_message",
           "estimate_system_tokens", "estimate_tools_tokens"]

# ---------- 启发式定价（estimate.ts） ----------

CHARS_PER_TOKEN = 4
BLOCK_OVERHEAD = 4
ROLE_OVERHEAD = 4


def estimate_content(blocks) -> int:
    """按固定密度递归定价内容块（含每块结构开销；未知块按 JSON 长度保守定价）。"""
    tokens = 0
    for block in blocks:
        btype = block.get("type")
        if btype in ("text", "reasoning"):
            tokens += _ceil_len(block.get("text", "")) + BLOCK_OVERHEAD
        elif btype == "tool-call":
            tokens += _ceil_len(block.get("name", "")) + _ceil_len(block.get("arguments", "")) + BLOCK_OVERHEAD
        elif btype == "tool-result":
            tokens += estimate_content(block.get("content", [])) + BLOCK_OVERHEAD
        else:
            tokens += BLOCK_OVERHEAD + _ceil_len(str(block))
    return tokens


def estimate_message(message) -> int:
    """定价一条模型可见消息（内容 + 角色开销）。"""
    return estimate_content(message.get("content", [])) + ROLE_OVERHEAD


def estimate_system_tokens(header) -> int:
    """定价请求信封的 system 部分（上游 estimate.ts estimateSystemTokens）：
    无 system 字段 → 0；否则 ceil(len/4) + ROLE_OVERHEAD。"""
    if header is None or header.get("system") is None:
        return 0
    return _ceil_len(header["system"]) + ROLE_OVERHEAD


def estimate_tools_tokens(header) -> int:
    """定价请求信封的 tools 部分（上游 estimate.ts estimateToolsTokens）：
    无 tools 或空列表 → 0；否则 ceil(JSON长度/4) + BLOCK_OVERHEAD。"""
    tools = (header or {}).get("tools") or []
    if not tools:
        return 0
    return _ceil_len(json.dumps(tools, ensure_ascii=False, sort_keys=True)) + BLOCK_OVERHEAD


def estimate_header(header) -> int:
    """定价请求信封的非 surface 部分（上游 estimate.ts estimateHeader）：
    system + tools 启发式开销，config 不计价。"""
    return estimate_system_tokens(header) + estimate_tools_tokens(header)


def _ceil_len(text: Any) -> int:
    import math
    return math.ceil(len(str(text)) / CHARS_PER_TOKEN)


# ---------- surface fold（surface-fold.ts） ----------

def _fold_surface_tokens(nodes: list[dict], ev: dict):
    """把一个 surface 事件折入定价 surface：返回 (自身价, 新 nodes, 增量)。"""
    message = derive_event_message(ev)
    tokens = 0 if message is None else estimate_message(message)
    op = ev.get("surfaceOp")
    if op == "append":
        return tokens, nodes + [{"seq": ev["seq"], "tokens": tokens}], tokens
    start, end = op["start"], op["end"]
    start_idx = next((i for i, n in enumerate(nodes) if n["seq"] == start), None)
    end_idx = next((i for i, n in enumerate(nodes) if n["seq"] == end), None)
    if start_idx is None or end_idx is None or start_idx > end_idx:
        raise ValueError(
            f"token surface: replace at seq {ev['seq']} 区间 {start}-{end} 非法（不在 surface 或顺序颠倒）"
        )
    removed = sum(n["tokens"] for n in nodes[start_idx:end_idx + 1])
    next_nodes = nodes[:start_idx] + [{"seq": ev["seq"], "tokens": tokens}] + nodes[end_idx + 1:]
    return tokens, next_nodes, tokens - removed


def _usage_tokens(usage: dict) -> int:
    """usage 各桶求和（不重复计 reasoning 输出；对齐上游 usageTokens）。"""
    return (usage.get("inputTokens", 0)
            + (usage.get("cacheReadTokens") or 0)
            + (usage.get("cacheWriteTokens") or 0)
            + usage.get("outputTokens", 0))


def _header_equals(left, right) -> bool:
    if left is None or right is None:
        return left is right
    return dict(left.get("header", {})) == dict(right.get("header", {}))


def _optional_header_equals(left, right) -> bool:
    if left is None or right is None:
        return left is right
    return _header_equals(left, right)


class TokenMeter:
    """单一 replay-aware 计量器：per-session 增量 fold + usage 折入锚。

    用法（对齐上游 index.ts measure）：
      meter.measure(session) → {logRevision, baseline, surfaceDeltaTokens,
      totalTokens, surfaceTokens, nodes}
    """

    def __init__(self):
        self._states: dict[str, dict] = {}

    def measure(self, session) -> dict:
        """测量当前请求压力与 surface（纯读，不产生会话事件）。"""
        state = self._sync(session)
        header = state["header"]
        anchor = state["anchor"]
        if anchor is not None and _optional_header_equals(anchor["header"], header):
            baseline = anchor["baseline"]
            surface_delta = state["surfaceTokens"] - anchor["surfaceTokens"]
        elif header is None and state["surfaceTokens"] == 0:
            baseline = {"kind": "none", "tokens": 0}
            surface_delta = 0
        else:
            baseline = {"kind": "estimated", "tokens": estimate_header(header) + state["surfaceTokens"]}
            surface_delta = 0
        return {
            "logRevision": state["consumedEvents"],
            "baseline": baseline,
            "surfaceDeltaTokens": surface_delta,
            "totalTokens": max(0, baseline["tokens"] + surface_delta),
            "surfaceTokens": state["surfaceTokens"],
            "nodes": list(state["surface"]),
        }

    def estimate_message(self, message) -> int:
        """启发式定价一条模型可见消息（实例面）。"""
        return estimate_message(message)

    # ---------- 增量 fold ----------

    def _sync(self, session) -> dict:
        key = session.session_id
        state = self._states.get(key)
        if state is None:
            state = {
                "consumedEvents": 0,
                "header": None,
                "surface": [],
                "surfaceTokens": 0,
                "stepStart": None,
                "anchor": None,
            }
            self._states[key] = state
        while state["consumedEvents"] < len(session.events):
            self._fold_event(session, state, session.events[state["consumedEvents"]])
            state["consumedEvents"] += 1
        return state

    def _fold_event(self, session, state: dict, ev: dict) -> None:
        """逐事件推进 fold；先验证再落地，坏事件每次重试都同样失败（对齐上游）。"""
        next_header = state["header"]
        next_step_start = state["stepStart"]
        next_anchor = state["anchor"]

        etype = ev["type"]
        if etype == "request/header":
            next_header = dict(ev["data"].get("header", {}))
        elif etype == "step/start":
            if state["stepStart"] is not None:
                raise ValueError(
                    f"token meter: step/start at seq {ev['seq']} 在 turn {state['stepStart']['turn']}"
                    f"/step {state['stepStart']['step']} 结束前到达"
                )
            next_step_start = {**dict(ev["data"]), "surfaceTokens": state["surfaceTokens"]}
        elif etype == "step/end":
            if (state["stepStart"] is None
                    or state["stepStart"]["turn"] != ev["data"].get("turn")
                    or state["stepStart"]["step"] != ev["data"].get("step")):
                raise ValueError(f"token meter: step/end at seq {ev['seq']} 无匹配 step/start")
            next_step_start = None

        surface = None
        if is_surface_event(ev):
            surface = _fold_surface_tokens(state["surface"], ev)

        if etype == "assistant/message":
            step_start = state["stepStart"]
            if (step_start is None
                    or step_start["turn"] != ev["data"].get("turn")
                    or step_start["step"] != ev["data"].get("step")):
                raise ValueError(
                    f"token meter: assistant/message at seq {ev['seq']} 无匹配 step/start"
                )
            event_tokens = surface[0]
            usage = ev["data"].get("usage")
            if usage is not None and next_header is not None:
                provider_assistant = self._estimate_provider_assistant(session, ev, event_tokens)
                anchor_surface = step_start["surfaceTokens"] + provider_assistant
                provider_tokens = _usage_tokens(usage)
                estimated_anchor = estimate_header(next_header) + anchor_surface
                next_anchor = {
                    "header": next_header,
                    "surfaceTokens": anchor_surface,
                    "baseline": (
                        {"kind": "usage", "tokens": provider_tokens, "usage": dict(usage)}
                        if provider_tokens >= estimated_anchor
                        else {"kind": "estimated", "tokens": estimated_anchor}
                    ),
                }
            else:
                anchor_surface = step_start["surfaceTokens"] + event_tokens
                next_anchor = {
                    "header": next_header,
                    "surfaceTokens": anchor_surface,
                    "baseline": {
                        "kind": "estimated",
                        "tokens": estimate_header(next_header) + anchor_surface,
                    },
                }

        state["header"] = next_header
        state["stepStart"] = next_step_start
        if surface is not None:
            state["surface"] = surface[1]
            state["surfaceTokens"] += surface[2]
        state["anchor"] = next_anchor

    def _estimate_provider_assistant(self, session, ev: dict, durable_event_tokens: int) -> int:
        """按 sourceEventSeqs 重装 provider 原始输出并定价（usage 锚的替代面）。"""
        source_seqs = ev.get("sourceEventSeqs")
        if source_seqs is None:
            return durable_event_tokens
        assembler = BlockAssembler()
        seen = set()
        events = session.events
        for seq in source_seqs:
            if seq >= ev["seq"]:
                raise ValueError(f"token meter: assistant/message at seq {ev['seq']} 引用后续 seq {seq}")
            if seq in seen:
                raise ValueError(f"token meter: assistant/message at seq {ev['seq']} 重复引用 seq {seq}")
            seen.add(seq)
            source = events[seq]
            if source["type"] != "assistant/chunk":
                raise ValueError(f"token meter: assistant/message at seq {ev['seq']} 引用非 chunk seq {seq}")
            if (source["data"].get("turn") != ev["data"].get("turn")
                    or source["data"].get("step") != ev["data"].get("step")):
                raise ValueError(f"token meter: assistant/message at seq {ev['seq']} 引用了别的 step 的 chunk")
            assembler.push(source["data"]["chunk"])
        blocks = assembler.blocks
        return 0 if not blocks else estimate_content(blocks) + ROLE_OVERHEAD


# 兼容冻结事件（MappingProxyType）与普通 dict
def is_surface_event(ev: dict) -> bool:
    """事件是否 surface 事件（类型合法 + 带 surfaceOp）。"""
    if ev["type"] not in SURFACE_TYPES:
        return False
    op = ev.get("surfaceOp")
    return op is not None