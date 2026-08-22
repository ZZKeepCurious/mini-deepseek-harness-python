"""可重放、无模型的 tool-result 裁剪服务（对齐 packages/compaction/compaction-tool-result-pruner）。

硬性规定（与上游逐条一致）：
  1. measure_content 按 Unicode 码点计价（非 UTF-16 单元），非 text 块计 0
  2. prune_content：在预算内（<= thresholdChars）返回 None（不裁剪）；超预算时保留
     headChars + 固定标记 + tailChars，把中间替换为 PRUNE_MARKER
  3. prune_session：对当前 surface 上的每个 tool/result 节点做裁剪；每个替换保留
     完整事件数据（仅 content 变），并紧邻一个 log-only 的 ``compaction/prune``
     影子计价事件（经 tokenMeter 计价被遮蔽节点），随后一个带 replace surfaceOp +
     sourceEventSeqs 的 tool/result 替换事件——纯消费者可据影子事件扣减，无需
     逐节点状态
  4. 替换必须满足 surface 重写规则（恰好遮蔽 1 个节点、目标为 tool/result、除
     content 外深相等）；不满足则上游 fail loud，mini 由 session.append 的
     assertToolResultRewrite 同款校验兜住
"""
from __future__ import annotations

from typing import Any

from ..core.session import Session
from ..llm.token_meter import TokenMeter

__all__ = [
    "PRUNE_MARKER",
    "DEFAULTS",
    "code_point_length",
    "resolve_config",
    "ToolResultPruner",
    "install_tool_result_pruner",
]

# 每个被移除的中间片段统一替换为固定标记（上游 config.ts PRUNE_MARKER 逐字）
PRUNE_MARKER = "\n\n[... tool result middle pruned ...]\n\n"

# 低摩擦默认（上游 DEFAULTS：coding-agent 工具输出）
DEFAULTS = {
    "thresholdChars": 8192,
    "headChars": 4096,
    "tailChars": 1024,
}

_CONFIG_KEYS = frozenset({"thresholdChars", "headChars", "tailChars"})


def code_point_length(text: str) -> int:
    """按 Unicode 码点计数（Python str 已是码点序列，天然不劈裂代理对）。"""
    return len(text)


def resolve_config(config: dict | None = None) -> dict:
    """解析并校验裁剪预算（对齐上游 config.ts resolveConfig）。"""
    config = config or {}
    for key in config:
        if key not in _CONFIG_KEYS:
            raise ValueError(
                f'ToolResultPruneConfig: unknown key "{key}" '
                f"(allowed: thresholdChars, headChars, tailChars)"
            )
    resolved = {
        "thresholdChars": config.get("thresholdChars", DEFAULTS["thresholdChars"]),
        "headChars": config.get("headChars", DEFAULTS["headChars"]),
        "tailChars": config.get("tailChars", DEFAULTS["tailChars"]),
    }
    _assert_int("thresholdChars", resolved["thresholdChars"], positive=True)
    _assert_int("headChars", resolved["headChars"], positive=False)
    _assert_int("tailChars", resolved["tailChars"], positive=False)

    emitted = resolved["headChars"] + code_point_length(PRUNE_MARKER) + resolved["tailChars"]
    if emitted > resolved["thresholdChars"]:
        raise ValueError(
            f"ToolResultPruneConfig: headChars + marker + tailChars ({emitted}) "
            f"must be at most thresholdChars ({resolved['thresholdChars']})"
        )
    return resolved


def _assert_int(name: str, value: Any, positive: bool) -> None:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"ToolResultPruneConfig: {name} ({value!r}) must be an integer")
    if positive and value <= 0:
        raise ValueError(f"ToolResultPruneConfig: {name} ({value}) must be a positive integer")
    if not positive and value < 0:
        raise ValueError(f"ToolResultPruneConfig: {name} ({value}) must be a non-negative integer")


class ToolResultPruner:
    """无模型 tool-result 裁剪服务（上游 ToolResultPruner extends Service）。

    mini 不引入 ctx.tokenMeter 服务依赖——直接持有一个 TokenMeter 实例做影子计价，
    与 CompactionEngine 同款（上游注入 ctx.tokenMeter，计价语义等价）。
    """

    # 上游 static inject = ['tokenMeter']；mini 用局部实例等价承载
    Config = staticmethod(resolve_config)

    def __init__(self, ctx=None, config: dict | None = None):
        self.ctx = ctx
        self.config = resolve_config(config)
        self.meter = TokenMeter()

    def measure_content(self, blocks: list[dict]) -> int:
        """按 Unicode 码点测量文本内容；非 text 块计 0。"""
        chars = 0
        for block in blocks:
            if block.get("type") == "text":
                chars += code_point_length(block.get("text", ""))
        return chars

    def prune_content(self, blocks: list[dict]) -> list[dict] | None:
        """确定性 head/middle/tail 裁剪；预算内返回 None（不裁剪）。

        按 Unicode 码点切片（非 UTF-16 单元），保留块顺序与非 text 块。
        """
        total = self.measure_content(blocks)
        if total <= self.config["thresholdChars"]:
            return None

        removed_start = self.config["headChars"]
        removed_end = total - self.config["tailChars"]
        pruned: list[dict] = []
        consumed = 0
        marker_inserted = False

        for block in blocks:
            if block.get("type") != "text":
                pruned.append(block)
                continue
            points = list(block.get("text", ""))  # 码点序列
            block_start = consumed
            block_end = block_start + len(points)
            head_end = min(len(points), max(0, removed_start - block_start))
            tail_start = min(len(points), max(0, removed_end - block_start))
            intersects = block_start < removed_end and block_end > removed_start
            marker = PRUNE_MARKER if (intersects and not marker_inserted) else ""
            if marker:
                marker_inserted = True
            text = "".join(points[:head_end]) + marker + "".join(points[tail_start:])
            if text:
                pruned.append({**block, "text": text})
            consumed = block_end

        if not marker_inserted:
            raise ValueError("tool-result prune: failed to locate the removed text span")
        after = self.measure_content(pruned)
        if after > self.config["thresholdChars"] or after >= total:
            raise ValueError("tool-result prune: replacement must be smaller and within threshold")
        return pruned

    def prune_session(self, session: Session) -> dict:
        """对一个稳定当前-surface 快照里的每个超预算 tool/result 做裁剪。

        返回 {pruned, charsRemoved}；每个替换前紧邻一个 ``compaction/prune``
        影子计价事件，本次 pass 已落地的替换 durablely 保留。
        """
        candidates: list[tuple[int, dict]] = []
        for node in session.surface_nodes():
            seq = node["seq"]
            event = session.events[seq]
            if event is not None and event.get("type") == "tool/result":
                candidates.append((seq, event))

        pruned: list[dict] = []
        chars_removed = 0
        for seq, event in candidates:
            message = event["data"]["message"]
            result_block = message["content"][0]
            inner = result_block["content"]
            content = self.prune_content(inner)
            if content is None:
                continue
            chars_before = self.measure_content(inner)
            chars_after = self.measure_content(content)
            # source 是上游冻结的 mappingproxy，重发事件须转回可 JSON 序列化的普通 dict
            new_message = {
                **message,
                "content": [{**result_block, "content": content}],
                "source": dict(message.get("source") or {}),
            }
            # 影子计价协议：计价事件与替换同步相邻追加
            session.append("compaction/prune", {
                "shadowedRange": {"start": seq, "end": seq},
                "shadowedSeqs": [seq],
                "shadowedTokenCount": self.meter.estimate_message(message),
            })
            replacement = session.append(
                "tool/result",
                {**event["data"], "message": new_message},
                surfaceOp={"op": "replace", "start": seq, "end": seq},
                sourceEventSeqs=[seq],
            )
            source = message.get("source") or {}
            pruned.append({
                "originalSeq": seq,
                "replacementSeq": replacement["seq"],
                "callId": source.get("callId"),
                "charsBefore": chars_before,
                "charsAfter": chars_after,
            })
            chars_removed += chars_before - chars_after
        return {"pruned": pruned, "charsRemoved": chars_removed}


def install_tool_result_pruner(ctx, config: dict | None = None):
    """幂等装配 toolResultPruner 服务（对齐上游插件 apply）。"""
    if getattr(ctx, "_miniharness_pruner_installed", False):
        return
    ctx._miniharness_pruner_installed = True
    ctx.provide("toolResultPruner", ToolResultPruner(ctx, config))
