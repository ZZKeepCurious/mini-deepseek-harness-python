"""v1→v2 相邻迁移（上游 session-format-v1-to-v2/src/migration.ts 逐语义移植）。

五步：源 scoped 校验 + 封闭词表（未知含 ignorable 拒）→ attempt 分组（六种封口边界：
finish 自封 / message 认领 / step-end / llm-retry / llm-retry-started / turn-end）→
staging（chunk 全消费：已认领组丢弃、未认领组最后一条变 `assistant/attempt`）→
cut 重导出（新 cut = origin<旧cut 条数；切割拒；marker 重打标/合成，time 兜底链
next→previous→createdAt）→ 密集重映射（先删全部组 chunk seq，消费引用拒、绝不
重定向；只改声明字段；title framed 文本逐字节保真）。

usage 不提级：usage chunk 以原始 `chunk` record 留在 stream 内；message 级 `data.usage`
原样透传（目标校验由现行 v2 restore 的三事实比对兜底）。引用 label 用**新 index**
（上游 remapReferences(event, i, ...) 同款）。
"""
from __future__ import annotations

from typing import Any

from . import dispositions as _disp
from .helpers import is_json_object, snapshot_json, unsupported
from .validate import assert_released_v1_artifact, assert_released_v1_header
from .validate_v2 import assert_released_v2_artifact

__all__ = ["V1_TO_V2"]

_SEALING_STEP_EVENTS = frozenset({"step/end", "llm/retry", "llm/retry-started"})


class _Group:
    """一次 assistant 流 attempt（按 turn:step 坐标切分）。"""

    __slots__ = ("turn", "step", "chunks", "terminal", "message_seq")

    def __init__(self, turn: int, step: int) -> None:
        self.turn = turn
        self.step = step
        self.chunks: list[tuple[int, int, dict]] = []  # (seq, time, chunk)
        self.terminal = False
        self.message_seq: int | None = None

    @property
    def claimed(self) -> bool:
        return self.message_seq is not None

    def member_seqs(self) -> list[int]:
        seqs = [seq for seq, _, _ in self.chunks]
        if self.message_seq is not None:
            seqs.append(self.message_seq)
        return seqs


def _collect_attempt_groups(events: list[dict]) -> tuple[list[_Group], dict[int, _Group],
                                                          dict[int, _Group]]:
    """把 chunk 序列切成一次一次 attempt（上游 collectAttemptGroups）。

    返回 (groups, chunk_seq→group, message_seq→group)。
    """
    groups: list[_Group] = []
    by_chunk: dict[int, _Group] = {}
    by_message: dict[int, _Group] = {}
    current: dict[str, _Group] = {}
    for event in events:
        etype = event["type"]
        seq = event["seq"]
        data = event["data"]
        if etype == "assistant/chunk":
            turn, step = data["turn"], data["step"]
            key = f"{turn}:{step}"
            group = current.get(key)
            if group is None or group.terminal:
                group = _Group(turn, step)
                groups.append(group)
            group.chunks.append((seq, event["time"], data["chunk"]))
            current[key] = group
            by_chunk[seq] = group
            if data["chunk"].get("type") == "finish":
                group.terminal = True
        elif etype == "assistant/message":
            turn, step = data["turn"], data["step"]
            sources = event.get("sourceEventSeqs")
            if sources is None:
                unclaimed = [g for g in groups
                             if g.turn == turn and g.step == step and g.message_seq is None]
                if unclaimed:
                    raise unsupported(
                        f"assistant/message {seq} does not cite its complete v1 chunk attempt")
                group = _Group(turn, step)
            elif sources == []:
                group = _Group(turn, step)
            else:
                match = next((g for g in groups
                              if g.message_seq is None and g.turn == turn and g.step == step
                              and [chunk_seq for chunk_seq, _, _ in g.chunks] == list(sources)),
                             None)
                if match is None:
                    raise unsupported(
                        f"assistant/message {seq} chunk provenance is not one complete "
                        "ordered attempt")
                match.message_seq = seq
                match.terminal = True
                by_message[seq] = match
                continue
            # 无 sources（无未认领组）或显式 []：新建空组并被本 message 认领
            group.message_seq = seq
            group.terminal = True
            groups.append(group)
            by_message[seq] = group
        else:
            # 边界封口：turn/end 封该 turn 全部组；step/end 与 llm/retry 族封本步组
            if etype == "turn/end":
                turn = data.get("turn")
                for group in groups:
                    if group.turn == turn:
                        group.terminal = True
            elif etype in _SEALING_STEP_EVENTS:
                group = current.get(f"{data.get('turn')}:{data.get('step')}")
                if group is not None:
                    group.terminal = True
    return groups, by_chunk, by_message


def _stream_of(group: _Group) -> list[dict]:
    """chunk 流 → AssistantStreamRecord（逐 chunk 喂现行 Accumulator）。"""
    from ....llm.assistant_stream import AssistantStreamAccumulator
    accumulator = AssistantStreamAccumulator()
    for _, time, chunk in group.chunks:
        accumulator.push_chunk_time(time, chunk)
    return accumulator.snapshot()


def _attempt_event(group: _Group) -> dict:
    last_seq, last_time, _ = group.chunks[-1]
    return {"type": "assistant/attempt", "seq": last_seq, "time": last_time,
            "data": {"turn": group.turn, "step": group.step, "stream": _stream_of(group)}}


def _message_event(event: dict, group: _Group) -> dict:
    data = dict(event["data"])
    data["stream"] = _stream_of(group)
    staged = {k: v for k, v in event.items() if k != "sourceEventSeqs"}
    staged["data"] = data
    return staged


def _stage_events(events: list[dict], groups: list[_Group], by_chunk: dict[int, _Group],
                  by_message: dict[int, _Group], header: dict, old_cut: int,
                  ) -> list[tuple[int, dict]]:
    """按源序 staging（上游 staging 循环）。返回 (origin, event) 列表（origin=-1 合成）。"""
    staged: list[tuple[int, dict]] = []
    for event in events:
        seq = event["seq"]
        etype = event["type"]
        if etype == "assistant/chunk":
            group = by_chunk[seq]
            if group.claimed:
                continue
            if seq != group.chunks[-1][0]:
                continue
            staged.append((seq, _attempt_event(group)))
        elif etype == "assistant/message":
            staged.append((seq, _message_event(event, by_message[seq])))
        elif header.get("isSeeded") and seq == old_cut and etype == "session/end-seed":
            staged.append((seq, {"type": etype, "seq": seq, "time": event["time"],
                                 "data": {"inherited": True}}))
        else:
            staged.append((seq, dict(event)))
    return staged


def _map_one(old_seq: int, old_to_new: dict[int, int], label: str) -> int:
    target = old_to_new.get(old_seq)
    if target is None:
        raise unsupported(f"{label} targets consumed assistant/chunk {old_seq}")
    return target


def _map_list(values: list[Any], old_to_new: dict[int, int], label: str) -> list[int]:
    return [_map_one(member, old_to_new, label) for member in values]


def _remap_references(event: dict, old_to_new: dict[int, int]) -> dict:
    """密集重映射：只改声明字段（信封 provenance / surfaceOp / 四类 payload 引用）。

    label 逐字对齐上游 remapReferences（migration.ts:248-343）：sources /
    surface start,end / sourceEventSeq / shadowedRange start,end / shadowedSeqs /
    messageSeqs。

    `session/title-llm-request.messages` 的 framed 文本逐字节保真——嵌在文本里的旧
    seq 不重解释（目标校验以 preservedSourceTitleRequestText 跳过 framed 复核，
    Phase A 由现行 restore 承担）。
    """
    etype = event["type"]
    seq = event["seq"]
    remapped = dict(event)
    if "sourceEventSeqs" in remapped:
        label = f"{etype} {seq} sources"
        remapped["sourceEventSeqs"] = _map_list(remapped["sourceEventSeqs"], old_to_new, label)
    surface_op = remapped.get("surfaceOp")
    if is_json_object(surface_op):
        label = f"{etype} {seq} surface"
        remapped["surfaceOp"] = {
            "op": "replace",
            "start": _map_one(surface_op["start"], old_to_new, f"{label} start"),
            "end": _map_one(surface_op["end"], old_to_new, f"{label} end"),
        }
    data = remapped["data"]
    if etype == "command/done" and "sourceEventSeq" in data:
        label = f"{etype} {seq} sourceEventSeq"
        data = {**data, "sourceEventSeq": _map_one(data["sourceEventSeq"], old_to_new, label)}
    elif etype in ("compaction/prune", "compaction/summary"):
        label = f"{etype} {seq} shadowedRange"
        shadowed_range = dict(data.get("shadowedRange") or {})
        if shadowed_range:
            shadowed_range["start"] = _map_one(shadowed_range["start"], old_to_new,
                                               f"{label} start")
            shadowed_range["end"] = _map_one(shadowed_range["end"], old_to_new, f"{label} end")
        updates: dict[str, Any] = {"shadowedRange": shadowed_range}
        if "shadowedSeqs" in data:
            updates["shadowedSeqs"] = _map_list(data["shadowedSeqs"], old_to_new,
                                                f"{etype} {seq} shadowedSeqs")
        data = {**data, **updates}
    elif etype in ("session/title", "session/title-llm-request") and "messageSeqs" in data:
        label = f"{etype} {seq} messageSeqs"
        data = {**data, "messageSeqs": _map_list(data["messageSeqs"], old_to_new, label)}
    remapped["data"] = data
    return remapped


def _migrate_header_v1_v2(header: dict) -> dict:
    assert_released_v1_header(header)
    return {**header, "version": 2}


def _migrate_v1_v2(artifact: dict) -> dict:
    """v1 → v2：chunk 流内嵌 + attempt 结算 + cut 重导出 + 密集重映射。"""
    header = artifact["header"]
    events = artifact["events"]
    old_cut = artifact["inherited_event_count"]
    # ① 源 v1 精确校验（上游 migrate：assertReleasedV1Artifact）
    assert_released_v1_artifact(artifact)
    # ② 封闭词表：表外事件即使 ignorable 也拒
    for event in events:
        if event["type"] not in _disp.RELEASED_V0_EVENT_DISPOSITIONS:
            raise unsupported(
                f'format v1 contains unknown event type "{event["type"]}" at seq {event["seq"]}')
    # ③ attempt 分组
    groups, by_chunk, by_message = _collect_attempt_groups(events)
    # ④ 切割拒绝：组内成员跨 cut 即拒（源 seq 语义）
    for group in groups:
        members = group.member_seqs()
        if any(member < old_cut for member in members) \
                and any(member >= old_cut for member in members):
            raise unsupported(
                f"inherited Session cut {old_cut} splits one Assistant attempt")
    # ⑤ staging
    staged = _stage_events(events, groups, by_chunk, by_message, header, old_cut)
    # ⑥ cut 重导出：新 cut = **真实** origin<旧cut 的条数（合成 marker origin=-1
    # 恰落在 cut 索引上、不计入——上游向量：空种子 cut=0 + marker@0；保留前缀
    # cut=1 + marker@1；marker 重打标 time 保真）
    new_cut = sum(1 for origin, _ in staged if 0 <= origin < old_cut)
    if header.get("isSeeded"):
        marker_at_cut = old_cut < len(events) and events[old_cut].get("type") == "session/end-seed"
        if not marker_at_cut:
            if old_cut < len(events):
                marker_time = events[old_cut]["time"]
            elif old_cut > 0:
                marker_time = events[old_cut - 1]["time"]
            else:
                marker_time = header["createdAt"]
            staged.insert(new_cut, (-1, {
                "type": "session/end-seed", "seq": new_cut, "time": marker_time,
                "data": {"inherited": True},
            }))
    # ⑦ 密集化：先定终序 index（后续引用 label 用新 index）
    dense = [(index, origin, event) for index, (origin, event) in enumerate(staged)]
    # ⑧ old→new 映射：先删全部组 chunk seq（消费引用拒、绝不重定向）
    old_to_new = {origin: index for index, origin, _ in dense if origin >= 0}
    for group in groups:
        for chunk_seq, _, _ in group.chunks:
            old_to_new.pop(chunk_seq, None)
    # ⑨ 密集重映射 + seq 覆盖
    target_events = []
    for index, _, event in dense:
        remapped = _remap_references(event, old_to_new)
        remapped["seq"] = index
        target_events.append(remapped)
    target = {"header": _migrate_header_v1_v2(header),
              "inherited_event_count": new_cut,
              "events": target_events}
    # ⑩ 目标快照 + v2 精确校验（上游：snapshot released v1-to-v2 target → assertReleasedV2Artifact）
    target = snapshot_json(target, "released v1-to-v2 target")
    assert_released_v2_artifact(target)
    return target


V1_TO_V2 = {
    "name": "@deepseek-ai/dsh-session-format-v1-to-v2",
    "from_version": 1,
    "to_version": 2,
    "migrate_header": _migrate_header_v1_v2,
    "migrate": _migrate_v1_v2,
}
