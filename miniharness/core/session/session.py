"""会话本体：追加式事件日志（唯一事实来源）。

上游对照：packages/core/session/src/index.ts 的 Session 类。事件信封
{type, seq, time, data}，seq == log.length；append / 恢复模式均走
invariant 校验，坏事件进不来。
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any, Callable

from .invariant import validate_event
from .json import deep_freeze, is_json_safe, now_ms, thaw
from .surface import _surface_nodes
from .types import KNOWN_TYPES, SURFACE_TYPES

__all__ = ["Session"]


class Session:
    """追加式事件日志：唯一事实来源。构造时可带 seed（恢复/回放历史）与 meta。

    meta 是会话的持久化头部元数据（上游 Session.header 的 mini 子集：
    parentSession / origin / delegationDepth / seedLength / cwd），冷恢复与
    子代理继承用；普通会话缺省为空 dict。

    on_append 是 store 发布钩子（SessionStore.enter 注入）：正常 append 落盘后
    回调（上游 index.ts 的 session/event 派发），构造 seed 回放不触发。
    """

    def __init__(self, session_id: str, seed: list | None = None, created_at: int | None = None,
                 meta: dict | None = None, on_append: Callable[[dict[str, Any]], None] | None = None):
        self.session_id = session_id
        self.created_at = created_at or now_ms()
        self.meta = dict(meta) if meta else {}
        self._events: list[dict[str, Any]] = []
        self._replace_count = 0
        self._on_append = on_append

        if seed:
            self._replay_seed(seed)

    @property
    def seq(self) -> int:
        return len(self._events)

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        """只读视图：外部永远拿不到可变的内部列表。"""
        return tuple(self._events)

    @property
    def replace_generation(self) -> int:
        """已提交的 surface 位置替换次数（上游 surface.ts SurfaceManager.replaceGeneration）。

        替换使 surface 上可见 seq 非单调；压缩的 overflow 恢复以此判断
        "surface 是否真的前进过"。
        """
        return self._replace_count

    def surface_nodes(self) -> list[dict]:
        """当前 surface 节点（含 seq，模型可见顺序）。

        沿日志折叠（上游 Session.surface.nodes），O(n)；token 计量与
        压缩据此选区间与验界。
        """
        return _surface_nodes(self._events)

    def append(self, type_: str, data: dict | None = None, surfaceOp=None,
               sourceEventSeqs: list[int] | None = None) -> dict[str, Any]:
        """源头校验 + 冻结：坏事件永远进不了日志。

        与上游 append(type, data, surfaceOp) 签名一致；surface 事件必须带
        surfaceOp（'append' 或 {op:'replace', start, end}），非 surface 事件
        禁止携带；sourceEventSeqs 仅 surface 事件可带（上游 SurfaceIntent）。
        """
        payload = validate_event(type_, data, surfaceOp, sourceEventSeqs)
        record = deep_freeze({"seq": self.seq, "time": now_ms(), **payload})
        self._events.append(record)
        if surfaceOp is not None and surfaceOp != "append":
            self._replace_count += 1
        if self._on_append is not None:
            self._on_append(record)
        return record

    def _replay_seed(self, seed: list) -> None:
        """恢复模式回放 seed：seq 必须从 0 连续、类型已知、surface 合法。

        与上游 restore 模式一致：冻结但不二次克隆；seed 末事件不是
        session/end-seed 时自动补记该标记（本进程首个 append 之前的边界）。
        """
        for i, ev in enumerate(seed):
            if not isinstance(ev, (dict, MappingProxyType)) or ev.get("seq") != i:
                raise ValueError(f"seed 事件 seq 必须从 0 连续，第 {i} 条不符")
            etype = ev.get("type")
            if etype not in KNOWN_TYPES:
                raise ValueError(f"未知事件类型: {etype!r}")
            data = ev.get("data", {})
            if etype in SURFACE_TYPES:
                op = ev.get("surfaceOp")
                if op not in ("append",) and not (isinstance(op, dict) and op.get("op") == "replace"):
                    raise ValueError(f"surface 事件 {etype} 必须带合法 surfaceOp")
                if op != "append":
                    self._replace_count += 1
            if not is_json_safe(thaw(ev)):
                raise TypeError(f"seed 事件必须可无损 JSON 序列化: {ev!r}")
            self._events.append(deep_freeze(ev))
        if not seed or self._events[-1]["type"] != "session/end-seed":
            # time 取当前时钟（上游 append 一律 Date.now()，end-seed 不复用 last_time）
            marker = deep_freeze({"type": "session/end-seed", "seq": self.seq, "time": now_ms(), "data": {}})
            self._events.append(marker)