"""surface 保留区间选择 + 日志记录的压缩事务。

上游对照：packages/compaction/compaction-basic/src/region.ts（selectCompactableRange /
compactSurfaceRegion / validateSurfaceRegion / inspectCompactionEntryState）+
packages/compaction/compaction/src/tool-pairing.ts（配对平衡）。

事务顺序（对齐上游）：compaction/start 先落地为锁 → 摘要 → 校验区间仍稳定 →
compaction/summary → user/message 检查点（surfaceOp replace，sourceEventSeqs
覆盖 [startSeq, summarySeq, *shadowedSeqs]）→ compaction/end 释放锁；任何失败
路径恰好补一次 compaction/end（带 error），闭合失败留下可检测的孤儿锁。

mini 简化标注：同步模型下摘要期间无并发 surface 变更（工具不在 pre-step 执行），
稳定性校验保留但实际不会失败；崩溃孤儿锁可被 inspect 检出但 repair 不自动恢复。
"""
from __future__ import annotations

import uuid

from ..core.session import create_message, derive_event_message
from .summarizer import frame_summary, summarize_with_adapter

__all__ = ["compact_surface_region", "inspect_compaction_entry_state", "select_compactable_range"]


def select_compactable_range(session, measurement: dict, retain_tokens: int):
    """从头锚定的可压缩区间：保留定价的近期尾部，不切散工具调用/结果对。

    返回 {start, end}（当前 surface 上首/末被遮蔽节点的 seq），无安全区间返回 None。
    """
    priced = measurement["nodes"]
    if not priced:
        return None
    surface_nodes = session.surface_nodes()
    if len(surface_nodes) != len(priced) or any(
            n["seq"] != p["seq"] for n, p in zip(surface_nodes, priced)):
        raise ValueError("compaction: token-meter surface does not match the current session surface")

    accumulated = 0
    keep_from = len(priced)
    for index in range(len(priced) - 1, -1, -1):
        accumulated += priced[index]["tokens"]
        keep_from = index
        if accumulated >= retain_tokens:
            break
    if keep_from == 0:
        return None
    balance = _balance(session)
    while keep_from > 0:
        if balance[keep_from]:
            break
        keep_from -= 1
    if keep_from == 0:
        return None
    return {"start": surface_nodes[0]["seq"], "end": surface_nodes[keep_from - 1]["seq"]}


async def compact_surface_region(session, meter, agent, config: dict, start: int, end: int) -> dict:
    """在一个选定 surface 区间上跑完整压缩事务（start/end 为当前 surface 上的 seq）。

    返回压缩结果 {compactionId, startSeq, summarySeq, endSeq, summary,
    shadowedRange, shadowedSeqs, shadowedTokenCount}。async：摘要经适配器
    async 迭代器（asyncio 化重构后唯一形态）。
    """
    selection = _validate_surface_region(session, start, end)
    entry = inspect_compaction_entry_state(session.events)
    _assert_compaction_inactive(entry, "compaction")
    if entry["openTurn"] is None:
        raise RuntimeError("compactRegion: no open turn — automatic compaction events must be enclosed in a turn")
    owner = entry["openTurn"]

    compaction_id = str(uuid.uuid4())
    start_event = session.append("compaction/start", {
        "compactionId": compaction_id, "turn": owner,
    })
    failure = None
    closing = False
    try:
        measurement = meter.measure(session)
        selected = measurement["nodes"]
        current = session.surface_nodes()
        if len(selected) != len(current) or any(
                n["seq"] != c["seq"] for n, c in zip(selected, current)):
            raise ValueError("compaction: selected surface changed before summarization began")
        shadowed_tokens = sum(n["tokens"] for n in selected)
        input_ = _build_summarization_input(session, selection["shadowedSeqs"])
        summary_result = await summarize_with_adapter(agent, config, input_)

        checkpoint = create_message(
            "user",
            frame_summary(summary_result["summary"]),
            {"kind": "plugin", "plugin": "compact", "compactionId": compaction_id},
        )
        framed_tokens = meter.estimate_message(checkpoint)
        if framed_tokens >= shadowed_tokens:
            raise ValueError(
                f"summary is not smaller than the shadowed content "
                f"({framed_tokens} estimated framed tokens >= {shadowed_tokens})"
            )
        closing = True
        summary_event = session.append("compaction/summary", {
            "compactionId": compaction_id,
            "summary": summary_result["summary"],
            "shadowedRange": {"start": start, "end": end},
            "shadowedSeqs": list(selection["shadowedSeqs"]),
            "shadowedTokenCount": shadowed_tokens,
            "provider": summary_result["provider"],
            "model": summary_result["model"],
            "maxTokens": summary_result["maxTokens"],
            **({"usage": summary_result["usage"]} if summary_result.get("usage") is not None else {}),
        })
        session.append("user/message", checkpoint, surfaceOp={
            "op": "replace", "start": start, "end": end,
        }, sourceEventSeqs=[start_event["seq"], summary_event["seq"], *selection["shadowedSeqs"]])
        end_event = session.append("compaction/end", {
            "compactionId": compaction_id, "turn": owner,
        })
    except Exception as error:
        failure = error
        if not closing:
            try:
                session.append("compaction/end", {
                    "compactionId": compaction_id, "turn": owner, "error": str(error),
                })
            except Exception as close_error:  # noqa: BLE001 - 闭合失败留下可检测孤儿锁
                raise close_error from error
    if failure is not None:
        raise failure
    return {
        "compactionId": compaction_id,
        "startSeq": start_event["seq"],
        "summarySeq": summary_event["seq"],
        "endSeq": end_event["seq"],
        "summary": summary_result["summary"],
        "shadowedRange": {"start": start, "end": end},
        "shadowedSeqs": list(selection["shadowedSeqs"]),
        "shadowedTokenCount": shadowed_tokens,
    }


def inspect_compaction_entry_state(events) -> dict:
    """倒扫日志：打开中的 turn、未闭合 compaction/start、最新 end-seed 边界。

    对齐上游 region.ts inspectCompactionEntryState 的独立判定。
    """
    open_turn = None
    open_turn_known = False
    unmatched_start = None
    compaction_known = False
    latest_end_seed = None
    for ev in reversed(events):
        etype = ev["type"]
        if latest_end_seed is None and etype == "session/end-seed":
            latest_end_seed = ev["seq"]
        if not compaction_known:
            if etype == "compaction/start":
                unmatched_start = ev
                compaction_known = True
            elif etype == "compaction/end":
                compaction_known = True
        if not open_turn_known:
            if etype == "turn/start":
                open_turn = ev["data"].get("turn")
                open_turn_known = True
            elif etype == "turn/end":
                open_turn_known = True
        if open_turn_known and compaction_known and latest_end_seed is not None:
            break
    return {"openTurn": open_turn, "unmatchedCompactionStart": unmatched_start,
            "latestEndSeedSeq": latest_end_seed}


# ---------- 内部 ----------

def _assert_compaction_inactive(entry: dict, stage: str) -> None:
    """拒绝未闭合的压缩锁；end-seed 之后的孤儿 start 属于更早会话生命周期。"""
    unmatched = entry["unmatchedCompactionStart"]
    if unmatched is None:
        return
    latest_seed = entry["latestEndSeedSeq"]
    if latest_seed is not None and latest_seed > unmatched["seq"]:
        return
    raise RuntimeError(
        f"{stage}: compaction already in progress; the session compaction lock is already active"
    )


def _validate_surface_region(session, start: int, end: int) -> dict:
    """校验一个 surface-position 区间（seq 命名），边界必须配对平衡。"""
    nodes = session.surface_nodes()
    balance = _balance(session)
    start_idx = next((i for i, n in enumerate(nodes) if n["seq"] == start), None)
    end_idx = next((i for i, n in enumerate(nodes) if n["seq"] == end), None)
    if start_idx is None:
        raise ValueError(f"compactRegion: start seq {start} not found in surface")
    if end_idx is None:
        raise ValueError(f"compactRegion: end seq {end} not found in surface")
    if start_idx > end_idx:
        raise ValueError(
            f"compactRegion: start seq {start} (position {start_idx}) is after end seq {end} (position {end_idx})"
        )
    if not balance[start_idx]:
        raise ValueError(f"compactRegion: start seq {start} is not a balanced boundary (would split a step's tool-call/result pair)")
    if not balance[end_idx + 1]:
        raise ValueError(f"compactRegion: end seq {end} is not a balanced boundary (would split a step, or the step is still open)")
    return {"start": start, "end": end, "startIdx": start_idx, "endIdx": end_idx,
            "shadowedSeqs": [n["seq"] for n in nodes[start_idx:end_idx + 1]]}


def _build_summarization_input(session, shadowed_seqs: list) -> dict:
    """重放被遮蔽区间的派生消息（mini 无 system/tools 信封，仅 messages）。"""
    messages = []
    for seq in shadowed_seqs:
        message = derive_event_message(session.events[seq])
        if message is not None:
            messages.append(message)
    return {"messages": messages}


# ---------- 工具配对平衡（tool-pairing.ts） ----------

def _balance(session) -> list:
    """surface 每条切割的配对平衡：N 个节点 → N+1 条切割（i 在节点 i 之前）。"""
    nodes = session.surface_nodes()
    events = session.events
    balance = [True]
    in_progress = 0
    for node in nodes:
        ev = events[node["seq"]]
        etype = ev["type"]
        if etype == "assistant/message":
            in_progress += sum(
                1 for b in ev["data"].get("message", {}).get("content", [])
                if b.get("type") == "tool-call"
            )
        elif etype == "tool/result":
            in_progress -= 1
            if in_progress < 0:
                raise ValueError(
                    f"tool-pairing balance: tool/result at surface seq {node['seq']} has no matching tool-call"
                )
        balance.append(in_progress == 0)
    return balance