"""Assistant 紧凑流编解码：AssistantStreamRecord 压缩 / 展开 / 校验。

对应 dsh 真实源码：packages/llm/llm/src/assistant-stream.ts。

V2 会话事件格式把模型流内嵌进 `assistant/message`/`assistant/attempt` 的
`stream: AssistantStreamRecord[]` 字段（替代旧版逐 chunk 落 `assistant/chunk`
事件 + sourceEventSeqs 引用）。本模块提供：

  * `AssistantStreamAccumulator`——增量压缩（生产端）：把带时间戳的
    `TimedStreamChunk` 流压缩成 `AssistantStreamRecord[]`，不保留第二份
    原始 chunk 列表。
  * `expand_assistant_stream`——逆向解码（消费端）：把 record 还原成精确的
    `TimedStreamChunk[]`（保留每个 delta 边界与相对时间）。
  * `validate_record`——单条 record 的严格形状校验（seed 恢复边界用它 fail-closed）。

四种 record 变体（compressed run vs 原样 chunk）：
  * `{type:'text-chunks', time0, index, dt[], texts[]}`  —— 连续 text-delta 游程
  * `{type:'reasoning-chunks', time0, index, dt[], texts[]}` —— 连续 reasoning-delta
  * `{type:'tool-call-chunks', time0, index, dt[], id, name?, args[]}` —— 连续 tool-call-delta
  * `{type:'chunk', time, chunk}` —— 不可游程压缩的原始 chunk（block-start/end、
    usage、finish、或不合格的 tool-call-delta）

约束：dt.length === texts.length - 1（args 同理）；所有 time 为安全整数。
"""
from __future__ import annotations

from typing import Any, Mapping

from .protocol import STREAM_CHUNK_KINDS

__all__ = [
    "AssistantStreamAccumulator",
    "TimedStreamChunk",
    "expand_assistant_stream",
    "validate_record",
]

_SAFE_INT_MAX = 2 ** 53 - 1


class TimedStreamChunk:
    """一条模型 chunk + 其原始会话时间戳（上游 TimedStreamChunk）。"""

    __slots__ = ("time", "chunk")

    def __init__(self, time: int, chunk: dict):
        self.time = time
        self.chunk = chunk

    def to_dict(self) -> dict:
        return {"time": self.time, "chunk": self.chunk}


def _safe_time(value: Any) -> int:
    """time 必须是安全整数（上游 safeTime）。"""
    if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= _SAFE_INT_MAX):
        raise TypeError(f"assistant stream time must be a non-negative safe integer, got {value!r}")
    return value


def _safe_index(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not (0 <= value <= _SAFE_INT_MAX):
        raise TypeError(f"assistant stream index must be a non-negative safe integer, got {value!r}")
    return value


def _safe_gap(last_time: int, time: int) -> int:
    gap = time - last_time
    if not (0 <= gap <= _SAFE_INT_MAX):
        raise TypeError("assistant stream delta gap is not a non-negative safe integer")
    return gap


def _snapshot_chunk(chunk: dict) -> dict:
    """浅拷贝一份 chunk（上游 snapshotChunk：不深拷贝、不冻结，保持 JSON 结构）。"""
    return dict(chunk)


def expand_assistant_stream(stream) -> list[TimedStreamChunk]:
    """把 compact records 展开为精确的 TimedStreamChunk 序列（上游同名函数）。

    保留每个原始 delta 边界。record 无效时抛 TypeError（seed 恢复边界据此
    fail-closed）。
    """
    chunks: list[TimedStreamChunk] = []
    for candidate in stream:
        record = validate_record(candidate)
        if record["type"] == "chunk":
            time = _safe_time(record["time"])
            if not isinstance(record["chunk"], Mapping) or record["chunk"].get("type") not in STREAM_CHUNK_KINDS:
                raise TypeError("assistant stream chunk record carries an invalid chunk")
            chunks.append(TimedStreamChunk(time, record["chunk"]))
            continue
        members = record["args"] if record["type"] == "tool-call-chunks" else record["texts"]
        time = _safe_time(record["time0"])
        index = record["index"]
        for i, _member in enumerate(members):
            if i > 0:
                time += record["dt"][i - 1]
            if record["type"] == "text-chunks":
                chunk = {"type": "text-delta", "index": index, "text": members[i]}
            elif record["type"] == "reasoning-chunks":
                chunk = {"type": "reasoning-delta", "index": index, "text": members[i]}
            else:
                chunk = {"type": "tool-call-delta", "index": index, "id": record["id"],
                         "argumentsDelta": members[i]}
                if "name" in record:
                    chunk["name"] = record["name"]
            chunks.append(TimedStreamChunk(time, chunk))
    return chunks


def validate_record(record) -> dict:
    """校验单条 AssistantStreamRecord 形状并返回普通 dict（fail-closed）。

    完整复刻上游 validateRecord 的 exactKeys / 类型 / dt 长度不变量：非法即
    TypeError。接受 MappingProxyType（mini 冻结事件）、Mapping、dict 等。
    """
    if not isinstance(record, Mapping):
        raise TypeError("assistant stream record must be an object")
    rtype = record.get("type")
    if rtype == "chunk":
        allowed = {"type", "time", "chunk"}
        if set(record.keys()) != allowed:
            raise TypeError("assistant stream chunk record must have exactly type, time, chunk")
        _safe_time(record.get("time"))
        if not isinstance(record.get("chunk"), Mapping) or record["chunk"].get("type") not in STREAM_CHUNK_KINDS:
            raise TypeError("assistant stream chunk record carries an invalid chunk")
        return dict(record)
    if rtype in ("text-chunks", "reasoning-chunks"):
        allowed = {"type", "time0", "index", "dt", "texts"}
        if set(record.keys()) != allowed:
            raise TypeError(f"assistant stream {rtype} record key set mismatch")
        _safe_time(record.get("time0"))
        _safe_index(record.get("index"))
        dt, texts = _val_lists(record.get("dt"), record.get("texts"), "text")
        _run_invariant(rtype, len(dt), len(texts))
        return dict(record)
    if rtype == "tool-call-chunks":
        for key in ("time0", "index", "id"):
            if key not in record:
                raise TypeError("assistant stream tool-call-chunks record missing required key")
        allowed = {"type", "time0", "index", "dt", "id", "name", "args"}
        if not set(record.keys()).issubset(allowed):
            raise TypeError("assistant stream tool-call-chunks record key set mismatch")
        _safe_time(record.get("time0"))
        _safe_index(record.get("index"))
        if not isinstance(record.get("id"), str):
            raise TypeError("assistant stream tool-call-chunks id must be a string")
        if "name" in record and not isinstance(record["name"], str):
            raise TypeError("assistant stream tool-call-chunks name must be a string")
        dt, args = _val_lists(record.get("dt"), record.get("args"), "args")
        _run_invariant(rtype, len(dt), len(args))
        return dict(record)
    raise TypeError(f"unknown assistant stream record type: {rtype!r}")


def _val_lists(dt, members, kind):
    if not isinstance(dt, (list, tuple)) or not isinstance(members, (list, tuple)):
        raise TypeError("assistant stream run lists must be arrays")
    dt_list = [d for d in dt]
    members_list = [m for m in members]
    for d in dt_list:
        if not isinstance(d, int) or isinstance(d, bool) or not (0 <= d <= _SAFE_INT_MAX):
            raise TypeError("assistant stream dt element must be a non-negative safe integer")
    if kind == "args":
        for m in members_list:
            if not isinstance(m, str):
                raise TypeError("assistant stream tool-call args element must be a string")
    else:
        for m in members_list:
            if not isinstance(m, str):
                raise TypeError(f"assistant stream {kind} element must be a string")
    return dt_list, members_list


def _run_invariant(rtype: str, dt_len: int, members_len: int) -> None:
    if dt_len != members_len - 1:
        raise TypeError(f"assistant stream {rtype} dt length must equal members length - 1")


class AssistantStreamAccumulator:
    """增量压缩一次 attempt 的模型流为 AssistantStreamRecord[]。

    逐条 push 原始 chunk，同时维护压缩游程；snapshot() 产出可用于落事件的
    不可变 record 列表。上游同名类（assistant-stream.ts:85-179）。
    """

    def __init__(self):
        # 内部记录带 lastTime 仅用于游程判定，snapshot() 时剔除
        self._records: list[dict] = []

    def push_chunk_time(self, time: int, chunk: dict) -> TimedStreamChunk:
        """push 一条带时间戳的 chunk（上游 push(value: TimedStreamChunk) 的等价物）。

        返回深游离的不变 TimedStreamChunk（供组装与实时发布复用同一次快照）。
        """
        time = _safe_time(time)
        snapshot = _snapshot_chunk(chunk)
        timed = TimedStreamChunk(time, snapshot)
        ctype = snapshot.get("type")
        records = self._records
        prev = records[-1] if records else None

        if ctype in ("text-delta", "reasoning-delta"):
            _safe_index(snapshot.get("index"))
            if not isinstance(snapshot.get("text"), str):
                raise TypeError(f"{ctype} text must be a string")
            run_type = "text-chunks" if ctype == "text-delta" else "reasoning-chunks"
            if (prev is not None and prev["type"] == run_type
                    and prev["index"] == snapshot["index"]
                    and _gap_ok(prev, time)):
                prev["dt"].append(_safe_gap(prev["lastTime"], time))
                prev["texts"].append(snapshot["text"])
                prev["lastTime"] = time
            else:
                records.append({"type": run_type, "time0": time, "index": snapshot["index"],
                                "dt": [], "texts": [snapshot["text"]], "lastTime": time})
            return timed

        if ctype == "tool-call-delta":
            _safe_index(snapshot.get("index"))
            if not isinstance(snapshot.get("id"), str):
                raise TypeError("tool-call-delta id must be a string")
            if "name" in snapshot and not isinstance(snapshot["name"], str):
                raise TypeError("tool-call-delta name must be a string")
            if not isinstance(snapshot.get("argumentsDelta"), str):
                raise TypeError("tool-call-delta argumentsDelta must be a string")
            if len(snapshot.get("id", "")) == 0 or snapshot.get("name") == "":
                records.append({"type": "chunk", "time": time, "chunk": snapshot})
                return timed
            same_run = (prev is not None and prev["type"] == "tool-call-chunks"
                        and prev["index"] == snapshot["index"]
                        and prev["id"] == snapshot["id"]
                        and ("name" in prev) == ("name" in snapshot)
                        and prev.get("name") == snapshot.get("name")
                        and _gap_ok(prev, time))
            if same_run:
                prev["dt"].append(_safe_gap(prev["lastTime"], time))
                prev["args"].append(snapshot["argumentsDelta"])
                prev["lastTime"] = time
            else:
                rec = {"type": "tool-call-chunks", "time0": time, "index": snapshot["index"],
                       "dt": [], "id": snapshot["id"], "args": [snapshot["argumentsDelta"]],
                       "lastTime": time}
                if "name" in snapshot:
                    rec["name"] = snapshot["name"]
                records.append(rec)
            return timed

        if ctype in ("block-start", "block-end", "usage", "finish"):
            records.append({"type": "chunk", "time": time, "chunk": snapshot})
            return timed

        raise TypeError(f"unknown chunk type: {ctype!r}")

    def push(self, timed: TimedStreamChunk) -> TimedStreamChunk:
        return self.push_chunk_time(timed.time, timed.chunk)

    def snapshot(self) -> list[dict]:
        """产出不可变 record 列表（用于落事件），剔除内部 lastTime。"""
        out: list[dict] = []
        for rec in self._records:
            if rec["type"] == "chunk":
                out.append({"type": "chunk", "time": rec["time"], "chunk": rec["chunk"]})
                continue
            if rec["type"] == "tool-call-chunks":
                base = {"type": "tool-call-chunks", "time0": rec["time0"], "index": rec["index"],
                        "dt": list(rec["dt"]), "id": rec["id"], "args": list(rec["args"])}
                if "name" in rec:
                    base["name"] = rec["name"]
                out.append(base)
            else:
                out.append({"type": rec["type"], "time0": rec["time0"], "index": rec["index"],
                            "dt": list(rec["dt"]), "texts": list(rec["texts"])})
        return out


def _gap_ok(prev: dict, time: int) -> bool:
    try:
        _safe_gap(prev["lastTime"], time)
        return True
    except TypeError:
        return False
