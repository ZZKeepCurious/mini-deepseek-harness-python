"""会话管理服务：ctx.sessions（对齐 packages/core/session/src/index.ts 的 SessionStore）。

契约面（与上游逐条一致）：
  * ctx.sessions.create / prepare / enter / announce / flush / get / list / fork
  * 生命周期事件（ctx 事件，非会话日志事件，不进 KNOWN_TYPES）：
      session/created  发布公告，同步 throw 即回滚（附带配对的 disposal）
      session/disposed 离店公告（含发布回滚，从不给未开始公告的条目）
      session/event    append 后的 fire-and-forget 追加流（监听器失败 log+contain）
      session/flush    并行持久化检查点，返回是否至少一个监听器参与
  * prepare 校验 id/元数据（cwd 绝对路径、origin 恒 'subagent'、createdAt 非负整数、
    字符串与整数字段类型）——坏 meta 进不了店，等价上游 validateSessionHeader
  * fork 五种错误码：SESSION_NOT_FOUND / SESSION_NOT_LIVE / SESSION_ALREADY_EXISTS /
    INVALID_BOUNDARY / OPEN_TURN；boundary 缺省 = 源最后事件 seq（空会话 → 空 seed）

mini 简化（有意保留，须在文档标注）：
  * 无 Cordis fiber/effect：create = prepare + enter + announce 的顺序事务，announce
    抛错时手动调 enter 返回的 detach 回滚（上游 effect 自动 yield 回滚；语义等价）
  * 无 typert lookup 注册（mini 无 typert）
  * flush 为同步并行近似（上游 Promise.allSettled 后抛第一个失败；mini 首个异常直接上抛）
  * session/event payload 为 {"session","event"} 单对象（上游 (session, event) 双参）；
    created / disposed / flush payload 为 {"session": s}

scope 路由（2026-08-18 地基①，对齐上游 dsh-scope）：
  * enter 记录 owner scope（owner_ctx = enter 时的调用上下文；缺省 store ctx）
  * session/created|disposed|event|flush 在 owner scope 上派发：owner scope 自身 +
    祖先链监听器收到（含 root/全局）；兄弟/后代作用域不收到。等价上游
    `scopeTarget(session, scopeOf(ctx))` 的"祖先接收、旁支隔离"。
"""
from __future__ import annotations

import os
from typing import Any

from .session.session import Session
from .scope import Context

__all__ = [
    "SESSION_NOT_FOUND",
    "SESSION_NOT_LIVE",
    "SESSION_ALREADY_EXISTS",
    "INVALID_BOUNDARY",
    "OPEN_TURN",
    "SessionForkError",
    "SessionStore",
    "install_sessions",
]

SESSION_NOT_FOUND = "SESSION_NOT_FOUND"
SESSION_NOT_LIVE = "SESSION_NOT_LIVE"
SESSION_ALREADY_EXISTS = "SESSION_ALREADY_EXISTS"
INVALID_BOUNDARY = "INVALID_BOUNDARY"
OPEN_TURN = "OPEN_TURN"


class SessionForkError(Exception):
    """fork 拒绝（上游 SessionForkError）：携带 code，调用方据此区分拒因。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


class SessionStore:
    """内存会话仓库（ctx.sessions）：create/prepare/enter/announce 生命周期 + fork。

    每一条目保存会话本体 + 发布状态（announced/announcing/appending/detachRequested）。
    enter 注入 append 发布钩子（session/event）；detach 移除钩子并退店；已公告的条目
    detach 时补发 session/disposed。
    """

    def __init__(self, ctx: Context):
        self.ctx = ctx
        self._store: dict[str, dict] = {}
        self._counter = 0

    # ---------- 生命周期 ----------

    def create(self, id: str | None = None, options: dict | None = None,
               owner_ctx: "Context | None" = None) -> Session:
        """构造 + 进店 + 公告一条龙（上游 create：prepare 后 effect 包 enter+announce）。

        options：{seed?, meta?}。meta 是持久化头部元数据（cwd/parentSession/
        seedLength/origin/delegationDepth/agentPreset/createdAt），校验后传入
        Session.meta。id 缺省按 session-<n> 顺延去重。
        owner_ctx：会话的 owner scope（对齐上游 enter 处 scopeOf(ctx)）；缺省 store ctx。
        """
        session = self.prepare(id, options)
        detach = self.enter(session, owner_ctx=owner_ctx)
        try:
            self.announce(session)
        except Exception:
            # 发布公告失败即回滚：detach 附带配对的 session/disposed
            detach()
            raise
        return session

    def prepare(self, id: str | None = None, options: dict | None = None) -> Session:
        """构造会话但不进店：校验 id/cwd 与元数据，构造 Session（不发布任何公告）。

        与 enter + announce 配对（复合创建方把三者折进一个事务）；只调 prepare 的
        调用方拿到的会话不在店内，append 不触发 session/event。
        """
        options = options or {}
        meta = dict(options.get("meta") or {})
        self._validate_meta(meta)
        if id is None:
            while True:
                self._counter += 1
                candidate = f"session-{self._counter}"
                if candidate not in self._store:
                    id = candidate
                    break
        if id in self._store:
            raise RuntimeError(f'session "{id}" already exists')
        created_at = meta.pop("createdAt", None)
        return Session(id, seed=options.get("seed"), created_at=created_at, meta=meta)

    def enter(self, session: Session, owner_ctx: "Context | None" = None) -> Any:
        """把 prepare 好的会话装进店内：安装 append 发布钩子 + 登记条目。返回 detach 幂等 disposer。

        owner_ctx：会话的 owner scope（对齐上游 scopeTarget(session, scopeOf(ctx))）。
        记录在条目上，session/created|disposed|event|flush 都在该上下文上派发——
        监听器只收到所属作用域（及祖先）的事件。缺省 store 自身上下文。

        不触发 session/created（由 announce 负责，便于把 detach 先 yield 再公告）。
        重复 id 或已 attach 的会话一律拒绝（fail loud）。
        """
        id = session.session_id
        if id in self._store:
            raise RuntimeError(f'session "{id}" already exists')
        entry = {
            "id": id,
            "session": session,
            "owner_ctx": owner_ctx or self.ctx,
            "announced": False,
            "announcing": False,
            "appending": False,
            "detachRequested": False,
        }
        self._store[id] = entry
        session._on_append = lambda event: self._publish_event(session, event)
        entered = [True]

        def detach() -> None:
            if not entered[0]:
                return
            entered[0] = False
            # 公告/append 派发中的 detach 延迟到派发结束后执行（上游同款守卫）
            if entry["announcing"] or entry["appending"]:
                entry["detachRequested"] = True
                return
            self._detach_entered(entry)

        return detach

    def announce(self, session: Session) -> None:
        """发布 session/created 公告（恰一次）：监听器同步 throw 传播并 veto 发布。

        公告在 emit 前标记（防监听器递归再建生命周期边）；throw 后由 create 的
        detach 回滚，回滚时附带配对 disposal。
        """
        entry = self._live_entry_for(session)
        if entry["announced"] or entry["announcing"]:
            raise RuntimeError(f'session "{session.session_id}" was already announced')
        entry["announced"] = True
        entry["announcing"] = True
        try:
            entry["owner_ctx"].emit("session/created", {"session": session})
        finally:
            entry["announcing"] = False
            if entry["detachRequested"] and not entry["appending"]:
                self._detach_entered(entry)

    def flush(self, session: Session) -> bool:
        """并行派发 session/flush 持久化检查点：监听器全部跑完后返回是否有人参与。

        同步近似：第一个抛出的监听器异常直接上抛（上游 allSettled 后抛第一个）。
        """
        entry = self._live_entry_for(session)
        return len(entry["owner_ctx"].parallel("session/flush", {"session": session})) > 0

    # ---------- 查询 ----------

    def get(self, id: str) -> Session | None:
        entry = self._store.get(id)
        return entry["session"] if entry else None

    def list(self) -> list[Session]:
        return [e["session"] for e in self._store.values()]

    # ---------- fork ----------

    def fork(self, source: Session | str, boundary: int | None = None,
             child_session_id: str | None = None) -> Session:
        """从 live 源会话的稳定前缀创建 live 子会话。

        boundary 是包含性的源事件 seq；缺省取源当前最后一条。所选切片可以停在
        回合间事件，但不能停在一个打开的回合内。子会话 meta 继承源 cwd、
        parentSession=源 id、seedLength=seed 长度（上游 fork 同款）。
        """
        if child_session_id is not None and self.get(child_session_id) is not None:
            raise SessionForkError(
                SESSION_ALREADY_EXISTS,
                f'session "{child_session_id}" already exists',
            )
        live_source = self._resolve_fork_source(source)
        seed = self._fork_seed(live_source, boundary)
        meta: dict[str, Any] = {}
        if live_source.meta.get("cwd") is not None:
            meta["cwd"] = live_source.meta["cwd"]
        meta["parentSession"] = live_source.session_id
        meta["seedLength"] = len(seed)
        return self.create(child_session_id, {"seed": seed, "meta": meta})

    def _resolve_fork_source(self, source: Session | str) -> Session:
        if isinstance(source, str):
            session = self.get(source)
            if session is None:
                raise SessionForkError(SESSION_NOT_FOUND, f'session "{source}" not found')
            return session
        live = self.get(source.session_id)
        if live is None:
            raise SessionForkError(SESSION_NOT_FOUND, f'session "{source.session_id}" not found')
        if live is not source:
            raise SessionForkError(
                SESSION_NOT_LIVE,
                f'session "{source.session_id}" is not the live store instance',
            )
        return source

    def _fork_seed(self, session: Session, requested_boundary: int | None) -> list:
        events = list(session.events)
        if requested_boundary is None:
            if not events:
                return []
            boundary = events[-1]["seq"]
        else:
            boundary = requested_boundary
        if not isinstance(boundary, int) or isinstance(boundary, bool) or boundary < 0:
            raise SessionForkError(
                INVALID_BOUNDARY,
                f'fork boundary for session "{session.session_id}" must be a non-negative integer, '
                f"got {boundary!r}",
            )
        if boundary >= len(events):
            last_seq = events[-1]["seq"] if events else None
            raise SessionForkError(
                INVALID_BOUNDARY,
                f'fork boundary {boundary} does not exist in session "{session.session_id}" '
                f"(last seq: {last_seq})",
            )
        boundary_event = events[boundary]
        if boundary_event.get("seq") != boundary:
            raise SessionForkError(
                INVALID_BOUNDARY,
                f"fork boundary {boundary} does not match a contiguous event seq in "
                f'session "{session.session_id}"',
            )
        last_turn = None
        for event in events[: boundary + 1]:
            if event["type"] in ("turn/start", "turn/end"):
                last_turn = event
        if last_turn is not None and last_turn["type"] == "turn/start":
            raise SessionForkError(
                OPEN_TURN,
                f'fork boundary {boundary} in session "{session.session_id}" ends inside '
                f'open turn {last_turn["data"]["turn"]}',
            )
        return events[: boundary + 1]

    # ---------- 内部 ----------

    def _validate_meta(self, meta: dict) -> None:
        if "cwd" in meta and not os.path.isabs(meta["cwd"]):
            raise RuntimeError(f'session meta cwd must be an absolute path, got "{meta["cwd"]}"')
        if "origin" in meta and meta["origin"] != "subagent":
            raise RuntimeError('session meta origin must be "subagent"')
        for key in ("parentSession", "agentPreset"):
            if key in meta and not isinstance(meta[key], str):
                raise RuntimeError(f"session meta {key} must be a string")
        for key in ("seedLength", "delegationDepth", "createdAt"):
            if key in meta and (not isinstance(meta[key], int) or isinstance(meta[key], bool)
                                or meta[key] < 0):
                raise RuntimeError(f"session meta {key} must be a non-negative integer")

    def _live_entry_for(self, session: Session) -> dict:
        entry = self._store.get(session.session_id)
        if entry is None or entry["session"] is not session:
            raise RuntimeError(f'session "{session.session_id}" is not live in this store')
        return entry

    def _detach_entered(self, entry: dict) -> None:
        entry["detachRequested"] = False
        if self._store.get(entry["id"]) is not entry:
            return
        self._store.pop(entry["id"], None)
        entry["session"]._on_append = None
        if entry["announced"]:
            self._emit_disposed(entry)

    def _publish_event(self, session: Session, event: dict) -> None:
        entry = self._store.get(session.session_id)
        if entry is None or entry["session"] is not session:
            return  # 派发中的 detach 已把条目摘走，就地短路
        entry["appending"] = True
        try:
            entry["owner_ctx"].emit("session/event", {"session": session, "event": event})
        except Exception as error:
            logger = getattr(self.ctx, "logger", None)
            if logger is not None and hasattr(logger, "warn"):
                logger.warn(f'session "{session.session_id}": session/event listener threw: {error}')
        finally:
            entry["appending"] = False
            if entry["detachRequested"]:
                self._detach_entered(entry)

    def _emit_disposed(self, entry: dict) -> None:
        try:
            entry["owner_ctx"].emit("session/disposed", {"session": entry["session"]})
        except Exception as error:
            logger = getattr(self.ctx, "logger", None)
            if logger is not None and hasattr(logger, "warn"):
                logger.warn(f'session "{entry["id"]}": session/disposed dispatch threw: {error}')


def install_sessions(ctx: Context) -> SessionStore:
    """幂等装配：创建 ctx.sessions 服务。首个调用生效；已存在时收养并直接返回。"""
    if getattr(ctx, "_miniharness_sessions_installed", False):
        return ctx.inject("sessions")
    try:
        store = ctx.inject("sessions")
    except KeyError:
        store = SessionStore(ctx)
        ctx.provide("sessions", store)
    ctx._miniharness_sessions_installed = True
    return store