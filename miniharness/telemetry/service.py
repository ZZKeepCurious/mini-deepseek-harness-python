"""会话统计 + 用量统计服务：`ctx.usageStats` 增量镜像（事件订阅面）。

对照上游，能力本体 = sessionProjections 注册表上的 sessionStats/tokenUsage
单位（packages/session/session-stats、packages/llm/token-meter）。mini 不建
完整注册表，以**固定两个单位的进程内镜像服务 + 现场折叠**达成等价（简化登记
见 verified-diffs §2.30）：

  * `UsageStatsService(Service)`：opt-in，构造即经 ctx.provide 登记
    provide="usageStats"。订阅 `session/event`（增量预折叠）与
    `session/disposed`（弃置该会话状态，内存卫生），listener 异常 contained。
    未订阅会话（服务装于既有会话之后 / 冷会话）在读取时 `_sync` 补折叠。
  * `projection_values(session, service=None)` 自由函数：无服务时现场从
    session.events 折叠出 `{sessionStats, tokenUsage}` 两个 wire 视图，
    供 wire/CLI 直接使用（demo/headless 不装服务也可产出真实投影）。

fold 语义见 `folds.py`（逐事件对照上游 projection.ts / usage-projection.ts）；
服务只做「按会话组织 fold 状态 + 生命周期收纳」，不含任何逻辑改写。
"""
from __future__ import annotations

from typing import Any

from ..core.scope import Context, Service
from .folds import fold_session_stats, fold_token_usage, session_stats_view, token_usage_view

__all__ = ["UsageStatsService", "install_usage_stats", "projection_values"]


def projection_values(session, service: "UsageStatsService | None" = None) -> dict:
    """会话的投影视图 `{sessionStats, tokenUsage}`（wire values 块本体）。

    有服务用服务的镜像状态（增量已折叠），否则现场从 session.events 全量折叠。
    """
    if service is not None:
        return service._views(session)
    state = {"session_stats": None, "token_usage": None, "consumedEvents": 0}
    UsageStatsService._sync_all(state, list(session.events))
    return {
        "sessionStats": session_stats_view(_ensure_stats(state["session_stats"])),
        "tokenUsage": token_usage_view(_ensure_usage(state["token_usage"])),
    }


def _ensure_usage(value: Any) -> Any:
    from .folds import init_token_usage
    return value if value is not None else init_token_usage()


def _ensure_stats(value: Any) -> Any:
    from .folds import init_session_stats
    return value if value is not None else init_session_stats()


class UsageStatsService(Service):
    """`ctx.usageStats`：按会话维护 sessionStats/tokenUsage 增量 fold 状态。

    事件订阅面与 goal/jobs 同构：`session/event` 逐条预折叠（O(1)/事件），
    `session/disposed` 弃置对应会话状态。冷会话/session 未进订阅时，读取侧
    `_sync` 按 `consumedEvents` 游标补齐，任何时刻与从日志全量折叠结果一致。
    """

    provide = "usageStats"

    def __init__(self, ctx: Context):
        self._states: dict[str, dict] = {}
        self._disposers: list[Any] = []
        super().__init__(ctx, "usageStats")
        self._disposers.append(ctx.on("session/event", self._on_session_event))
        self._disposers.append(ctx.on("session/disposed", self._on_session_disposed))

    def dispose(self) -> None:
        for disposer in self._disposers:
            disposer()
        self._disposers.clear()
        self._states.clear()

    # ---------- 事件订阅 ----------

    def _on_session_event(self, payload: dict) -> None:
        session = payload.get("session")
        event = payload.get("event")
        if session is None or event is None:
            return
        state = self._states.get(session.session_id)
        if state is None:
            return
        self._sync_state(state, [event])

    def _on_session_disposed(self, payload: dict) -> None:
        session = payload.get("session")
        if session is None:
            return
        self._states.pop(session.session_id, None)

    # ---------- 读取 ----------

    def session_stats(self, session) -> dict:
        return session_stats_view(self._fold(session)[0])

    def token_usage(self, session) -> dict:
        return token_usage_view(self._fold(session)[1])

    def _views(self, session) -> dict:
        s, u = self._fold(session)
        return {"sessionStats": session_stats_view(s), "tokenUsage": token_usage_view(u)}

    def _fold(self, session):
        state = self._states.get(session.session_id)
        if state is None:
            state = self._new_state()
            self._states[session.session_id] = state
        self._sync_state(state, list(session.events))
        from .folds import init_session_stats, init_token_usage
        s = state["session_stats"] or init_session_stats()
        u = state["token_usage"] or init_token_usage()
        return s, u

    @staticmethod
    def _new_state() -> dict:
        return {"session_stats": None, "token_usage": None, "consumedEvents": 0}

    @staticmethod
    def _sync_all(state: dict, events: list) -> None:
        """对任意状态按事件列表补齐折叠（供服务与 projection_values 复用）。"""
        from .folds import init_session_stats, init_token_usage
        while state["consumedEvents"] < len(events):
            event = events[state["consumedEvents"]]
            if state["session_stats"] is None:
                state["session_stats"] = init_session_stats()
            if state["token_usage"] is None:
                state["token_usage"] = init_token_usage()
            state["session_stats"] = fold_session_stats(state["session_stats"], event)
            state["token_usage"] = fold_token_usage(state["token_usage"], event)
            state["consumedEvents"] += 1

    def _sync_state(self, state: dict, events: list) -> None:
        self._sync_all(state, events)


def install_usage_stats(ctx: Context) -> UsageStatsService:
    """装配 `ctx.usageStats`（opt-in，重复装返回既有实例；模式同 install_*）。"""
    existing = ctx.get("usageStats")
    if existing is not None:
        return existing
    return UsageStatsService(ctx)