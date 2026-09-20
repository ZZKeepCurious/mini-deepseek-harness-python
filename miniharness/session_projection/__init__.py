"""会话投影注册表（session-projection 注册 API v2）。

对齐上游 `packages/session/session-projection/src/{types,index}.ts`：

- `ProjectionDefinition`：一个域的状态驱动计算单元——纯同步 fold + 声明
  （`key` / `init(header, inherited_event_count)` / `apply(state, event)` /
  可选 `view`（wire 视图）+ `state_version`）；事件必须携带改变后的完整状态
  （whole-value 规则），未命中的事件必须返回同一状态引用（`is` 相等）。
- `SessionProjectionRegistry`（`ctx.sessionProjections`）：注册表 + 驱动。
  订阅 `session/created`（为新会话惰性/即刻初始化各单元 cell）与 `session/event`
  （eager drive：每个已提交事件过每个单元的 `apply`）；改变的状态引用计算下一
  客户端视图，仅当视图按 `is` 变化时通知变更订阅者。cell 惰性构建（迟注册的
  单元或早于注册表的会话在首次触碰时把 `init` 折过内存日志）。
- 读面：`stateOf`（host-only 原始状态）、`snapshot`/`cachedSnapshot`（客户端一致
  割面）、`checkpoint`/`restoreFloor`/`viewCheckpoint`/`restore`/`hydrate`
  （持久投影缓存的读写梯）。

载体差异（登记 §3.28）：
- 上游用 merge-extensible 类型表（`SessionProjectionMap` / `SessionProjectionStateMap`）
  + zod schema；mini 以运行时 `state_schema`/`view_schema` 可选可调用对象承载
  （默认恒等），Python 无声明合并。
- 上游 `session.header` 是 SessionHeader；mini 以 `session.meta` 作为 init 的
  header 参数（mini 的不可变会话元数据载体）。
- 上游用 WeakMap 持有 cell；mini 用 `WeakKeyDictionary`（同义）。
"""
from __future__ import annotations

from typing import Any, Callable
from weakref import WeakKeyDictionary

from ..core.scope import Context, Service

__all__ = [
    "ProjectionDefinition",
    "ProjectionSnapshot",
    "SessionProjectionRegistry",
    "install_session_projections",
]


class ProjectionDefinition:
    """一个域的状态驱动计算单元（对齐上游 ProjectionDefinition）。

    @param key - 本单元拥有的投影键。
    @param init - `(header, inherited_event_count) -> state`：空日志的初始状态。
    @param apply - `(state, event) -> state`：纯转换；不感兴趣的事件返回同一引用。
    @param state_version - 持久缓存失效版本（非负整数）。
    @param view - 可选客户端视图 `state -> wire value`；省略即 host-only 单元。
    @param state_schema - 可选持久状态校验 `value -> value`（对齐上游 stateSchema）。
    @param view_schema - 可选 wire 载荷校验 `value -> value`（对齐上游 viewSchema）。
    """

    def __init__(self, key: str, *, init: Callable, apply: Callable,
                 state_version: int = 0,
                 view: Callable | None = None,
                 state_schema: Callable | None = None,
                 view_schema: Callable | None = None):
        if not isinstance(key, str) or key == "":
            raise ValueError("projection key must be a non-empty string")
        if not (isinstance(state_version, int) and not isinstance(state_version, bool)
                and state_version >= 0):
            raise ValueError(
                f"session projection {key!r} stateVersion must be a non-negative integer, "
                f"got {state_version!r}")
        self.key = key
        self.init = init
        self.apply = apply
        self.state_version = state_version
        self.view = view
        self.state_schema = state_schema
        self.view_schema = view_schema


class ProjectionSnapshot(dict):
    """一个会话在全部已注册客户端可见单元上的一致读割。

    `{"asOfSeq": <int>, "values": {key: view}}`；`asOfSeq` 是每个值都反映的
    最后事件 seq（空日志为 -1）。
    """


def _cursor_before(offset: int) -> int:
    return offset - 1 if offset > 0 else -1


class _Cell:
    __slots__ = ("state", "observed_seq", "views")

    def __init__(self, state: Any, observed_seq: int):
        self.state = state
        self.observed_seq = observed_seq
        self.views: list = [None, None]


class SessionProjectionRegistry(Service):
    """`ctx.sessionProjections`：投影单元表与其驱动。"""

    provide = "sessionProjections"

    def __init__(self, ctx: Context):
        self._registrations: dict = {}
        self._listeners: list = []
        self._disposers: list = []
        super().__init__(ctx, "sessionProjections")
        self._disposers.append(ctx.on("session/created", self._on_session_created))
        self._disposers.append(ctx.on("session/event", self._on_session_event))

    def dispose(self) -> None:
        for disposer in self._disposers:
            disposer()
        self._disposers.clear()
        self._registrations.clear()
        self._listeners.clear()

    # ---------- 事件订阅 ----------

    def _on_session_created(self, payload: dict) -> None:
        session = payload.get("session")
        if session is None or session.seq != 0:
            return
        for registration in self._registrations.values():
            cells = registration["cells"]
            if session in cells:
                continue
            definition = registration["def"]
            cells[session] = _Cell(
                definition.init(session.meta, session.inherited_event_count), -1)

    def _on_session_event(self, payload: dict) -> None:
        session = payload.get("session")
        event = payload.get("event")
        if session is None or event is None:
            return
        self._drive(session, event)

    # ---------- 注册 / 观察 ----------

    def register(self, definition: ProjectionDefinition) -> Callable[[], None]:
        """注册一个域单元；返回注销 disposer（effect 挂在调用 fiber 上）。"""
        key = definition.key

        def setup():
            existing = self._registrations.get(key)
            if existing is None:
                self._registrations[key] = {
                    "def": definition, "cells": WeakKeyDictionary(), "refs": 1}
            else:
                if existing["def"].state_version != definition.state_version:
                    raise ValueError(
                        f"session projection key {key!r} is already registered at "
                        f"stateVersion {existing['def'].state_version}; refusing to share "
                        f"it with stateVersion {definition.state_version}")
                existing["refs"] += 1

            def dispose() -> None:
                live = self._registrations.get(key)
                if live is None:
                    return
                live["refs"] -= 1
                if live["refs"] == 0:
                    del self._registrations[key]
            return dispose

        return self.ctx.effect(setup, "sessionProjections.register()")

    def on_changed(self, listener: Callable) -> Callable[[], None]:
        """订阅变更 feed；返回注销 disposer。"""
        def setup():
            self._listeners.append(listener)

            def dispose() -> None:
                try:
                    self._listeners.remove(listener)
                except ValueError:
                    pass
            return dispose

        return self.ctx.effect(setup, "sessionProjections.onChanged()")

    # ---------- 读面 ----------

    def state_of(self, session, key: str) -> Any:
        """一个单元的当前 host 状态（未注册返回 None）。"""
        registration = self._registrations.get(key)
        if registration is None:
            return None
        self._materialize_cells(session)
        return self._cell_for(registration, session).state

    def snapshot(self, session, keys: list | None = None) -> ProjectionSnapshot:
        """全部已注册客户端可见单元的一致割面。"""
        values: dict = {}
        selected = None if keys is None else set(keys)
        self._materialize_cells(session)
        for registration in self._registrations.values():
            definition = registration["def"]
            if definition.view is None:
                continue
            if selected is not None and definition.key not in selected:
                continue
            values[definition.key] = self._view_cell(registration, self._cell_for(registration, session))
        return ProjectionSnapshot(asOfSeq=_cursor_before(session.seq), values=values)

    def cached_snapshot(self, session, keys: list | None = None) -> ProjectionSnapshot | None:
        """只读已物化 cell（不折历史）；无可见 cell 时返回 None。"""
        values: dict = {}
        as_of_seq = None
        selected = None if keys is None else set(keys)
        for registration in self._registrations.values():
            definition = registration["def"]
            if definition.view is None:
                continue
            if selected is not None and definition.key not in selected:
                continue
            cell = registration["cells"].get(session)
            if cell is None:
                continue
            values[definition.key] = self._view_cell(registration, cell)
            if as_of_seq is None or cell.observed_seq < as_of_seq:
                as_of_seq = cell.observed_seq
        if as_of_seq is None:
            return None
        return ProjectionSnapshot(asOfSeq=as_of_seq, values=values)

    def checkpoint(self, session) -> dict:
        """每个已注册单元的状态级检查点（`{key: {ver, seq, val}}`）。"""
        rows: dict = {}
        for registration in self._registrations.values():
            definition = registration["def"]
            cell = self._cell_for(registration, session)
            rows[definition.key] = {
                "ver": definition.state_version,
                "seq": cell.observed_seq,
                "val": _clone(cell.state),
            }
        return rows

    def restore_floor(self, checkpoint: dict) -> int | None:
        """restore 尾部读的起点（最低可用 watermark 之下一格）。"""
        floor = None
        for registration in self._registrations.values():
            definition = registration["def"]
            row = checkpoint.get(definition.key)
            need = (max(row["seq"] + 1, 0)
                    if isinstance(row, dict) and row.get("ver") == definition.state_version
                    else 0)
            floor = need if floor is None else min(floor, need)
        return None if floor is None else max(floor - 1, 0)

    def view_checkpoint(self, checkpoint: dict, keys: list | None = None) -> dict:
        """零 I/O 读：对每个可见单元，ver 匹配的行直接出 wire 视图。"""
        values: dict = {}
        selected = None if keys is None else set(keys)
        for registration in self._registrations.values():
            definition = registration["def"]
            if definition.view is None:
                continue
            if selected is not None and definition.key not in selected:
                continue
            row = checkpoint.get(definition.key)
            if not isinstance(row, dict) or row.get("ver") != definition.state_version:
                continue
            try:
                state = definition.state_schema(row["val"]) if definition.state_schema else row["val"]
            except Exception:
                continue
            values[definition.key] = self._validate_view(definition, definition.view(state))
        return values

    def restore(self, checkpoint: dict, events: list, base_seq: int, header: Any,
                inherited_event_count: int) -> dict:
        """冷读：对存储日志尾部按 checkpoint 播种后折每个单元。"""
        end_seq = events[-1]["seq"] if events else _cursor_before(base_seq)
        before_base = _cursor_before(base_seq)
        values: dict = {}
        refreshed: dict = {}
        for registration in self._registrations.values():
            definition = registration["def"]
            row = checkpoint.get(definition.key)
            usable = (isinstance(row, dict)
                      and row.get("ver") == definition.state_version
                      and before_base <= row.get("seq", -2) <= end_seq)
            if not usable and base_seq > 0:
                raise ValueError(
                    f"session projection {definition.key!r} cannot restore from seq {base_seq}: "
                    "its checkpoint row is missing, version-mismatched, or beyond the supplied "
                    "log end; re-read from seq 0")
            state = (definition.state_schema(row["val"]) if usable and definition.state_schema
                     else (row["val"] if usable else definition.init(header, inherited_event_count)))
            start = (row["seq"] if usable else before_base) - base_seq + 1
            for index in range(start, len(events)):
                event = events[index]
                expected = base_seq + index
                if event is None or event["seq"] != expected:
                    raise ValueError(
                        f"session projection {definition.key!r} cannot restore across "
                        f"missing seq {expected}")
                state = definition.apply(state, event)
            if definition.view is not None:
                values[definition.key] = self._validate_view(definition, definition.view(state))
            refreshed[definition.key] = {
                "ver": definition.state_version, "seq": end_seq, "val": state}
        return {"snapshot": ProjectionSnapshot(asOfSeq=end_seq, values=values),
                "checkpoint": refreshed}

    def hydrate(self, session, checkpoint: dict, events: list, base_seq: int) -> ProjectionSnapshot:
        """恢复精确割面并把状态装到已准备好的 Session 上。"""
        end_seq = events[-1]["seq"] if events else _cursor_before(base_seq)
        complete = all(
            registration["cells"].get(session) is not None
            and registration["cells"][session].observed_seq == end_seq
            for registration in self._registrations.values())
        if complete:
            values: dict = {}
            for registration in self._registrations.values():
                definition = registration["def"]
                if definition.view is None:
                    continue
                values[definition.key] = self._view_cell(
                    registration, registration["cells"][session])
            return ProjectionSnapshot(asOfSeq=end_seq, values=values)
        restored = self.restore(checkpoint, events, base_seq, session.meta,
                                session.inherited_event_count)
        for registration in self._registrations.values():
            row = restored["checkpoint"].get(registration["def"].key)
            if row is None:
                continue
            cell = registration["cells"].get(session)
            if cell is not None and cell.observed_seq > row["seq"]:
                continue
            registration["cells"][session] = _Cell(row["val"], row["seq"])
        return restored["snapshot"]

    # ---------- 驱动内部 ----------

    def _materialize_cells(self, session) -> None:
        for registration in self._registrations.values():
            self._cell_for(registration, session)

    def _build_cell(self, definition: ProjectionDefinition, session, events) -> _Cell:
        state = definition.init(session.meta, session.inherited_event_count)
        for event in events:
            state = definition.apply(state, event)
        observed = events[-1]["seq"] if events else -1
        return _Cell(state, observed)

    def _cell_for(self, registration: dict, session) -> _Cell:
        cells = registration["cells"]
        cell = cells.get(session)
        if cell is None:
            cell = self._build_cell(registration["def"], session, list(session.snapshot_events()))
            cells[session] = cell
        else:
            self._advance_cell(registration["def"], cell, session, _cursor_before(session.seq))
        return cell

    def _advance_cell(self, definition: ProjectionDefinition, cell: _Cell, session,
                      through_seq: int) -> None:
        if cell.observed_seq >= through_seq:
            return
        for seq in range(cell.observed_seq + 1, through_seq + 1):
            event = session.event_at(seq)
            if event is None or event["seq"] != seq:
                raise ValueError(
                    f"session projection {definition.key!r} cannot advance across "
                    f"missing seq {seq}")
            next_state = definition.apply(cell.state, event)
            if next_state is not cell.state:
                cell.views[0] = cell.views[1]
                cell.views[1] = None
            cell.state = next_state
            cell.observed_seq = seq

    def _drive(self, session, event) -> None:
        for registration in self._registrations.values():
            definition = registration["def"]
            cells = registration["cells"]
            cell = cells.get(session)
            if cell is not None and cell.observed_seq >= event["seq"]:
                continue
            if cell is None:
                cell = self._build_cell(definition, session,
                                        list(session.snapshot_events(0, event["seq"])))
                cells[session] = cell
            else:
                self._advance_cell(definition, cell, session, event["seq"] - 1)
            previous = cell.state
            next_state = definition.apply(previous, event)
            changed = next_state is not previous
            cell.state = next_state
            cell.observed_seq = event["seq"]
            if changed and definition.view is not None:
                views = cell.views
                views[0] = views[1]
                if self._listeners:
                    views[1] = definition.view(next_state)
                    if views[0] is not views[1]:
                        value = self._validate_view(definition, views[1])
                        for listener in list(self._listeners):
                            listener(session, definition.key, value, event["seq"])
                else:
                    views[1] = None

    def _view_cell(self, registration: dict, cell: _Cell) -> Any:
        definition = registration["def"]
        if definition.view is None:
            raise ValueError(f"session projection {definition.key!r} has no wire view")
        return self._validate_view(definition, definition.view(cell.state))

    @staticmethod
    def _validate_view(definition: ProjectionDefinition, value: Any) -> Any:
        return definition.view_schema(value) if definition.view_schema else value


def _clone(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _clone(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clone(v) for v in value]
    return value


def install_session_projections(ctx: Context) -> SessionProjectionRegistry:
    """装配 `ctx.sessionProjections`（可重复装，返回既有实例）。"""
    existing = ctx.get("sessionProjections")
    if existing is not None:
        return existing
    return SessionProjectionRegistry(ctx)
