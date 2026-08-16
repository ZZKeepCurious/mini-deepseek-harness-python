"""可继续子代理描述符：`subagent/descriptor` 事件的快照 / 折叠 / 校验 / 播种。

上游对照：packages/subagent/subagent/src/descriptor.ts（已核实）：

  * 描述符是会话内 model-hidden、log-only 非 surface 事件，**首条权威**
    （FIRST-authoritative）：折叠时取第一条 `subagent/descriptor`，之后
    再出现即视为会话损坏 → None（冷恢复失败）。
  * snapshotSubagentDescriptor = { ...descriptor, version: 2 }；
    种子轮 = Session.create + append('subagent/descriptor', snapshot)。
  * foldSubagentDescriptor(events) 返回 { descriptor | None }：
    没有事件 / 没有描述符事件 / 版本不符 / schema 不符 → None。

mini 子集：descriptor 字段 {kind, mode, label?, agentProvider?, agentModel?,
persona?, toolFilter?}；mini 只产出 kind='continuable'、mode='continuable'
（上游 'fork'/'provider' 通道由 providers.py 承载，不走描述符续跑）。
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any

from ...core.session import Session, is_json_safe, thaw

__all__ = [
    "SUBAGENT_DESCRIPTOR_VERSION",
    "fold_subagent_descriptor",
    "parse_subagent_descriptor",
    "seed_descriptor_turn",
    "snapshot_subagent_descriptor",
]

SUBAGENT_DESCRIPTOR_VERSION = 2

# 描述符字段的 JSON Schema（subset）。descriptor 本身是事件 data，必须
# 可无损 JSON 序列化（Session.append 已强制），这里只做形状/取值校验。
_DESCRIPTOR_SCHEMA = {
    "type": "object",
    "required": ["kind", "mode"],
    "properties": {
        "kind": {"type": "string"},
        "mode": {"type": "string"},
        "label": {"type": "string"},
        "agentProvider": {"type": "string"},
        "agentModel": {"type": "string"},
        "persona": {"type": "string"},
        "toolFilter": {"type": "array", "items": {"type": "string"}},
    },
}


def snapshot_subagent_descriptor(descriptor: dict) -> dict:
    """把描述符扩展成带版本号的事件载荷（{...descriptor, version}）。

    对齐上游 snapshotSubagentDescriptor：版本号跟随快照进日志，折叠时
    据此决定是否接受。
    """
    return {**dict(descriptor), "version": SUBAGENT_DESCRIPTOR_VERSION}


def parse_subagent_descriptor(payload: Any) -> dict | None:
    """校验并归一化一个描述符载荷；不合法 → None（fail-closed）。

    版本号必须恰为当前版本（上游 fold 对版本不符直接弃用）；kind/mode
    必须是已知取值。mini 只认识 'continuable' 续跑描述符。
    """
    if not isinstance(payload, (dict, MappingProxyType)):
        return None
    if payload.get("version") != SUBAGENT_DESCRIPTOR_VERSION:
        return None
    if payload.get("kind") != "continuable":
        return None
    if payload.get("mode") != "continuable":
        return None
    tool_filter = payload.get("toolFilter")
    if tool_filter is not None:
        if not isinstance(tool_filter, list) or not all(isinstance(t, str) for t in tool_filter):
            return None
    for key in ("label", "agentProvider", "agentModel", "persona"):
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            return None
    # 冻结（MappingProxyType）时 json.dumps 不可用；以解冻形态校验
    if not is_json_safe(thaw(payload)):
        return None
    return payload


def fold_subagent_descriptor(events) -> dict | None:
    """沿事件日志折叠描述符：首条 `subagent/descriptor` 权威。

    对齐上游 foldSubagentDescriptor：第一条即终态，之后重复出现视为
    会话损坏返回 None；无描述符 / 校验失败同样 None（冷恢复据此判定
    NOT_RESUMABLE）。
    """
    seen = None
    for ev in events:
        if not isinstance(ev, (dict, MappingProxyType)):
            continue
        if ev.get("type") != "subagent/descriptor":
            continue
        if seen is not None:
            return None
        seen = ev
    if seen is None:
        return None
    return parse_subagent_descriptor(seen.get("data"))


def seed_descriptor_turn(session: Session, descriptor: dict) -> dict:
    """把描述符播种进会话：append 'subagent/descriptor'（log-only 非 surface）。

    对齐上游 seedDescriptorTurn：调用方保证 session 处于"空恢复"或
    "已带 end-seed 标记"状态；重复播种会破坏首条权威 → 由调用方负责
    （此处不设防）。
    """
    return session.append("subagent/descriptor", snapshot_subagent_descriptor(descriptor))
