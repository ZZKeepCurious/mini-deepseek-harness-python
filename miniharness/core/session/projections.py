"""会话消息投影（SessionMessageProjection）：durable 事实 → 模型消息覆盖。

对应 dsh 真实源码：
  * `packages/core/session/src/surface.ts`（SessionMessageProjection / foldSurface 投影）；
  * `packages/compaction/compaction-image-offload/src/projection.ts`（imageOffloadProjection）；
  * `packages/compaction/compaction-image-offload/src/project-message.ts`（offloadMessageImages）。

投影是「纯重放」的：`image/offload` 事件记录一组 input-image occurrence，
后续请求组装时把这些 occurrence 标 `offloaded: true`（模型侧投影为占位文本）。
消息身份、原始事件字节、已派生快照都不改变；投影结果是新的冻结消息对象。

mini 载体：投影函数以 `MessageProjection` 协议（type + project）承载；
`fold_projections(events, nodes)` 按 surface 顺序折叠当前消息覆盖表。
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from .json import deep_freeze
from .types import MESSAGE_PROJECTION_EVENT_TYPES

__all__ = [
    "MESSAGE_PROJECTION_EVENT_TYPES",
    "ImageOffloadProjection",
    "MessageProjection",
    "default_message_projections",
    "fold_projections",
    "offload_message_images",
]


class MessageProjection:
    """一个 message-投影定义：type 是它解释的事件类型，project(事件, 上下文) 返回覆盖。

    上游 `SessionMessageProjection<'T'>`：`project` 收一个该类型事件与只读上下文
    （nodes/events/baseSeq/messages），返回 `Map<SessionSeq, Message>` 的覆盖项。
    mini 以 dict 承载（seq → 投影后消息）。
    """

    type: str

    def project(self, event: dict, context: dict) -> dict:
        raise NotImplementedError


def offload_message_images(message: dict, indexes: list[int]) -> dict:
    """把选中的 image occurrence 投影为不可变 offloaded 块（上游 offloadMessageImages）。

    @param message - 本次决策之前投影出的消息。
    @param indexes - 非空、严格递增的深度优先图片序号。
    @returns 身份不变、选中图片被标记的不可变消息。
    @throws 当选中的 occurrence 缺失或已 offloaded。
    """
    if not indexes:
        raise ValueError("image/offload: imageIndexes must be nonempty")
    state = {"image": 0, "selected": 0}

    def visit(blocks: list) -> list:
        next_blocks: list | None = None
        for index, block in enumerate(blocks or []):
            projected = block
            if block.get("type") == "image":
                if state["selected"] < len(indexes) and state["image"] == indexes[state["selected"]]:
                    if block.get("offloaded") is True:
                        raise ValueError(
                            f"image/offload: image index {state['image']} is already offloaded")
                    projected = {**block, "offloaded": True}
                    state["selected"] += 1
                state["image"] += 1
            if projected is not block:
                if next_blocks is None:
                    next_blocks = list(blocks[:index])
            if next_blocks is not None:
                next_blocks.append(projected)
        return next_blocks if next_blocks is not None else blocks

    content = visit(message.get("content") or [])
    if state["selected"] != len(indexes):
        raise ValueError(
            f"image/offload: image index {indexes[state['selected']]} does not exist")
    return deep_freeze({**message, "content": content})


def _is_index(value: Any) -> bool:
    return (isinstance(value, int) and not isinstance(value, bool) and value >= 0)


def _is_object(value: Any) -> bool:
    """durable JSON 对象判定：冻结事件 data 是 MappingProxyType，dict 判型会漏。"""
    return isinstance(value, Mapping)


def _is_sequence(value: Any) -> bool:
    """durable JSON 数组判定：冻结后 list 变 tuple（str/bytes 不算数组）。"""
    return isinstance(value, Sequence) and not isinstance(value, (str, bytes))


class ImageOffloadProjection(MessageProjection):
    """`image/offload` 的拆分校验与重建（上游 imageOffloadProjection）。

    事件 payload `{targets:[{seq, imageIndexes}]}`：targets 非空；每个 target
    恰 seq + imageIndexes 两键；seq 是当前 surface 上的 user/message 或
    tool/result 节点；imageIndexes 非空且严格递增。任一不符 fail loud（无部分应用）。
    """

    type = "image/offload"

    def project(self, event: dict, context: dict) -> dict:
        data = event.get("data")
        if (not _is_object(data) or len(data) != 1
                or not _is_sequence(data.get("targets")) or len(data["targets"]) == 0):
            raise ValueError("image/offload: data must contain a nonempty targets array")
        messages: dict = {}
        nodes = set(context["nodes"])
        for target in data["targets"]:
            if (not _is_object(target) or len(target) != 2
                    or not _is_index(target.get("seq"))
                    or not _is_sequence(target.get("imageIndexes"))
                    or len(target["imageIndexes"]) == 0):
                raise ValueError(
                    "image/offload: each target must contain a seq and nonempty imageIndexes")
            seq = target["seq"]
            if seq in messages:
                raise ValueError(f"image/offload: duplicate target seq {seq}")
            if seq not in nodes:
                raise ValueError(
                    f"image/offload: target seq {seq} is not a current surface node")
            source = context["event_at"](seq)
            if source is None or source.get("type") not in ("user/message", "tool/result"):
                raise ValueError(
                    f"image/offload: target seq {seq} must be user/message or tool/result")
            previous = -1
            for index in target["imageIndexes"]:
                if not _is_index(index) or index <= previous:
                    raise ValueError(
                        "image/offload: imageIndexes must be strictly increasing "
                        "non-negative safe integers")
                previous = index
            message = context["messages"].get(seq)
            if message is None:
                message = (source["data"] if source["type"] == "user/message"
                           else source["data"].get("message"))
            messages[seq] = offload_message_images(message, target["imageIndexes"])
        return messages


def default_message_projections() -> tuple[MessageProjection, ...]:
    """已安装的第一方 message 投影定义（上游 session-format-catalog 目录）。"""
    return (ImageOffloadProjection(),)


def fold_projections(events, nodes: list[dict],
                     projections: tuple[MessageProjection, ...] | None = None) -> dict:
    """折叠投影覆盖表：按事件顺序对每个投影类型累积 seq → 投影消息。

    上游 foldSurface：投影只对当前 surface 上的节点生效（replace 遮蔽的节点
    不再被替换），且投影按事件顺序叠加（后一次投影看到前一次的结果）。

    @param events - 全部已提交事件（按 seq）。
    @param nodes - 当前 surface 节点（含 seq）。
    @param projections - 投影定义；缺省用第一方目录。
    @returns `{seq: 投影后消息}`；无覆盖时为空 dict。
    """
    active = projections if projections is not None else default_message_projections()
    by_type = {projection.type: projection for projection in active}
    node_seqs = [node["seq"] for node in nodes]
    projected: dict = {}

    def event_at(seq: int):
        return next((ev for ev in events if ev["seq"] == seq), None)

    context = {
        "nodes": node_seqs,
        "events": events,
        "baseSeq": events[0]["seq"] if events else 0,
        "messages": projected,
        "event_at": event_at,
    }
    for event in events:
        if event["type"] not in MESSAGE_PROJECTION_EVENT_TYPES:
            continue
        projection = by_type.get(event["type"])
        if projection is None:
            continue
        if event["seq"] not in node_seqs:
            # 该事件不是 surface 事件（image/offload 本就 log-only），targets 仍指向
            # 当前 surface 节点，故无需自身在 surface 上。
            pass
        projected.update(projection.project(event, context))
    return projected


def project_message(message: dict) -> dict:
    """无投影时原样返回（供 derive_messages 的统一入口）。"""
    return message
