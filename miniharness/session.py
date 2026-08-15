"""第 1 章：事件溯源会话（event-sourced session）。

对应 dsh 真实源码：packages/core/session —— Session 是 SessionEvent 的
追加式事件日志，模型历史由 derive_messages() 投影派生，绝不另存副本。

与上游对齐的硬性规定（均在 packages/core/session 核实）：
  1. 事件信封 {type, seq, time, data}，seq == log.length 连续（types.ts）
  2. turn/step 编号从 1 起，每 turn 内 step 重置为 1（invariant.ts）
  3. 坏事件进不来（未知类型 / 非无损 JSON → 直接抛错）
  4. 消息模型 {id, role, content: ContentBlock[], source}（packages/llm/llm）
  5. 模型可见 ⟺ 已记录：投影纯函数，无第二份副本
"""
from __future__ import annotations

import json
import time
import uuid
from types import MappingProxyType
from typing import Any

# 磁盘会话格式版本（上游 packages/core/session/src/types.ts SESSION_FORMAT_VERSION）
SESSION_FORMAT_VERSION = 0

KNOWN_TYPES = frozenset({
    "turn/start", "turn/end", "step/start", "step/end",
    "user/message", "assistant/message", "assistant/chunk",
    "tool/call", "tool/result", "request/header", "session/end-seed",
    # 审批审计（上游 user-approval/src/index.ts SessionEventMap，log-only 非 surface）
    "approval/asked", "approval/decided", "approval/policy",
    # 钩子审计（上游 hook-protocol/src/types.ts SessionEventMap，log-only 非 surface）
    "hook/invoked", "hook/result",
    # LLM 重试审计（上游 llm-retry/src/index.ts SessionEventMap，log-only 非 surface）
    "llm/retry", "llm/retry-started",
})

# 只有这三种事件产生模型消息，可带 surfaceOp（上游 types.ts SurfaceEventType）
SURFACE_TYPES = frozenset({"user/message", "assistant/message", "tool/result"})

# 崩溃恢复码（上游 session/src/repair.ts）
TOOL_NOT_STARTED = "TOOL_NOT_STARTED"
TOOL_OUTCOME_UNKNOWN = "TOOL_OUTCOME_UNKNOWN"


# ---------- 无损 JSON 与深度冻结 ----------

def is_json_safe(value: Any) -> bool:
    """无损 JSON 强制：无法序列化的值（含非有限浮点数）直接判非法。"""
    try:
        json.dumps(value, ensure_ascii=False, allow_nan=False)
        return True
    except (TypeError, ValueError):
        return False


def deep_freeze(value: Any) -> Any:
    """深度冻结：dict → 只读代理，list → tuple。冻结后任何修改都抛 TypeError。"""
    if isinstance(value, dict):
        return MappingProxyType({k: deep_freeze(v) for k, v in value.items()})
    if isinstance(value, list):
        return tuple(deep_freeze(v) for v in value)
    return value


def now_ms() -> int:
    """Unix epoch 毫秒（与上游事件 time 字段一致）。"""
    return int(time.time() * 1000)


# ---------- 消息模型（packages/llm/llm/src/message.ts + types.ts） ----------

def create_message(role: str, content: list, source: dict | None = None) -> dict:
    """构造带稳定 id 的消息：{id, role, content: ContentBlock[], source}。

    消息在落日志时由 Session.append 冻结；此处保持普通 dict/list，
    以便适配器序列化与 wire 传输（冻结是日志边界的职责）。
    """
    return {
        "id": str(uuid.uuid4()),
        "role": role,
        "content": list(content),
        "source": source or {"kind": role},
    }


def text_block(text: str) -> dict:
    return {"type": "text", "text": text}


def reasoning_block(text: str) -> dict:
    return {"type": "reasoning", "text": text}


def tool_call_block(call_id: str, name: str, arguments: str) -> dict:
    """tool-call 块：arguments 是模型产出的原始 JSON 字符串（不解析）。"""
    return {"type": "tool-call", "id": call_id, "name": name, "arguments": arguments}


def tool_result_block(tool_call_id: str, content: list, is_error: bool = False) -> dict:
    block = {"type": "tool-result", "toolCallId": tool_call_id, "content": list(content)}
    if is_error:
        block["isError"] = True
    return block


def thaw(value: Any) -> Any:
    """解冻：MappingProxyType → dict，tuple → list。持久化前还原为普通 JSON 结构。"""
    if isinstance(value, MappingProxyType):
        value = dict(value)
    if isinstance(value, dict):
        return {k: thaw(v) for k, v in value.items()}
    if isinstance(value, tuple):
        return [thaw(v) for v in value]
    return value


def _is_replace_op(op: Any) -> bool:
    """surfaceOp 是否为 {op:'replace', start, end}（兼容冻结后的 MappingProxyType）。"""
    return isinstance(op, (dict, MappingProxyType)) and op.get("op") == "replace"


# ---------- 会话 ----------

class Session:
    """追加式事件日志：唯一事实来源。构造时可带 seed（恢复/回放历史）。"""

    def __init__(self, session_id: str, seed: list | None = None, created_at: int | None = None):
        self.session_id = session_id
        self.created_at = created_at or now_ms()
        self._events: list[dict[str, Any]] = []
        if seed:
            self._replay_seed(seed)

    @property
    def seq(self) -> int:
        return len(self._events)

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        """只读视图：外部永远拿不到可变的内部列表。"""
        return tuple(self._events)

    def append(self, type_: str, data: dict | None = None, surfaceOp=None,
               sourceEventSeqs: list[int] | None = None) -> dict[str, Any]:
        """源头校验 + 冻结：坏事件永远进不了日志。

        与上游 append(type, data, surfaceOp) 签名一致；surface 事件必须带
        surfaceOp（'append' 或 {op:'replace', start, end}），非 surface 事件
        禁止携带；sourceEventSeqs 仅 surface 事件可带（上游 SurfaceIntent）。
        """
        if type_ not in KNOWN_TYPES:
            raise ValueError(f"未知事件类型: {type_!r}")
        payload: dict[str, Any] = {"type": type_, "data": data if data is not None else {}}
        if type_ in SURFACE_TYPES:
            if surfaceOp is None:
                raise ValueError(f"surface 事件 {type_} 必须带 surfaceOp")
            if surfaceOp != "append":
                if not (isinstance(surfaceOp, dict) and surfaceOp.get("op") == "replace"):
                    raise ValueError(f"非法 surfaceOp: {surfaceOp!r}")
            payload["surfaceOp"] = surfaceOp
            if sourceEventSeqs is not None:
                payload["sourceEventSeqs"] = list(sourceEventSeqs)
        elif surfaceOp is not None:
            raise ValueError(f"非 surface 事件 {type_} 不允许携带 surfaceOp")
        if not is_json_safe(payload):
            raise TypeError(f"事件必须可无损 JSON 序列化: {payload!r}")
        record = deep_freeze({"seq": self.seq, "time": now_ms(), **payload})
        self._events.append(record)
        return record

    def _replay_seed(self, seed: list) -> None:
        """恢复模式回放 seed：seq 必须从 0 连续、类型已知、surface 合法。

        与上游 restore 模式一致：冻结但不二次克隆；seed 末事件不是
        session/end-seed 时自动补记该标记（本进程首个 append 之前的边界）。
        """
        for i, ev in enumerate(seed):
            if not isinstance(ev, (dict, MappingProxyType)) or ev.get("seq") != i:
                raise ValueError(f"seed 事件 seq 必须从 0 连续，第 {i} 条不符")
            etype = ev.get("type")
            if etype not in KNOWN_TYPES:
                raise ValueError(f"未知事件类型: {etype!r}")
            data = ev.get("data", {})
            if etype in SURFACE_TYPES:
                op = ev.get("surfaceOp")
                if op not in ("append",) and not (isinstance(op, dict) and op.get("op") == "replace"):
                    raise ValueError(f"surface 事件 {etype} 必须带合法 surfaceOp")
            if not is_json_safe(thaw(ev)):
                raise TypeError(f"seed 事件必须可无损 JSON 序列化: {ev!r}")
            self._events.append(deep_freeze(ev))
        if not seed or self._events[-1]["type"] != "session/end-seed":
            last_time = self._events[-1]["time"] if self._events else self.created_at
            marker = deep_freeze({"type": "session/end-seed", "seq": self.seq, "time": last_time, "data": {}})
            self._events.append(marker)


# ---------- 投影（surface → 模型消息） ----------

def _project_message(ev: dict) -> dict | None:
    """surface 节点 → 模型消息。空内容 assistant/message（如 max-tokens 只含
    usage 的 step）派生为 None，不入转录（上游 surface.ts deriveEventMessage）。"""
    data = ev["data"]
    if ev["type"] == "user/message":
        return data
    if ev["type"] == "assistant/message":
        message = data.get("message")
        if message and not message.get("content"):
            return None
        return message
    if ev["type"] == "tool/result":
        return data.get("message")
    return None


def derive_messages(events) -> list[dict]:
    """纯投影：沿 surface 节点顺序派生模型消息（不修改日志，可重复调用）。

    replace 节点遮蔽被替换区间（上游 surface.ts：{op:'replace', start, end}
    替换 start..end 两个 surface 节点为一个新节点）。
    """
    surface: list[dict] = []
    for ev in events:
        if ev["type"] not in SURFACE_TYPES:
            continue
        op = ev.get("surfaceOp")
        if op == "append":
            surface.append(ev)
        elif _is_replace_op(op):
            start, end = op["start"], op["end"]
            surface = surface[:start] + [ev] + surface[end + 1:]
    messages = []
    for node in surface:
        msg = _project_message(node)
        if msg is not None:
            messages.append(msg)
    return messages


# ---------- 括号平衡与崩溃恢复（上游 session/src/repair.ts） ----------

def turn_balance(events) -> int:
    """括号平衡硬性规定：返回未闭合 turn 数（>=0）。为负说明日志被破坏。"""
    balance = 0
    for ev in events:
        if ev["type"] == "turn/start":
            balance += 1
        elif ev["type"] == "turn/end":
            balance -= 1
            if balance < 0:
                raise ValueError("turn/end 出现在没有对应 turn/start 的位置，日志不平衡")
    return balance


def repair_interrupted_turn(events: list) -> list[dict]:
    """崩溃恢复：为未闭合的尾部 turn 合成确定性闭包事件（上游 interruptedTurnClosers）。

    顺序与上游一致：先为未匹配的 tool call 补 error 结果（TOOL_NOT_STARTED /
    TOOL_OUTCOME_UNKNOWN），再补 step/end，最后补 turn/end {kind:'interrupted'}；
    seq 延续日志，时间戳复用最后真实事件。已平衡的日志返回空列表。
    """
    open_turn: int | None = None
    open_step: int | None = None
    pending_calls: dict[str, dict] = {}
    for ev in events:
        t = ev["type"]
        data = ev["data"]
        if t == "turn/start":
            open_turn, open_step, pending_calls = data["turn"], None, {}
        elif t == "turn/end":
            open_turn = open_step = None
            pending_calls = {}
        elif t == "step/start":
            open_step = data["step"]
        elif t == "step/end":
            open_step = None
            pending_calls = {}
        elif t == "assistant/message":
            for block in data["message"]["content"]:
                if block["type"] == "tool-call":
                    pending_calls[block["id"]] = {"step": data["step"], "callSeq": None}
        elif t == "tool/call":
            entry = pending_calls.get(data["callId"])
            if entry is not None:
                entry["callSeq"] = ev["seq"]
        elif t == "tool/result":
            pending_calls.pop(data["message"]["source"]["callId"], None)

    if open_turn is None or not events:
        return []

    seq = events[-1]["seq"] + 1
    time_ = events[-1]["time"]
    closers: list[dict] = []

    # 先关调用再关 step：provider 拒绝悬挂的 assistant tool call
    for call_id, info in pending_calls.items():
        started = info["callSeq"] is not None
        message = {
            "id": f"interrupted-tool-result-{call_id}-{seq}",
            "role": "user",
            "source": {"kind": "tool", "callId": call_id},
            "content": [{
                "type": "tool-result",
                "toolCallId": call_id,
                "isError": True,
                "content": [{"type": "text", "text": (
                    "The tool call was interrupted after it was recorded, but no result was durably "
                    "recorded. Its outcome is unknown. Decide whether to retry from the tool semantics: "
                    "retry only if the operation is read-only or idempotent; if it may have side effects, "
                    "first verify external state or ask the user. Do not retry blindly."
                    if started else
                    "The tool call was interrupted before the Harness recorded it as started. "
                    "Retry it if it is still needed."
                )}],
            }],
        }
        error = {
            "name": "ToolOutcomeUnknownError" if started else "ToolNotStartedError",
            "code": TOOL_OUTCOME_UNKNOWN if started else TOOL_NOT_STARTED,
        }
        closers.append({
            "type": "tool/result",
            "seq": seq, "time": time_,
            "data": {"turn": open_turn, "step": info["step"], "message": message, "error": error},
            "surfaceOp": "append",
            **({"sourceEventSeqs": [info["callSeq"]]} if started else {}),
        })
        seq += 1

    if open_step is not None:
        closers.append({"type": "step/end", "seq": seq, "time": time_,
                        "data": {"turn": open_turn, "step": open_step}})
        seq += 1

    closers.append({"type": "turn/end", "seq": seq, "time": time_,
                    "data": {"turn": open_turn, "reason": {"kind": "interrupted"}}})
    return closers