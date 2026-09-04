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


def _json_deep_equal(a: Any, b: Any) -> bool:
    """JSON 值域深比较（null/bool/num/str、数组、普通对象；兼容冻结结构）。"""
    from types import MappingProxyType
    if isinstance(a, MappingProxyType):
        a = dict(a)
    if isinstance(b, MappingProxyType):
        b = dict(b)
    if type(a) is not type(b) and not (
        (isinstance(a, (dict, MappingProxyType)) and isinstance(b, (dict, MappingProxyType)))
        or (isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)))
    ):
        return False
    if isinstance(a, (dict, MappingProxyType)) and isinstance(b, (dict, MappingProxyType)):
        if set(a.keys()) != set(b.keys()):
            return False
        return all(_json_deep_equal(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        if len(a) != len(b):
            return False
        return all(_json_deep_equal(x, y) for x, y in zip(a, b))
    return a == b


class Session:
    """追加式事件日志：唯一事实来源。构造时可带 seed（恢复/回放历史）与 meta。

    meta 是会话的持久化头部元数据（上游 Session.header 的 mini 子集：
    parentSession / origin / delegationDepth / isSeeded / cwd），冷恢复与
    子代理继承用；普通会话缺省为空 dict。

    inherited_event_count：fork 继承前缀长度（对齐上游 Session.inheritedEventCount）。
    seeded session 必须有 seed 且 inherited_event_count > 0；
    unseeded session 的 inherited_event_count 恒为 0。

    on_append 是 store 发布钩子（SessionStore.enter 注入）：正常 append 落盘后
    回调（上游 index.ts 的 session/event 派发），构造 seed 回放不触发。
    """

    def __init__(self, session_id: str, seed: list | None = None, created_at: int | None = None,
                 meta: dict | None = None, on_append: Callable[[dict[str, Any]], None] | None = None,
                 inherited_event_count: int | None = None, mode: str = "snapshot"):
        self.session_id = session_id
        self.created_at = created_at or now_ms()
        self.meta = dict(meta) if meta else {}
        self._events: list[dict[str, Any]] = []
        self._replace_count = 0
        self._on_append = on_append

        # 对齐上游 isSeeded + inheritedEventCount 语义（rc.1）
        self._is_seeded: bool = self.meta.get("isSeeded", False)
        if inherited_event_count is not None:
            self._inherited_event_count = inherited_event_count
        elif seed and self._is_seeded:
            # seeded session 默认 inherited = seed 长度
            self._inherited_event_count = len(seed)
        else:
            self._inherited_event_count = 0

        # 上游构造不变量（V2 index.ts constructor）：seeded 允许显式空前缀
        # （[] 是合法 seed，仅缺失/None 拒绝；inheritedEventCount 可为 0）
        if self._is_seeded and seed is None:
            raise ValueError("seeded session requires an explicit constructor seed")
        if self._is_seeded and self._inherited_event_count < 0:
            raise ValueError("seeded session requires a non-negative inherited event count")
        if not self._is_seeded and self._inherited_event_count != 0:
            raise ValueError("unseeded session inherited event count must be 0")

        if seed:
            self._replay_seed(seed)

        # inherited_event_count 不得超过日志长度
        if self._inherited_event_count > len(self._events):
            raise ValueError(
                f"session inherited event count {self._inherited_event_count} "
                f"exceeds its event log length {len(self._events)}"
            )
        # V2: seeded snapshot 的 seed 必须恰好等于继承前缀（上游 index.ts
        # 'seeded session constructor seed must equal its inherited prefix'）
        if mode == "snapshot" and self._is_seeded and self._inherited_event_count != len(self._events):
            raise ValueError("seeded session constructor seed must equal its inherited prefix")

        # V2 end-seed marker（上游 index.ts constructor）：snapshot 的 seeded 子会话
        # 恒在继承切割点带 {inherited:true}；restore / unseeded 走普通 {} 边界
        # （上游 types.ts 'session/end-seed': { inherited?: true }——仅可选 true，
        # resume 标记不带 inherited 键；空 seed 同样补记——上游 at(-1) 为
        # undefined ≠ end-seed → 追加）。
        # 直接落 _events（构造期未注册 store，不走 on_append，上游该 marker 是
        # constructor 内部 append，store 发布在会话完整接线后才发生）。
        if seed is not None:
            if mode == "snapshot" and self._is_seeded:
                marker_data: dict[str, Any] = {"inherited": True}
            elif not self._events or self._events[-1]["type"] != "session/end-seed":
                marker_data = {}
            else:
                marker_data = None
            if marker_data is not None:
                marker = deep_freeze({"type": "session/end-seed", "seq": self.seq,
                                      "time": now_ms(), "data": marker_data})
                self._events.append(marker)

    @property
    def seq(self) -> int:
        return len(self._events)

    @property
    def events(self) -> tuple[dict[str, Any], ...]:
        """完整事件视图（含继承前缀）。保留向后兼容；新代码用 snapshot_events()。"""
        return tuple(self._events)

    @property
    def is_seeded(self) -> bool:
        """是否为 fork 子会话（对齐上游 SessionHeader.isSeeded）。"""
        return self._is_seeded

    @property
    def inherited_event_count(self) -> int:
        """fork 继承前缀长度（对齐上游 Session.inheritedEventCount）。"""
        return self._inherited_event_count

    @property
    def replace_generation(self) -> int:
        """已提交的 surface 位置替换次数（上游 surface.ts SurfaceManager.replaceGeneration）。

        替换使 surface 上可见 seq 非单调；压缩的 overflow 恢复以此判断
        "surface 是否真的前进过"。
        """
        return self._replace_count

    def snapshot_events(self, from_: int | None = None, to_: int | None = None) -> tuple[dict[str, Any], ...]:
        """半开区间事件快照（对齐上游 Session.snapshotEvents(from?, to?)）。

        from_ 缺省 = 0，to_ 缺省 = self.seq。
        """
        start = from_ if from_ is not None else 0
        end = to_ if to_ is not None else len(self._events)
        return tuple(self._events[start:end])

    def event_at(self, seq: int) -> dict[str, Any]:
        """按 seq 查找单条事件（对齐上游 Session.eventAt(seq)）。"""
        if seq < 0 or seq >= len(self._events):
            raise IndexError(f"event seq {seq} out of range [0, {len(self._events)})")
        return self._events[seq]

    def own_events(self) -> tuple[dict[str, Any], ...]:
        """仅本会话自有事件（跳过继承前缀，对齐上游 Session.ownEvents()）。"""
        return tuple(self._events[self._inherited_event_count:])

    def is_own_seq(self, seq: int) -> bool:
        """判断 seq 是否属于本会话自有范围（对齐上游 Session.isOwnSeq(seq)）。"""
        return self._inherited_event_count <= seq < len(self._events)

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

    def request_context(self) -> dict | None:
        """最近一次 request/context 的路由元数据（上游 session.ts requestContext()）。

        返回 {provider, model, contextWindow?}；尚无 context 事件返回 None。
        """
        for event in reversed(self._events):
            if event["type"] == "request/context":
                return dict(event["data"])
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
            seq = ev.get("seq")
            # rc.1: seq 必须是安全整数且严格等序递增（拒绝 -0.0 之类浮点别名——
            # 对齐上游 walkJsonValue 对 -0 的 Object.is 拒收：-0 与 0 相等但
            # 不能充当无符号日志游标 seq == log.length）。
            if (
                not isinstance(ev, (dict, MappingProxyType))
                or not isinstance(seq, int)
                or isinstance(seq, bool)
                or seq != i
            ):
                raise ValueError(f"seed 事件 seq 必须从 0 连续，第 {i} 条不符")
            etype = ev.get("type")
            ign = ev.get("ignorable")
            if ign is not None and ign is not True:
                raise ValueError(f"ignorable 必须为 true 或缺失，第 {i} 条不符: {ign!r}")
            if etype not in KNOWN_TYPES and ign is not True:
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
            # V2: assistant/message 与 assistant/attempt 内嵌 stream，须在 restore
            # 边界做展开验证（上游 assertCurrentAssistantStream）
            if etype in ("assistant/message", "assistant/attempt"):
                self._assert_current_assistant_stream(ev, i)
            self._events.append(deep_freeze(ev))

    def _assert_current_assistant_stream(self, ev: dict, index: int) -> None:
        """V2 seed 边界验证：内嵌 stream 必须能还原且与 message 冗余字段一致。

        上游 index.ts assertCurrentAssistantStream：expand → 逐 chunk 喂
        BlockAssembler → 对 assistant/message 比对 message.content（interrupted
        时用 interruptedBlocks）/ usage / source.replayState。assistant/attempt
        或空流只做还原验证。
        """
        from ...llm import BlockAssembler, expand_assistant_stream
        data = ev.get("data") or {}
        try:
            timed = expand_assistant_stream(data.get("stream") or [])
            assembler = BlockAssembler()
            for member in timed:
                assembler.push(member.chunk)
        except Exception as e:
            raise ValueError(
                f"seed {ev['type']} at index {index} has an invalid embedded stream: {e}")
        if ev["type"] == "assistant/attempt" or not timed:
            return
        message = data.get("message") or {}
        if data.get("interrupted") is True:
            expected_content = assembler.interrupted_blocks()
        else:
            expected_content = assembler.blocks
        if not _json_deep_equal(message.get("content"), expected_content):
            raise ValueError(
                f"seed assistant/message at index {index} content disagrees with its embedded stream")
        if not _json_deep_equal(data.get("usage"), assembler.usage):
            raise ValueError(
                f"seed assistant/message at index {index} usage disagrees with its embedded stream")
        source = message.get("source") or {}
        if not _json_deep_equal(source.get("replayState"), assembler.replay_state):
            raise ValueError(
                f"seed assistant/message at index {index} replay state disagrees with its embedded stream")