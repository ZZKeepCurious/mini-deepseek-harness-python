"""owner 作用域持久 PTY 注册表（对齐 upstream terminal/src/index.ts TerminalSessionService）。

backend 持有终端机制，本服务持有 id、发布、授权与等待清理：
  * register_backend / list_backends —— backend 注册表（空类型拒、DUPLICATE_BACKEND）
  * spawn —— 事务式发布：pre-reserve name + reserve spawn → backend.spawn → 就绪
    → publish；失败回滚 close 未发布会话；disposing/owner 非 live 拒
  * start_send / read / signal / kill / list —— exact-owner 围栏（expect_owned）+
    SEND_ACTIVE 独占 + 幂等 kill + 出版序快照
  * 生命周期：ctx.effect 登记服务级 teardown（dispose_all）与 owner 级清理
    （dispose_owned），pending spawn 中止 + 会话现结全清
  * 错误码闭集 8 种（types.TERMINAL_ERROR_CODES）；id mint `pty-N`

同步载体差异（已登记 design-terminal-domain.md / verified-diffs）：
  * 上游 async spawn/kill/close + AbortSignal → 本实现同步调用链 +
    types.Cancellation；pending/reservation 窗口在单线程同步 spawn 中无跨调度帧
    （结构保留，P2 接真 PTY 线程后供并发语义复用）。
  * 服务内 register_backend 返回幂等 detach（同 ctx.effect 卸载时幂等重跑）。
"""

from __future__ import annotations

from ..core.scope import Context, Service
from .types import (
    TerminalBackend,
    TerminalBackendCleanupError,
    Cancellation,
    TerminalError,
    any_cancellation,
)

__all__ = ["TerminalSessionService", "install_terminals"]


class TerminalSessionService(Service):
    """进程内可替换 PTY backend 注册表 + exact-Agent 会话。"""

    provide = "terminals"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "terminals")
        self._backends: dict[str, TerminalBackend] = {}
        #: sessionId -> 会话记录 {id, owner, name, type, session, active, closing}
        self._sessions: dict = {}
        #: owner -> 保留中的名字集合
        self._reserved_names: dict = {}
        #: owner -> {controller, cleanup_failure, settled} 的 pending spawn 集合
        self._pending_spawns: dict = {}
        #: owner -> owner 作用域 detach（随 owner.ctx 拆解执行 cleanup）
        self._owner_cleanups: dict = {}
        self._disposed_owners: set = set()
        self._next_id = 0
        self._disposing = False
        ctx.effect(lambda: lambda: self._dispose_all(), "pty teardown")

    # ---------- backend 注册表 ----------

    def register_backend(self, backend: TerminalBackend):
        """注册一个 backend 类型；返回幂等 detach（对齐 index.ts:125-137）。

        空 type 拒；重复 type → DUPLICATE_BACKEND。注册经 ctx.effect 挂到服务
        作用域，teardown 时自动卸载；手动调用返回的 detach 幂等删除。
        """
        if len(backend.type) == 0:
            raise RuntimeError("pty backend type must be non-empty")
        if backend.type in self._backends:
            raise TerminalError(
                f'a PTY backend named "{backend.type}" is already registered', "DUPLICATE_BACKEND")

        def detach() -> None:
            if self._backends.get(backend.type) is backend:
                self._backends.pop(backend.type, None)

        def setup() -> None:
            self._backends[backend.type] = backend
            return detach

        self.ctx.effect(setup, "pty.registerBackend()")
        return detach

    def list_backends(self) -> list:
        return list(self._backends.keys())

    # ---------- spawn / 发布 ----------

    def spawn(self, owner, request: dict, signal: Cancellation | None = None) -> dict:
        """创建并发布一个 owner 作用域会话；返回发布快照 {sessionId, type, status, motd?...}。

        事务式：名字与 spawn 双保留 → backend.spawn → 就绪校验（caller 中止 /
        disposing / owner live）→ 发布。任一步失败回滚（未发布会话 close），
        中止原因按 upstream 逐帧语义重抛。
        """
        self._assert_active()
        _throw_if_aborted(signal)
        self._ensure_owner_cleanup(owner)
        backend = self._backends.get(request["type"])
        if backend is None:
            raise TerminalError(f'no PTY backend registered for "{request["type"]}"', "NO_BACKEND")
        name = request.get("name")
        if name is not None and len(name) == 0:
            raise RuntimeError("PTY session name must be non-empty")
        release_name = self._reserve_name(owner, name)
        spawn_reservation = self._reserve_spawn(owner)
        self._next_id += 1
        session_id = f"pty-{self._next_id}"
        session = None
        cleanup_failure = None
        try:
            spec = {"sessionId": session_id, "owner": owner, "type": request["type"]}
            if name is not None:
                spec["name"] = name
            if request.get("cwd") is not None:
                spec["cwd"] = request["cwd"]
            spec["signal"] = any_cancellation([signal, spawn_reservation["signal"]])
            session = backend.spawn(spec)
            _throw_if_aborted(signal)
            if self._disposing:
                raise TerminalError("PTY service is disposing", "SERVICE_DISPOSING")
            if not self._is_live_owner(owner):
                raise TerminalError("PTY owner is no longer live", "OWNER_NOT_LIVE")
            record = {
                "id": session_id,
                "owner": owner,
                "name": name,
                "type": request["type"],
                "session": session,
                "active": None,
                "closing": None,
            }
            self._sessions[session_id] = record
            return self._snapshot(record, session.motd)
        except Exception as error:
            if isinstance(error, TerminalBackendCleanupError):
                cleanup_failure = {"error": error.cleanup_error}
            rollback_failure = None
            if session is not None and session_id not in self._sessions:
                try:
                    session.close("PTY spawn rolled back")
                except Exception as close_error:
                    rollback_failure = {"error": close_error}
                    cleanup_failure = rollback_failure
            failure = error
            try:
                _throw_if_aborted(signal)
                spawn_reservation["signal"].throw_if_aborted()
            except BaseException as cancellation:
                failure = cancellation
            if rollback_failure is not None and not (signal is not None and signal.is_set()):
                raise _aggregate([failure, rollback_failure["error"]], "PTY spawn and rollback both failed")
            raise failure
        finally:
            spawn_reservation["release"](cleanup_failure)
            release_name()

    def has_owner_activity(self, owner) -> bool:
        """exact owner 是否有已发布会话或未发布 spawn（spawn→close 全程无空窗）。"""
        return (len(self._pending_spawns.get(owner, ())) > 0
                or any(record["owner"] is owner for record in self._sessions.values()))

    # ---------- 操作面 ----------

    def start_send(self, owner, session_id: str, request: dict):
        """启动一次独占交互式 send；活跃未结 → SEND_ACTIVE（对齐 index.ts:243-254）。"""
        record = self._expect_owned(owner, session_id)
        if record["closing"] is not None:
            raise RuntimeError(f"PTY session {session_id} is closing")
        active = record["active"]
        if active is not None:
            # 同步载体：已 settle 的 operation 即刻让位（对齐上游 op.done.then 的清理时机）。
            if getattr(active, "settled", False):
                record["active"] = None
            else:
                raise TerminalError(f"PTY session {session_id} already has an active send", "SEND_ACTIVE")
        operation = record["session"].start_send(request)
        record["active"] = operation
        return operation

    def read(self, owner, session_id: str, request: dict | None = None) -> dict:
        return self._expect_owned(owner, session_id)["session"].read(dict(request or {}))

    def signal(self, owner, session_id: str, signal: str) -> dict:
        return self._expect_owned(owner, session_id)["session"].signal(signal)

    def kill(self, owner, session_id: str, reason: str = "model request") -> bool:
        """幂等关闭；会话移除仅在 backend 清理结算后发生（对齐 index.ts:285-301）。"""
        record = self._expect_owned(owner, session_id)
        if record["closing"] is not None:
            return False
        record["closing"] = True
        try:
            record["session"].close(reason)
        except Exception:
            record["closing"] = None
            raise
        self._sessions.pop(session_id, None)
        return True

    def list(self, owner) -> list:
        return [self._snapshot(record) for record in self._sessions.values()
                if record["owner"] is owner]

    # ---------- 内部 ----------

    def _assert_active(self) -> None:
        if self._disposing:
            raise TerminalError("PTY service is disposing", "SERVICE_DISPOSING")

    def _is_live_owner(self, owner) -> bool:
        if owner in self._disposed_owners:
            return False
        agents = self.ctx.get("agents")
        return agents is not None and agents.get(owner.id) is owner

    def _ensure_owner_cleanup(self, owner) -> None:
        if not self._is_live_owner(owner):
            raise TerminalError(f'agent "{owner.id}" is not the registered PTY owner', "OWNER_NOT_LIVE")
        if owner in self._owner_cleanups:
            return

        def detach() -> None:
            self._disposed_owners.add(owner)
            self._owner_cleanups.pop(owner, None)
            self._dispose_owned(owner)

        owner.ctx.effect(lambda: detach, f"pty.ownerCleanup({owner.id})")
        self._owner_cleanups[owner] = detach

    def _reserve_name(self, owner, name: str | None):
        if name is None:
            return lambda: None
        for record in self._sessions.values():
            if record["owner"] is owner and record["name"] == name:
                raise TerminalError(f'PTY session name "{name}" already exists for this owner', "DUPLICATE_NAME")
        reserved = self._reserved_names.setdefault(owner, set())
        if name in reserved:
            raise TerminalError(f'PTY session name "{name}" is already being created', "DUPLICATE_NAME")
        reserved.add(name)

        def release() -> None:
            reserved.discard(name)
            if not reserved:
                self._reserved_names.pop(owner, None)

        return release

    def _reserve_spawn(self, owner) -> dict:
        controller = Cancellation()
        pending = {"owner": owner, "controller": controller, "settled": False, "cleanup_failure": None}
        owned = self._pending_spawns.setdefault(owner, [])
        owned.append(pending)

        def release(cleanup_failure) -> None:
            pending["cleanup_failure"] = cleanup_failure
            if cleanup_failure is None:
                self._remove_pending_spawn(pending)
            pending["settled"] = True

        return {"signal": controller, "release": release}

    def _remove_pending_spawn(self, pending) -> None:
        owned = self._pending_spawns.get(pending["owner"])
        if owned is None or pending not in owned:
            return
        owned.remove(pending)
        if not owned:
            self._pending_spawns.pop(pending["owner"], None)

    def _abort_pending_spawns(self, owner, reason) -> None:
        pending = ([p for p in self._pending_spawns.get(owner, ())]
                   if owner is not None
                   else [p for owned in self._pending_spawns.values() for p in owned])
        for spawn in pending:
            spawn["controller"].abort(reason)
        # 同步单线程：pending spawn 无跨调度帧驻足，全部已 release。
        for spawn in pending:
            self._remove_pending_spawn(spawn)
        failures = [spawn["cleanup_failure"]["error"]
                    for spawn in pending if spawn["cleanup_failure"] is not None]
        if failures:
            raise _aggregate(failures, "failed to roll back unpublished PTY setup")

    def _expect_owned(self, owner, session_id: str) -> dict:
        record = self._sessions.get(session_id)
        if record is None:
            raise TerminalError(f"unknown PTY session {session_id}", "NO_SESSION")
        if record["owner"] is not owner:
            raise TerminalError(f"PTY session {session_id} belongs to another agent", "FOREIGN_SESSION")
        return record

    def _snapshot(self, record: dict, motd: str | None = None) -> dict:
        result = {
            "sessionId": record["id"],
            "type": record["type"],
            "status": record["session"].status(),
        }
        if record["name"] is not None:
            result["name"] = record["name"]
        pid = record["session"].pid
        if pid is not None:
            result["pid"] = pid
        if motd is not None:
            result["motd"] = motd
        return result

    # ---------- 拆解 ----------

    def _abort_and_close(self, owner, abort_reason, close_reason) -> None:
        failures = []
        try:
            self._abort_pending_spawns(owner, abort_reason)
        except Exception as error:
            failures.append(error)
        records = [record for record in self._sessions.values()
                   if owner is None or record["owner"] is owner]
        try:
            self._close_records(records, close_reason)
        except Exception as error:
            failures.append(error)
        if failures:
            raise _aggregate(failures, "failed to clean up PTY lifecycle")

    def _dispose_owned(self, owner) -> None:
        try:
            self._abort_and_close(
                owner,
                TerminalError("PTY owner is no longer live", "OWNER_NOT_LIVE"),
                "PTY owner disposed",
            )
        finally:
            self._reserved_names.pop(owner, None)

    def _dispose_all(self) -> None:
        self._disposing = True
        # 尽力而为：close 失败也要清空注册表并跑 owner 清理（index.ts:435-454）。
        try:
            self._abort_and_close(
                None,
                TerminalError("PTY service is disposing", "SERVICE_DISPOSING"),
                "PTY service disposed",
            )
        finally:
            self._backends.clear()
            self._reserved_names.clear()
            self._pending_spawns.clear()
            cleanups = list(self._owner_cleanups.values())
            self._owner_cleanups.clear()
            for cleanup in cleanups:
                cleanup()

    def _close_records(self, records, reason: str) -> None:
        failures = []
        for record in records:
            try:
                record["session"].close(reason)
                self._sessions.pop(record["id"], None)
            except Exception as error:
                # 同步单线程无并发重入：失败即保留（closing 复位）供重试。
                record["closing"] = None
                failures.append(error)
        if failures:
            raise _aggregate(failures, f"failed to close {len(failures)} PTY session(s)")


def _throw_if_aborted(signal) -> None:
    if signal is not None:
        signal.throw_if_aborted()


def _aggregate(failures, message: str) -> Exception:
    errors = list(failures)
    if not errors:
        return RuntimeError(message)
    try:
        return ExceptionGroup(message, errors)
    except (NameError, TypeError):
        return RuntimeError(f"{message}: " + "; ".join(str(e) for e in errors))


def install_terminals(ctx: Context) -> TerminalSessionService:
    """幂等装配：创建 ctx.terminals 服务（对齐 install_agents 惯例）。"""
    service = ctx.get("terminals")
    if service is None:
        service = TerminalSessionService(ctx)
    return service