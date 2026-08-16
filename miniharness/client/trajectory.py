"""第 10 章：轨迹投影折叠引擎 —— 把事件日志折叠成回合台账。

对应 dsh 真实源码：packages/client/ui-trajectory（Trajectory 是 web 专属的
按 turn 组织的事件台账视图；数据源 = 同一份事件溯源日志在浏览器端的投影）。

上游折叠要点（已核实）：
  * 折叠是纯函数：每个 target 用独立 definition（match/update/finalNode）物化，
    无副作用、可重入 —— 这是"日志投影"而非"状态机"
  * 保留边界：Turn / Step / Request 边界保留因果结构（不拍平成裸记录流）
  * 产物 TrajectorySnapshot = { eventNodes, eventLocations, requests,
    callSchemas, partial, runningCalls }（trajectory-contract.ts:60-68）
  * headless 的 summarize（miniharness/headless.py）是最简投影（只拼 text 块），
    本模块是其向完整折叠的演进

载体简化说明：上游在浏览器端折叠并物化每 target 快照（虚拟化/增量索引属 UI）；
mini 复现折叠语义本身（事件流 → 结构化台账），UI 部分不做。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


@dataclass
class TrajectoryNode:
    """台账节点：保留 turn/step/request 边界的因果结构。
    ended_at 在 turn/end 时回填（折叠过程需要可变）。"""
    id: str
    kind: str                       # 'turn' | 'user' | 'assistant' | 'tool-call' | 'tool-result'
    turn: int
    step: int
    seq: int
    started_at: int
    ended_at: int | None = None
    text: str = ""
    call_id: str | None = None      # tool-call / tool-result 的 callId 关联
    parent_id: str | None = None    # tool-result → 其 tool-call
    children: list["TrajectoryNode"] = field(default_factory=list)

    @property
    def duration_ms(self) -> int | None:
        if self.ended_at is None:
            return None
        return self.ended_at - self.started_at


@dataclass
class TrajectorySnapshot:
    """折叠产物（对齐上游 TrajectorySnapshot 的语义子集）。
    partial=True 表示末尾有未闭合 turn（崩溃尾部）。"""
    nodes: list[TrajectoryNode] = field(default_factory=list)
    requests: list[dict] = field(default_factory=list)   # request/header 元数据
    turns: list[dict] = field(default_factory=list)      # 每 turn 摘要
    partial: bool = False

    def messages(self) -> list[dict]:
        """折叠后的消息序列（只含 user/assistant 文本消息，按 seq 序）。"""
        out = []
        for n in self.nodes:
            if n.kind in ("user", "assistant"):
                out.append({"turn": n.turn, "role": "user" if n.kind == "user" else "assistant",
                            "text": n.text})
        return out

    def last_assistant_text(self) -> str:
        for n in reversed(self.nodes):
            if n.kind == "assistant" and n.text != "":
                return n.text
        return ""

    def format_text(self) -> str:
        """终端可读台账：turn 分组 + 缩进层级 + 耗时。"""
        lines = []
        for n in self.nodes:
            indent = "  " * (0 if n.kind == "turn" else 1)
            dur = "" if n.duration_ms is None else f" [{n.duration_ms}ms]"
            if n.kind == "turn":
                lines.append(f"turn {n.turn}{dur}")
            elif n.kind in ("user", "assistant"):
                role = "用户" if n.kind == "user" else "助手"
                text = n.text.replace("\n", " ")
                lines.append(f"{indent}{role}: {text[:80]}{dur}")
            elif n.kind == "tool-call":
                lines.append(f"{indent}→ 工具 {n.call_id} 调用{dur}")
            elif n.kind == "tool-result":
                lines.append(f"{indent}← 工具 {n.call_id} 结果{dur}")
        return "\n".join(lines)


def fold_events_json(events: list | tuple) -> str:
    """把折叠结果序列化为 JSON（终端/CI 可断言）。"""
    s = fold_trajectory(events)
    return json.dumps({
        "partial": s.partial,
        "turns": s.turns,
        "requests": s.requests,
    }, ensure_ascii=False)


def fold_trajectory(events: list | tuple, first_seq: int = 0) -> TrajectorySnapshot:
    """纯函数折叠：事件流 → TrajectorySnapshot。

    折叠规则（对应上游 definition 的语义）：
      * turn/start 开新 turn 节点；turn/end 关闭（记录 ended_at）
      * user/message → 'user' 节点；assistant/message → 'assistant' 节点
        （文本来自 text 块；tool-call 块不并入消息文本，而是展开为子节点）
      * assistant/chunk 只影响当前 step 的 TTFT 观测，不产生节点
      * tool/call → 'tool-call' 节点；tool/result 按 callId 挂为子节点
      * request/header → requests 元数据（model/provider/reason）
      * 末尾仍有未闭合 turn → partial=True
    """
    snapshot = TrajectorySnapshot()
    nodes: dict[str, TrajectoryNode] = {}
    node_list: list[TrajectoryNode] = []
    turn_open: TrajectoryNode | None = None
    step_ttft: dict[int, int | None] = {}   # step -> 首个 assistant/chunk 的 time
    by_turn: dict[int, dict] = {}

    node_ids: dict[str, int] = {}

    def mkid(prefix: str) -> str:
        n = node_ids.get(prefix, 0) + 1
        node_ids[prefix] = n
        return f"{prefix}-{n}"

    for ev in events:
        if ev["seq"] < first_seq:
            continue
        t = ev["type"]
        d = ev["data"]
        time = ev.get("time", 0)

        if t == "turn/start":
            turn = TrajectoryNode(mkid("turn"), "turn", d["turn"], 0, ev["seq"],
                                  started_at=time)
            nodes[turn.id] = turn
            node_list.append(turn)
            turn_open = turn
            by_turn[d["turn"]] = {
                "turn": d["turn"], "user_texts": [], "assistant_texts": [],
                "started_at": time, "ended_at": None, "tool_calls": 0, "ttft_ms": None,
            }
        elif t == "user/message":
            text = "".join(b.get("text", "") for b in d.get("content", [])
                           if b.get("type") == "text" and b.get("text") is not None)
            node = TrajectoryNode(mkid("user"), "user", d.get("turn", 0), d.get("step", 0),
                                  ev["seq"], started_at=time, text=text)
            node_list.append(node)
        elif t == "assistant/chunk":
            chunk = d.get("chunk", {})
            step = d.get("step", 0)
            if step not in step_ttft:
                step_ttft[step] = time
        elif t == "assistant/message":
            text = "".join(b.get("text", "") for b in d.get("message", {}).get("content", [])
                           if b.get("type") == "text" and b.get("text") is not None)
            node = TrajectoryNode(mkid("assistant"), "assistant",
                                  d.get("turn", 0), d.get("step", 0), ev["seq"],
                                  started_at=time, text=text)
            node_list.append(node)
        elif t == "tool/call":
            tc = TrajectoryNode(mkid("tool-call"), "tool-call",
                                d.get("turn", 0), d.get("step", 0), ev["seq"],
                                started_at=time, call_id=d.get("callId"))
            nodes[tc.id] = tc
            node_list.append(tc)
        elif t == "tool/result":
            call_id = d.get("message", {}).get("source", {}).get("callId")
            parent = next((n for n in reversed(node_list)
                           if n.kind == "tool-call" and n.call_id == call_id), None)
            node = TrajectoryNode(mkid("tool-result"), "tool-result",
                                  d.get("turn", 0), d.get("step", 0), ev["seq"],
                                  started_at=time,
                                  parent_id=parent.id if parent else None)
            if parent is not None:
                parent.children.append(node)
            node_list.append(node)
        elif t == "step/end":
            step = d.get("step", 0)
            if step in step_ttft and step_ttft[step] is not None:
                pass  # TTFT 在 turn 摘要处汇总
        elif t == "turn/end":
            if turn_open is not None:
                turn_open.ended_at = time
                turn_open = None
        elif t == "request/header":
            header = d.get("header", {})
            snapshot.requests.append({
                "seq": ev["seq"], "turn": d.get("turn", 0), "step": d.get("step", 0),
                "model": header.get("model"), "provider": header.get("provider"),
                "reason": d.get("reason"),
            })

    snapshot.nodes = node_list
    snapshot.partial = turn_open is not None

    # turn 摘要收尾（框架已由 turn/start 登记，这里补消息/工具/闭合）
    for n in node_list:
        d_ = by_turn.get(n.turn)
        if d_ is None:
            continue
        if n.kind == "user" and n.text:
            d_["user_texts"].append(n.text)
        elif n.kind == "assistant" and n.text:
            d_["assistant_texts"].append(n.text)
        elif n.kind == "tool-call":
            d_["tool_calls"] += 1
    for ev_ in events:
        if ev_["seq"] < first_seq:
            continue
        if ev_["type"] == "turn/end":
            d_ = by_turn.get(ev_["data"].get("turn", 0))
            if d_ is not None:
                d_["ended_at"] = ev_.get("time", 0)
    turn_start: dict[int, int] = {}
    for n in node_list:
        if n.kind == "turn":
            turn_start[n.turn] = n.started_at
    # TTFT：每个 turn 内最早出现的 assistant/chunk 观测
    for ev_ in events:
        if ev_["seq"] < first_seq:
            continue
        if ev_["type"] == "assistant/chunk":
            d_ = by_turn.get(ev_["data"].get("turn", 0))
            if d_ is not None and d_["ttft_ms"] is None:
                start = turn_start.get(ev_["data"].get("turn", 0))
                if start is not None:
                    d_["ttft_ms"] = ev_.get("time", 0) - start
    snapshot.turns = [by_turn[k] for k in sorted(by_turn)]
    return snapshot


def fold_events_json(events: list | tuple) -> str:
    """把折叠结果序列化为 JSON（终端/CI 可断言）。"""
    s = fold_trajectory(events)
    return json.dumps({
        "partial": s.partial,
        "turns": s.turns,
        "requests": s.requests,
    }, ensure_ascii=False)