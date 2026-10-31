"""session/follow 可选 assistantStream 面（对齐 session-controller assistant-stream.ts）。

mini 载体：AgentLoop 尚未 emit `agent/assistant-stream`；此处提供 wire 就绪的
baseline（revision 0）与订阅挂点，后续 Agent 侧补事件即可增量闭合。
"""
from __future__ import annotations

from typing import Any, Callable

__all__ = ["assistant_stream_baseline", "bind_assistant_stream"]


def assistant_stream_baseline(_session_id: str) -> dict:
    return {"revision": 0}


def bind_assistant_stream(
        ctx, session_id: str, push_frame: Callable[[dict], None]) -> Callable[[], None]:
    """注册 assistant-stream 帧推送；当前无上游事件源时返回空 disposer。"""

    def on_frame(payload: dict) -> None:
        agent = payload.get("agent")
        frame = payload.get("frame")
        if getattr(agent, "id", None) != session_id or not isinstance(frame, dict):
            return
        push_frame({"type": "assistant-stream", "frame": frame})

    if not hasattr(ctx, "on"):
        return lambda: None
    try:
        return ctx.on("agent/assistant-stream", on_frame, global_=True)
    except Exception:  # noqa: BLE001 - 事件名未注册时保持 passive follow
        return lambda: None
