"""可继续子代理描述符：`subagent/descriptor` 事件的快照 / 折叠 / 校验 / 播种。

上游对照：packages/subagent/subagent/src/descriptor.ts（已核实，2026-08-18 对齐清零轮）：

  * 描述符是会话内 model-hidden、log-only 非 surface 事件，由建立 provider
    在子会话初始回合内追加恰好一条。
  * SUBAGENT_DESCRIPTOR_VERSION = 2；载荷 schema：
      - 必填 {version, mode: 'one-shot'|'continuable', provider}
      - continuable 必填 label（one-shot 可选）
      - 可选 agentProvider / agentModel / persona（字符串）
      - 可选 toolFilter: {allow?: string[], deny?: string[]}（至少声明一项）
    字段集封闭：未知字段拒绝（上游 assertKnownKeys throws；mini fail-closed → None）。
  * foldSubagentDescriptor 用 find() 取**第一条**权威——建立 provider 恰好
    追加一条，之后同型事件不能改写已声明的组合（重复事件被无视，非损坏）。
  * 版本不符 → undefined（本运行时不能分类该子代理）。

mini 差异（fail-closed 简化，标注）：上游对当前版本的 schema 违规抛错，
mini 一律折叠为 None（冷恢复据此判 NOT_RESUMABLE）；mini 只产出 continuable
描述符（provider 恒 'in-process'），one-shot 描述符可解析、不可续跑。
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any

from ...core.session import Session, is_json_safe, thaw

__all__ = [
    "CONTINUATION_PROVIDER",
    "SUBAGENT_DESCRIPTOR_VERSION",
    "fold_subagent_descriptor",
    "parse_subagent_descriptor",
    "seed_descriptor_turn",
    "snapshot_subagent_descriptor",
]

SUBAGENT_DESCRIPTOR_VERSION = 2

# mini 续跑通道的建立 provider 名（上游为 ctx.subagents 的 provider 名，
# 如 fork-in-process / acp / dsh-sdk；mini continuation 是进程内通道）
CONTINUATION_PROVIDER = "in-process"

_BASE_KEYS = frozenset({"version", "mode", "provider", "label"})
_ONE_SHOT_KEYS = _BASE_KEYS
_CONTINUABLE_KEYS = _BASE_KEYS | {"agentProvider", "agentModel", "persona", "toolFilter"}
_TOOL_FILTER_KEYS = frozenset({"allow", "deny"})


def snapshot_subagent_descriptor(descriptor: dict) -> dict:
    """把描述符扩展成带版本号的事件载荷（{...descriptor, version}）。

    对齐上游 snapshotSubagentDescriptor：版本号跟随快照进日志，折叠时
    据此决定是否接受。
    """
    return {**dict(descriptor), "version": SUBAGENT_DESCRIPTOR_VERSION}


def _tool_filter_valid(tool_filter: Any) -> bool:
    """toolFilter 形状校验：{allow?, deny?}，值均为字符串数组，至少一项。"""
    if not isinstance(tool_filter, (dict, MappingProxyType)):
        return False
    keys = set(tool_filter)
    if not keys or not keys <= _TOOL_FILTER_KEYS:
        return False
    for key in ("allow", "deny"):
        if key in tool_filter:
            value = tool_filter[key]
            if not isinstance(value, (list, tuple)) or not all(
                    isinstance(t, str) for t in value):
                return False
    return True


def parse_subagent_descriptor(payload: Any) -> dict | None:
    """校验一个描述符载荷；不合法 → None（fail-closed，见模块头差异标注）。

    版本号必须恰为当前版本；mode ∈ {'one-shot','continuable'}；provider
    必填字符串；continuable 的 label 必填；toolFilter 为 {allow?,deny?}。
    """
    if not isinstance(payload, (dict, MappingProxyType)):
        return None
    if payload.get("version") != SUBAGENT_DESCRIPTOR_VERSION:
        return None
    mode = payload.get("mode")
    if mode not in ("one-shot", "continuable"):
        return None
    allowed_keys = _ONE_SHOT_KEYS if mode == "one-shot" else _CONTINUABLE_KEYS
    if not set(payload) <= allowed_keys:
        return None  # 未知字段拒绝（上游 assertKnownKeys）
    if not isinstance(payload.get("provider"), str):
        return None
    if mode == "continuable" and not isinstance(payload.get("label"), str):
        return None
    if mode == "one-shot":
        label = payload.get("label")
        if label is not None and not isinstance(label, str):
            return None
    for key in ("agentProvider", "agentModel", "persona"):
        value = payload.get(key)
        if value is not None and not isinstance(value, str):
            return None
    if "toolFilter" in payload and not _tool_filter_valid(payload.get("toolFilter")):
        return None
    # 冻结（MappingProxyType）时 json.dumps 不可用；以解冻形态校验
    if not is_json_safe(thaw(payload)):
        return None
    return payload


def fold_subagent_descriptor(events) -> dict | None:
    """沿事件日志折叠描述符：**第一条** `subagent/descriptor` 权威。

    对齐上游 foldSubagentDescriptor（find 首条权威）：建立 provider 恰好
    追加一条，之后重复的同型事件被无视（不能改写已声明的组合，也不是
    损坏）。无描述符 / 校验失败 → None（冷恢复据此判定 NOT_RESUMABLE）。
    """
    for ev in events:
        if not isinstance(ev, (dict, MappingProxyType)):
            continue
        if ev.get("type") != "subagent/descriptor":
            continue
        return parse_subagent_descriptor(ev.get("data"))
    return None


def seed_descriptor_turn(session: Session, descriptor: dict) -> dict:
    """把描述符播种进会话：append 'subagent/descriptor'（log-only 非 surface）。

    对齐上游 seedDescriptorTurn：调用方保证 session 处于"空恢复"或
    "已带 end-seed 标记"状态；重复播种会破坏首条权威 → 由调用方负责
    （此处不设防）。
    """
    return session.append("subagent/descriptor", snapshot_subagent_descriptor(descriptor))
