"""AgentLoop 拥有的会话投影单元。

对齐上游 `packages/core/agent-loop/src/{index,inbox}.ts`：
- `turnBoundaryProjectionDefinition`（host-only，stateVersion 2）：消费
  turn/start·end、step/start·end，投影 open turn 起始 seq、最近 step 起始
  seq、最近 step 边界与最近 turn 号。host-only（无 wire）——不进
  `session/projections` 客户端 cut，只作宿主读面（上游 agent.ts:133 用
  `stateOf(session, 'turnBoundary')?.lastTurn` 续 turn 号）。
- `inboxProjectionDefinition`（wire，stateVersion 1）：从 `agent/inbox/spliced`
  事件 fold 待处理双队列（next-turn/next-step）；wire 值即 fold 状态本身
  （每条 pending 消息都无损往返 session log）。与 `core/agent_loop/inbox.py`
  的 live 状态同源（都由同一批 splice 事件推演），注册后 `session/projections`
  暴露 `inbox` 单元，对齐上游 wire（`sessionProjections` snapshot 对带 wire
  单元无条件暴露）。

fold 为纯函数：未命中事件返回同一状态引用（注册表的 `is` 变更门）。
"""
from __future__ import annotations

from typing import Any

from ...session_projection import ProjectionDefinition

__all__ = [
    "TURN_BOUNDARY_STATE_VERSION",
    "INBOX_STATE_VERSION",
    "turn_boundary_projection",
    "inbox_projection",
]

TURN_BOUNDARY_STATE_VERSION = 2
INBOX_STATE_VERSION = 1

_INBOX_TARGETS = ("next-turn", "next-step")


def _init_turn_boundary(header: Any, inherited_event_count: int) -> dict:
    return {
        "openTurnStartSeq": None,
        "lastStepStartSeq": None,
        "lastStepBoundary": None,
        "lastTurn": 0,
    }


def _apply_turn_boundary(state: dict, event: dict) -> dict:
    etype = event.get("type")
    if etype == "turn/start":
        data = event.get("data") or {}
        return {
            **state,
            "openTurnStartSeq": event.get("seq"),
            "lastTurn": data.get("turn"),
        }
    if etype == "turn/end":
        return {**state, "openTurnStartSeq": None}
    if etype == "step/start":
        return {
            **state,
            "lastStepStartSeq": event.get("seq"),
            "lastStepBoundary": {"kind": "start", "seq": event.get("seq")},
        }
    if etype == "step/end":
        return {
            **state,
            "lastStepBoundary": {"kind": "end", "seq": event.get("seq")},
        }
    return state


def turn_boundary_projection() -> ProjectionDefinition:
    return ProjectionDefinition(
        "turnBoundary",
        init=_init_turn_boundary,
        apply=_apply_turn_boundary,
        state_version=TURN_BOUNDARY_STATE_VERSION,
    )


def _init_inbox(header: Any, inherited_event_count: int) -> dict:
    return {"next-turn": [], "next-step": []}


def _apply_inbox(state: dict, event: dict) -> dict:
    if event.get("type") != "agent/inbox/spliced":
        return state
    splice = event.get("data") or {}
    target = splice.get("target")
    if target not in _INBOX_TARGETS:
        raise ValueError(
            f"invalid persisted inbox splice at session seq {event.get('seq')}: "
            f"unknown target {target!r}")
    inbox = state[target]
    start = splice.get("start")
    removed_count = splice.get("removedCount", 0)
    if not isinstance(start, int) or isinstance(start, bool) or start < 0 \
            or start > len(inbox) \
            or not isinstance(removed_count, int) or isinstance(removed_count, bool) \
            or removed_count < 0 or start + removed_count > len(inbox):
        raise ValueError(
            f"invalid persisted inbox splice at session seq {event.get('seq')}: "
            f"start {start!r} removedCount {removed_count!r} out of bounds")
    inserted = splice.get("inserted")
    if not isinstance(inserted, (list, tuple)):
        raise ValueError(
            f"invalid persisted inbox splice at session seq {event.get('seq')}: "
            f"inserted must be an array")
    next_queue = inbox[:start] + [dict(m) for m in inserted] \
        + inbox[start + removed_count:]
    other = state["next-step"] if target == "next-turn" else state["next-turn"]
    ids = set()
    for message in [*next_queue, *other]:
        message_id = message.get("id")
        if message_id in ids:
            raise ValueError(
                f"invalid persisted inbox splice at session seq {event.get('seq')}: "
                f"message {message_id!r} is already pending")
        ids.add(message_id)
    if target == "next-turn":
        return {"next-turn": next_queue, "next-step": state["next-step"]}
    return {"next-turn": state["next-turn"], "next-step": next_queue}


def inbox_projection() -> ProjectionDefinition:
    return ProjectionDefinition(
        "inbox",
        init=_init_inbox,
        apply=_apply_inbox,
        state_version=INBOX_STATE_VERSION,
        view=lambda state: state,
    )