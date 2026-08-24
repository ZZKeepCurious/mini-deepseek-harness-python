"""loop 侧运行时上下文投影（上游 packages/core/agent-loop/src/runtime-context.ts
RuntimeContextProjection 同款）。

职责：跟踪会话日志里最近一条"仍可见"的 runtime-context 快照 user 消息；
project() 仅在渲染结果变化时铸出候选快照消息，由调用方（AgentLoop pre-step）
落盘为 durable user/message（模型可见 ⟺ 已记录）。投影类自身不提交任何事件。

关键契约（逐条对齐上游 runtime-context.ts）：
  * SOURCE：owned 判定 = ``source.kind == 'plugin'`` 且 ``plugin == SOURCE``
    （上游保留 npm 包名字面量；mini system 基底消息的教学短名
    'system-prompt' 是既有简化，不属本契约）。
  * CLEARED：全部动态上下文消失时的哨兵文本，逐字对齐。
  * retained 三态：_NEVER（从未有过快照，对应上游 undefined）/
    None（曾有但已被 replace 遮蔽）/ {"seq", "text"}。restore 从日志倒序找
    最近一条 seq 仍在当前 surface 上的 owned 快照（被压缩遮蔽的更新快照
    跳过、继续向前找）；随后按追加序跟随权威 session 日志增量更新。
  * project(current, sections)：retained.text 与本次渲染相等 → None（去重，
    不重复注入相同快照）；sections 为空时消息 source 不带 form/sections
    （cleared 哨兵无节可归因）。
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..session import create_message, text_block
from ..session.surface import is_replace_op
from ..session.types import SURFACE_TYPES

SOURCE = "@deepseek-ai/dsh-system-prompt"

# 上游 CLEARED 常量（逐字）
CLEARED = (
    "Current runtime context: none. "
    "Earlier runtime-context snapshots no longer apply."
)

# 上游 retained === undefined 的三态哨兵（模块私有）
_NEVER = object()


def _is_owned(message: Any) -> bool:
    """owned 快照判定（上游 isOwned）。事件日志冻结为 mappingproxy，
    故按 Mapping 判形而非 dict。"""
    source = message.get("source")
    return (
        isinstance(source, Mapping)
        and source.get("kind") == "plugin"
        and source.get("plugin") == SOURCE
    )


def _snapshot_text(message: dict) -> str:
    """单 text block 的文本；否则空串（上游 textOf 返回 undefined。空串参与
    去重比较的行为与 undefined 等价：snapshot 恒非空串，畸形 owned 消息
    （单块非 text / 多块）两种实现下都不会命中去重）。"""
    content = message.get("content") or []
    if (
        len(content) == 1
        and isinstance(content[0], Mapping)
        and content[0].get("type") == "text"
    ):
        return content[0].get("text", "")
    return ""


def _is_replacement(event: dict) -> bool:
    """replace 型 surface 事件判定（上游 isReplacementSurfaceEvent：
    surface 类型且 surfaceOp 非 'append'）。"""
    if event.get("type") not in SURFACE_TYPES:
        return False
    op = event.get("surfaceOp")
    return isinstance(op, Mapping) and is_replace_op(op)


class RuntimeContextProjection:
    """跟踪最近一条 retained 快照（不拥有其落盘）。

    @param session - 接收投影消息的 Session；restore 一次 + 之后在每次
    project() 前按追加序消化新事件（与上游 ctx.on('session/event') 监听
    等状态等价：日志是唯一权威、顺序全序，retained 仅在 project() 内读取，
    惰性消化不影响可观察行为）。
    """

    def __init__(self, session):
        self._session = session
        self._seen = 0
        events = session.events
        surface = {node["seq"] for node in session.surface_nodes()}
        self._retained: Any = _NEVER
        for index in range(len(events) - 1, -1, -1):
            event = events[index]
            if event.get("type") != "user/message":
                continue
            data = event.get("data") or {}
            if not _is_owned(data):
                continue
            if self._retained is _NEVER:
                self._retained = None  # 最新 owned 至少把状态抬到 null
            if event["seq"] in surface:
                self._retained = {
                    "seq": event["seq"],
                    "text": _snapshot_text(data),
                }
                break
        self._seen = len(events)

    def _catch_up(self) -> None:
        """消化上次以来追加的事件（上游 'session/event' 监听体的同序重放）。"""
        events = self._session.events
        while self._seen < len(events):
            event = events[self._seen]
            self._seen += 1
            data = event.get("data") or {}
            if event.get("type") == "user/message" and _is_owned(data):
                self._retained = {
                    "seq": event["seq"],
                    "text": _snapshot_text(data),
                }
            elif (
                self._retained is not _NEVER
                and self._retained is not None
                and _is_replacement(event)
                and self._retained["seq"] in (event.get("sourceEventSeqs") or [])
            ):
                self._retained = None

    def project(self, current: str, sections: list[dict]) -> dict | None:
        """仅在 retained 值变化时铸出未提交的快照消息。

        @param current - 完整渲染后的动态上下文文本（空串表示无活跃上下文）。
        @param sections - 构成当前快照的具名节 [{name, text}, ...]。
        @returns 候选 user 消息；无需更新时返回 None。
        """
        self._catch_up()
        if self._retained is _NEVER and current == "":
            return None
        snapshot = CLEARED if current == "" else current
        if self._retained is not _NEVER and self._retained is not None \
                and self._retained["text"] == snapshot:
            return None
        # cleared 哨兵无节可归因：不带 form/sections
        source = {"kind": "plugin", "plugin": SOURCE}
        if sections:
            source = {
                "kind": "plugin",
                "plugin": SOURCE,
                "form": "snapshot",
                "sections": [dict(s) for s in sections],
            }
        return create_message("user", [text_block(snapshot)], source)
