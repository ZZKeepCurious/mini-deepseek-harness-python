"""图像卸载执行器（compaction seam 插件）。

对应 dsh 真实源码：packages/compaction/compaction-image-offload/src/{index,image-offload}.ts。

当 image-capable 路由以 `IMAGE_OFFLOAD_REQUIRED` 拒绝请求时，插件记录一条
`image/offload` 决策，选中当前请求顺序里最旧的 retained 输入 occurrence，
并经 agent 或 compaction summary 的 request-error waterfall 重试。后续请求
对这些 occurrence 发送占位文本（见 `core/session/projections.py`）。

这是 durable surface 修复，不是 provider 重试：不消耗重试预算，也不落
retry 事件。
"""
from __future__ import annotations

from typing import Any

from ..core.session import default_message_projections
from ..core.session.surface import _surface_nodes, derive_event_message
from ..llm.protocol import IMAGE_OFFLOAD_REQUIRED

__all__ = [
    "IMAGE_OFFLOAD_REQUIRED",
    "install_image_offload",
    "offload_oldest_images",
]


def _collect_retained(blocks: list, state: dict, seq: int) -> list[int]:
    """深度优先收集一个消息里最旧的 retained（非 offloaded）图片序号。"""
    indexes: list[int] = []
    for block in blocks or []:
        if state["count"] == 0:
            break
        btype = block.get("type")
        if btype == "image":
            if block.get("offloaded") is not True:
                indexes.append(state["image"])
                state["count"] -= 1
            state["image"] += 1
    return indexes


def offload_oldest_images(session, source_event_seqs: list[int], count: int) -> bool:
    """记录一条卸载最旧 retained 输入图片 occurrence 的决策（上游 offloadOldestImages）。

    图片序号计每个消息内所有 occurrence（含此前已 offloaded 的）；assistant
    节点承载模型输出被排除。

    @param session - 下一请求将应用该决策的会话。
    @param source_event_seqs - 失败请求顺序中的输入消息事件 seq。
    @param count - 适配器还需要的额外 retained occurrence 数。
    @returns 是否仍有 occurrence 可卸载。
    """
    targets: list[dict] = []
    state = {"count": count, "image": 0}
    for seq in source_event_seqs:
        if state["count"] == 0:
            break
        event = session.event_at(seq)
        if event is None or event["type"] not in ("user/message", "tool/result"):
            continue
        message = derive_event_message(event)
        if message is None:
            continue
        state["image"] = 0
        image_indexes = _collect_retained(message.get("content") or [], state, seq)
        if image_indexes:
            targets.append({"seq": seq, "imageIndexes": image_indexes})
    if not targets:
        return False
    session.append("image/offload", {"targets": targets})
    return True


def install_image_offload(ctx) -> Any:
    """在 ctx 上安装图像卸载恢复监听（上游 apply）。

    监听 `agent/request-error`：`IMAGE_OFFLOAD_REQUIRED` 且携带
    `offloadImages` 计数时，对当前 surface 输入节点记录一次 `image/offload`
    决策并返回 `{kind:'retry'}`；无可卸载 occurrence 则委派下游（next）。

    @param ctx - 拥有监听器的插件上下文。
    @returns disposer。
    """
    def on_request_error(payload: dict, next_fn):
        agent = payload.get("agent")
        failure = payload.get("failure")
        if (agent is None or failure is None
                or getattr(failure, "code", None) != IMAGE_OFFLOAD_REQUIRED):
            return next_fn()
        offload_count = getattr(failure, "offload_images", None)
        if offload_count is None:
            return next_fn()
        nodes = agent.session.surface_nodes()
        if not offload_oldest_images(agent.session, [node["seq"] for node in nodes],
                                     offload_count):
            return next_fn()
        return {"kind": "retry"}

    return ctx.on("agent/request-error", on_request_error)
