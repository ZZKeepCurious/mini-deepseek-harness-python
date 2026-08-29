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
from .surface import _surface_nodes, assert_provenance, assert_tool_result_rewrite
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

    def request_header(self) -> dict | None:
        """最近一次 request/header 的 header 对象（上游 session.ts requestHeader()）。

        尚无 header 返回 None（上游返回 undefined）；其 reason 可为
        initial/resume/change/series，可选带 startsSeries:true
        （RequestHeaderReason，session/types.ts:205-213）。
        """
        for event in reversed(self._events):
            if event["type"] == "request/header":
                return event["data"].get("header")
        return None

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

        事件 seq 即 append 前的日志长度；replace 的 sourceEventSeqs 必须覆盖
        当前 surface 上被遮蔽的全部节点（上游 surface.ts assertProvenance +
        assertToolResultRewrite，fail-closed——不满足即拒绝 append）。
        """
        payload = validate_event(type_, data, surfaceOp, sourceEventSeqs)
        if surfaceOp is not None and surfaceOp != "append":
            # replace 必须命中当前 surface 上已存在的 start/end 区间
            start, end = surfaceOp["start"], surfaceOp["end"]
            surface = _surface_nodes(self._events)
            shadowed = [node["seq"] for node in surface if start <= node["seq"] <= end]
            if surfaceOp["op"] == "replace" and not shadowed:
                raise ValueError(f"surface replace: seqs {start}-{end} not found in surface")
            assert_provenance(type_, sourceEventSeqs, self.seq, shadowed)
            event_probe = {"type": type_, "data": data or {}}
            assert_tool_result_rewrite(event_probe, shadowed, list(self._events))
        else:
            # append 事件同样校验血统：sourceEventSeqs 必须早于当前 seq
            assert_provenance(type_, sourceEventSeqs, self.seq, [])
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
        每个 seed 事件走与 append 相同的 surface 契约校验（surfaceOp /
        sourceEventSeqs 血统 / tool-result 重写规则，fail-closed）。
        """
        for i, ev in enumerate(seed):
            if not isinstance(ev, (dict, MappingProxyType)) or ev.get("seq") != i:
                raise ValueError(f"seed 事件 seq 必须从 0 连续，第 {i} 条不符")
            etype = ev.get("type")
            if etype not in KNOWN_TYPES:
                raise ValueError(f"未知事件类型: {etype!r}")
            data = ev.get("data", {})
            surface_op = ev.get("surfaceOp")
            source_seqs = ev.get("sourceEventSeqs")
            if etype in SURFACE_TYPES:
                if surface_op not in ("append",) and not (
                    isinstance(surface_op, (dict, MappingProxyType))
                    and surface_op.get("op") == "replace"
                ):
                    raise ValueError(f"surface 事件 {etype} 必须带合法 surfaceOp")
                if surface_op != "append":
                    self._replace_count += 1
                    start, end = surface_op["start"], surface_op["end"]
                    surface = _surface_nodes(list(self._events))
                    shadowed = [node["seq"] for node in surface if start <= node["seq"] <= end]
                    if not shadowed:
                        raise ValueError(f"surface replace: seqs {start}-{end} not found in surface")
                    assert_provenance(etype, source_seqs, i, shadowed)
                    assert_tool_result_rewrite(dict(ev), shadowed, list(self._events))
                else:
                    assert_provenance(etype, source_seqs, i, [])
            else:
                if surface_op is not None or source_seqs is not None:
                    raise ValueError(f"非 surface 事件 {etype} 不允许携带 surfaceOp/sourceEventSeqs")
            if not is_json_safe(thaw(ev)):
                raise TypeError(f"seed 事件必须可无损 JSON 序列化: {ev!r}")
            self._events.append(deep_freeze(ev))
        if not seed or self._events[-1]["type"] != "session/end-seed":
            # time 取当前时钟（上游 append 一律 Date.now()，end-seed 不复用 last_time）
            marker = deep_freeze({"type": "session/end-seed", "seq": self.seq, "time": now_ms(), "data": {}})
            self._events.append(marker)