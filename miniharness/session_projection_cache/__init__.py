"""持久化会话投影缓存（SessionProjectionCache，写后节流 + 身份匹配）。

上游：packages/session/session-projection-cache/src/index.ts（471 行）+ spec.ts。

职责：把 `ctx.sessionProjections.checkpoint(session)` 的逐单元状态（{ver, seq,
val}）写后节流落盘到 `session_projcache` domain（per-record 布局，version 7，
backup-and-skip），供会话列表读投影列做**零 I/O** 直读（cachedSnapshot）与
冷读播种（hydratePrepared / coldSnapshot）。

写后节流（write-behind）：
  * session/event：turn/end → 强制落盘；其它事件累计 pending，≥ writeEveryEvents
    强制落盘，否则按 writeIntervalMs 惰性定时。
  * session/created → 强制落盘（捕获 seed 派生 cut，fork 子从未发言也可读标题）。
  * session/disposed → 强制落盘 + 清理 dirty 表。

身份匹配：记录 identity = {formatVersion, createdAt, cwd?, isSeeded,
inheritedEventCount}；当前 lifecycle 完全匹配才 serve（header 变化 → 拒读、
冷读重折）。absent formatVersion 永不匹配（无法证明 fold 语义）；
predecessor 只对 `formatVersion` 更旧的记录开放（跨格式边仅 title 提示）。

载体差异：上游写路径 async（storage-domain）+ 持久化 flush 屏障；mini 经
`run_on_resident` 同步驱动 domain 写（存储层单写链 + 原子发布已是持久边界）；
上游 handle 级逐条读回验证（durability barrier）不承载。
"""
from __future__ import annotations

import threading
import time
from typing import Any

from ..core.scope import Context, Service
from ..core.session import SESSION_FORMAT_VERSION
from ..storage import DomainSpec, define_domain, domain_table
from ..storage.facility import DomainFacility

__all__ = [
    "PROJECTION_CACHE_DOMAIN_VERSION",
    "SessionProjectionCache",
    "install_session_projection_cache",
]

PROJECTION_CACHE_DOMAIN_VERSION = 7

_TRIGGERS = ("turn/end", "count threshold", "interval", "create", "detach")


def _is_safe_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _snapshot_json_value(value: Any) -> Any:
    """无损 JSON 检查 + 深拷贝（对齐 snapshotJsonValue：拒绝函数/symbol/
    bigint/非有限数/循环/undefined 成员/洞/-0）。"""
    seen = set()

    def walk(v: Any, path: str) -> Any:
        if v is None or isinstance(v, bool):
            return v
        if isinstance(v, (int, float)):
            if isinstance(v, float) and (v != v or v in (float("inf"), float("-inf"))):
                raise TypeError(f"non-finite number at {path}")
            return v
        if isinstance(v, str):
            return v
        if isinstance(v, (list, tuple)):
            if id(v) in seen:
                raise TypeError(f"circular reference at {path}")
            seen.add(id(v))
            result = []
            for i, item in enumerate(v):
                if item is None and isinstance(v, list):
                    result.append(None)
                else:
                    result.append(walk(item, f"{path}[{i}]"))
            seen.discard(id(v))
            return result
        if isinstance(v, dict):
            if id(v) in seen:
                raise TypeError(f"circular reference at {path}")
            seen.add(id(v))
            result = {}
            for k, item in v.items():
                result[str(k)] = walk(item, f"{path}.{k}")
            seen.discard(id(v))
            return result
        raise TypeError(f"non-JSON value at {path}: {type(v).__name__}")

    return walk(value, "value")


#: domain spec（spec.ts:101-108）：per-record、version 7、backup-and-skip。
def _projection_cache_domain_spec() -> DomainSpec:
    def _checkpoint_row_schema(value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("checkpoint row must be an object")
        ver = value.get("ver")
        seq = value.get("seq")
        if not _is_safe_int(ver):
            raise ValueError("checkpoint row ver must be a non-negative integer")
        if seq != -1 and not _is_safe_int(seq):
            raise ValueError("checkpoint row seq must be -1 or a non-negative integer")
        _snapshot_json_value(value.get("val"))
        return value

    def _identity_schema(value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("checkpoint identity must be an object")
        for key in ("formatVersion", "createdAt", "inheritedEventCount"):
            if key in value and not _is_safe_int(value[key]):
                raise ValueError(f"checkpoint identity {key} must be a non-negative integer")
        if "cwd" in value and not isinstance(value["cwd"], str):
            raise ValueError("checkpoint identity cwd must be a string")
        if "isSeeded" in value and not isinstance(value["isSeeded"], bool):
            raise ValueError("checkpoint identity isSeeded must be a boolean")
        return value

    def _record_schema(value: Any) -> Any:
        if not isinstance(value, dict):
            raise ValueError("checkpoint record must be an object")
        _identity_schema(value.get("identity"))
        rows = value.get("rows")
        if not isinstance(rows, dict):
            raise ValueError("checkpoint record rows must be an object")
        for row in rows.values():
            _checkpoint_row_schema(row)
        return value

    return define_domain(DomainSpec(
        name="session_projcache",
        version=PROJECTION_CACHE_DOMAIN_VERSION,
        tables={"sessions": domain_table(_record_schema)},
        layout="per-record",
        compatible_versions=(3, 4, 5, 6),
        invalid_records="backup-and-skip",
    ))


def _run_awaitable(awaitable) -> Any:
    """把任意 awaitable（含已调度 Task）提交到常驻循环并阻塞至完成。"""
    import asyncio
    from ..core.agent_loop.resident_loop import run_on_resident

    async def _await_it():
        return await awaitable

    return run_on_resident(_await_it())


class SessionProjectionCache(Service):
    """ctx.sessionProjectionCache：写后节流 + 身份匹配 + 冷读播种。"""

    provide = "sessionProjectionCache"

    def __init__(self, ctx: Context, config: dict | None = None):
        config = dict(config or {})
        write_every = config.get("writeEveryEvents")
        write_interval = config.get("writeIntervalMs")
        if not _is_safe_int(write_every) or write_every < 1:
            raise ValueError(
                "session-projection-cache: writeEveryEvents must be a positive integer")
        if not _is_safe_int(write_interval) or write_interval < 1:
            raise ValueError(
                "session-projection-cache: writeIntervalMs must be a positive integer")
        self.write_every = write_every
        self.write_interval = write_interval
        self._dirty: dict = {}
        self._domain = None
        self._table = None
        super().__init__(ctx, "sessionProjectionCache")
        self._open_domain()
        self._install_write_path()

    # ---------- 装配 ----------

    def _open_domain(self) -> None:
        facility: DomainFacility = self.ctx.get("storage").domain
        spec = _projection_cache_domain_spec()
        domain = _run_awaitable(facility.open(spec))
        self._domain = domain
        self._table = domain.table("sessions")

    def _install_write_path(self) -> None:
        self._disposers = [
            self.ctx.on("session/event", self._on_session_event),
            self.ctx.on("session/created", self._on_session_created),
            self.ctx.on("session/disposed", self._on_session_disposed),
        ]

    # ---------- 写后节流 ----------

    def _on_session_event(self, payload: dict) -> None:
        session = payload.get("session")
        event = payload.get("event")
        if session is None or event is None:
            return
        if event.get("type") == "turn/end":
            self._flush_soft(session, "turn/end")
            return
        dirty = self._dirty.get(id(session))
        if dirty is None:
            dirty = {"pending": 0, "timer": None, "session": session}
            self._dirty[id(session)] = dirty
        dirty["pending"] += 1
        if dirty["pending"] >= self.write_every:
            self._flush_soft(session, "count threshold")
        elif dirty["timer"] is None:
            dirty["timer"] = _arm_timer(
                self.write_interval / 1000, lambda: self._flush_soft(session, "interval"))

    def _on_session_created(self, payload: dict) -> None:
        session = payload.get("session")
        if session is not None:
            self._flush_soft(session, "create")

    def _on_session_disposed(self, payload: dict) -> None:
        session = payload.get("session")
        if session is None:
            return
        self._flush_soft(session, "detach")
        dirty = self._dirty.pop(id(session), None)
        if dirty is not None and dirty["timer"] is not None:
            dirty["timer"].cancel()

    def _flush_soft(self, session, trigger: str) -> None:
        try:
            self.write(session)
        except Exception as error:  # noqa: BLE001 - fail-soft（缓存保持陈旧）
            logger = getattr(self.ctx, "logger", None)
            if logger is not None and hasattr(logger, "warn"):
                logger.warn(f"session projection cache: {trigger} write for "
                            f'"{session.session_id}" failed (cache stays stale): {error}')

    def _mark_clean(self, session) -> None:
        dirty = self._dirty.pop(id(session), None)
        if dirty is not None and dirty["timer"] is not None:
            dirty["timer"].cancel()

    # ---------- 写 ----------

    def write(self, session) -> None:
        """一个强制定点：checkpoint 割面 → 落盘（log 先、cache 后）。"""
        registry = self.ctx.get("sessionProjections")
        if registry is None:
            return
        rows = registry.checkpoint(session)
        self._mark_clean(session)
        identity = self._identity_of(session)
        self._put(session.session_id, identity, rows)

    def _put(self, session_id: str, identity: dict, rows: dict) -> None:
        detached = _snapshot_json_value(rows)
        if detached is None:
            raise TypeError(
                "projection checkpoint is not losslessly JSON-serializable "
                "(a unit state violates the plain-JSON contract)")
        record = {"identity": identity, "rows": detached}
        _run_awaitable(self._table.put(session_id, record))

    # ---------- 身份 ----------

    @staticmethod
    def _lifecycle_identity_of(header: Any) -> dict:
        identity = {
            "formatVersion": SESSION_FORMAT_VERSION,
            "createdAt": header.get("createdAt"),
            "isSeeded": bool(header.get("isSeeded")),
        }
        cwd = header.get("cwd")
        if cwd is not None:
            identity["cwd"] = cwd
        return identity

    def _identity_of(self, session) -> dict:
        # mini Session.meta 不含 createdAt（Session 对象持有）；以会话对象的
        # createdAt 补进身份（上游 SessionHeader.createdAt 即持久头字段）。
        identity = self._lifecycle_identity_of(session.meta)
        if identity["createdAt"] is None:
            identity["createdAt"] = getattr(session, "created_at", None)
        cut = session.inherited_event_count
        if not session.is_seeded and cut != 0:
            raise ValueError(
                "unseeded projection-cache identity inherited event count must be 0")
        identity["inheritedEventCount"] = cut
        return identity

    @staticmethod
    def _lifecycle_identity_matches(stored: dict, expected: dict) -> bool:
        return (stored.get("createdAt") == expected.get("createdAt")
                and stored.get("cwd") == expected.get("cwd")
                and (stored.get("isSeeded") or False) == expected.get("isSeeded"))

    def _current_lifecycle_matches(self, stored: dict, expected: dict) -> bool:
        return (stored.get("formatVersion") == expected.get("formatVersion")
                and self._lifecycle_identity_matches(stored, expected))

    def _predecessor_identity_matches(self, stored: dict, expected: dict) -> bool:
        fv = stored.get("formatVersion")
        return (fv is None or fv < expected.get("formatVersion")) \
            and self._lifecycle_identity_matches(stored, expected)

    def _identity_matches(self, stored: dict, expected: dict) -> bool:
        return self._current_lifecycle_matches(stored, expected) \
            and (stored.get("inheritedEventCount") or 0) == expected.get("inheritedEventCount")

    def _record_for(self, session_id: str, expected: dict) -> dict | None:
        if self._table is None:
            return None
        record = self._table.get(session_id)
        if record is None:
            return None
        identity = record.get("identity") or {}
        if not self._identity_matches(identity, expected):
            return None
        return record

    # ---------- 读面 ----------

    def _view_record(self, record: dict, keys: list | None = None) -> dict | None:
        registry = self.ctx.get("sessionProjections")
        if registry is None:
            return None
        rows = record.get("rows") or {}
        values = registry.view_checkpoint(rows, keys)
        if not values:
            return None
        as_of_seq = None
        for row in rows.values():
            if isinstance(row, dict) and isinstance(row.get("seq"), int):
                seq = row["seq"]
                if as_of_seq is None or seq < as_of_seq:
                    as_of_seq = seq
        return {"asOfSeq": as_of_seq, "values": values}

    def cached_snapshot(self, meta: dict, keys: list | None = None) -> dict | None:
        """零 I/O 列表读：lifecycle 完全匹配才 serve。"""
        expected = self._lifecycle_identity_of(meta)
        record = self._table.get(meta.get("id")) if self._table is not None else None
        if record is None:
            return None
        identity = record.get("identity") or {}
        if not self._current_lifecycle_matches(identity, expected):
            return None
        return self._view_record(record, keys)

    def cached_predecessor_title(self, meta: dict) -> dict | None:
        """跨格式边标题提示：仅对更旧 formatVersion 开放。"""
        expected = self._lifecycle_identity_of(meta)
        record = self._table.get(meta.get("id")) if self._table is not None else None
        if record is None:
            return None
        identity = record.get("identity") or {}
        if not self._predecessor_identity_matches(identity, expected):
            return None
        return self._view_record(record, ["title"])

    def hydrate_prepared(self, session, events: list) -> dict:
        """冷恢复播种：匹配记录 → hydrate；无/坏记录 → 从空重折。"""
        registry = self.ctx.get("sessionProjections")
        if registry is None:
            return {"asOfSeq": -1, "values": {}}
        try:
            record = self._record_for(session.session_id, self._identity_of(session))
        except ValueError:
            record = None
        if record is None:
            return registry.hydrate(session, {}, events, 0)
        try:
            return registry.hydrate(session, record.get("rows") or {}, events, 0)
        except Exception:  # noqa: BLE001 - 坏缓存行不可使合法会话不可读
            return registry.hydrate(session, {}, events, 0)

    def cold_snapshot(self, meta: dict, inherited_event_count: int,
                      events: list) -> dict:
        """冷读：restore 播种 + 写回（fail-soft）。"""
        registry = self.ctx.get("sessionProjections")
        if registry is None:
            return {"asOfSeq": -1, "values": {}}
        expected = self._lifecycle_identity_of(meta)
        if not meta.get("isSeeded") and inherited_event_count != 0:
            raise ValueError(
                "unseeded projection-cache identity inherited event count must be 0")
        expected["inheritedEventCount"] = inherited_event_count
        record = self._table.get(meta.get("id")) if self._table is not None else None
        rows = {}
        if record is not None:
            identity = record.get("identity") or {}
            if self._identity_matches(identity, expected):
                rows = record.get("rows") or {}
        restored = registry.restore(rows, events, 0, meta, inherited_event_count)
        try:
            self._put(meta.get("id"), expected, restored["checkpoint"])
        except Exception as error:  # noqa: BLE001 - fail-soft（缓存保持陈旧）
            logger = getattr(self.ctx, "logger", None)
            if logger is not None and hasattr(logger, "warn"):
                logger.warn(f"session projection cache: cold-read write-back for "
                            f'"{meta.get("id")}" failed (cache stays stale): {error}')
        return restored["snapshot"]

    # ---------- 生命周期 ----------

    def dispose(self) -> None:
        for fn in reversed(self._disposers):
            fn()
        self._disposers.clear()
        for dirty in list(self._dirty.values()):
            if dirty["timer"] is not None:
                dirty["timer"].cancel()
        self._dirty.clear()


def _arm_timer(seconds: float, callback) -> Any:
    timer = threading.Timer(seconds, callback)
    timer.daemon = True
    timer.start()
    return timer


def install_session_projection_cache(ctx: Context, config: dict | None = None) \
        -> SessionProjectionCache:
    """装配 ctx.sessionProjectionCache（幂等，重复装返回既有实例）。

    要求 ctx.storage（install_storage）与 ctx.sessionProjections 在场。
    """
    existing = ctx.get("sessionProjectionCache")
    if existing is not None:
        return existing
    if ctx.get("storage") is None:
        raise RuntimeError(
            "session-projection-cache: the storage service is required (install_storage)")
    return SessionProjectionCache(ctx, config)