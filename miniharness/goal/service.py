"""GoalService（ctx.goals）：事件溯源目标域 + 进程内 continuation 激活。

上游对照：packages/goal/goal/src/index.ts（GoalService，TypertRemoteService）。

契约（与上游一致）：
  * 状态唯一事实来源是会话日志的 `goal/change` 全快照/墓碑事件；进程内 cache
    沿日志增量折叠（apply_goal_event），activation 是进程内态（不重放）。
  * 变更全为 compare-and-set：edit/pause/resume/complete/block/clear 要求精确
    当前 revision，stale ref → GOAL_STALE_REVISION；create 仅允许在当前为
    undefined 或 complete 时。
  * 迁移后为每个 mutation 发进程内 `goal/changed` 通知（上游 agentEvents.emit）。
  * 错误统一 GoalError(message, code)，code 与上游一致（GOAL_ALREADY_EXISTS /
    GOAL_STALE_REVISION / GOAL_INVALID_TRANSITION / GOAL_NOT_FOUND / ...）。

mini 简化（须在文档中标注）：无 Typert remote 边界（remoteExport* 未复现）；
session-start 重置 activation 依赖 cache 创建时机（mini Session 为进程内对象，
fork/恢复天然新 cache → 'disarmed'）。
"""
from __future__ import annotations

import re
import time
import uuid

from ..core.scope import Context
from ..core.agents import assert_live_agent
from .domain import (
    GOAL_CHANGE_VERSION,
    GoalError,
    apply_goal_event,
    goal_change_ref,
)

__all__ = ["GoalService", "install_goals"]

#: create 请求省略上限时的部署默认（上游 GoalService.Config defaultMaxGoalRounds=256）。
DEFAULT_MAX_GOAL_ROUNDS = 256

_KEBAB_RE = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*")


def _resolve_max_goal_rounds(value) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise GoalError("maxGoalRounds must be a positive safe integer", "GOAL_INVALID_MAX_ROUNDS")
    return value


def _resolve_objective(value) -> str:
    if not isinstance(value, str) or value.strip() == "":
        raise GoalError("goal objective must be a non-empty string", "GOAL_INVALID_OBJECTIVE")
    return value.strip()


def _resolve_block_reason(value) -> dict:
    if not isinstance(value, dict):
        raise GoalError(
            "goal block reason requires a lower-kebab-case code and a non-empty message",
            "GOAL_INVALID_BLOCK_REASON")
    code = value.get("code")
    message = value.get("message")
    if not isinstance(code, str) or not _KEBAB_RE.fullmatch(code) \
            or not isinstance(message, str) or message.strip() == "":
        raise GoalError(
            "goal block reason requires a lower-kebab-case code and a non-empty message",
            "GOAL_INVALID_BLOCK_REASON")
    return {"code": code, "message": message.strip()}


class GoalService:
    """同一会话目标域服务：事件溯源状态 + compare-and-set 变更 + 激活。"""

    def __init__(self, ctx: Context, default_max_goal_rounds: int = DEFAULT_MAX_GOAL_ROUNDS):
        self._ctx = ctx
        self._default_max = _resolve_max_goal_rounds(default_max_goal_rounds)
        self._caches: dict[int, dict] = {}   # id(session) -> cache
        ctx.provide("goals", self)

    # ---------- 内部：cache 与增量折叠 ----------

    def _cache(self, session) -> dict:
        key = id(session)
        cache = self._caches.get(key)
        if cache is not None:
            return cache
        state = {"goal": None, "roundsStarted": 0, "createdAt": None,
                 "updatedAt": None, "lastRef": None, "seenGoalIds": set()}
        for event in session.events:
            apply_goal_event(state, event)
        cache = {
            "state": state,
            "activation": "disarmed",   # 恢复/fork 后需人授权重新 armed（上游 session-start 同语义）
            "observedSeq": session.seq,
            "pendingActivation": None,
        }
        self._caches[key] = cache
        return cache

    def _sync(self, session, cache: dict) -> None:
        """增量观察 durable 事件并调和本地激活意图（上游 sync）。"""
        for event in session.events[cache["observedSeq"]:]:
            apply_goal_event(cache["state"], event)
            if event["type"] == "goal/change":
                pending = cache["pendingActivation"]
                cache["activation"] = pending["activation"] if pending is not None \
                    and pending["seq"] == event["seq"] else "disarmed"
            cache["observedSeq"] += 1

    def _prepare_mutation(self, agent) -> dict:
        # R4：沿用 jobs 公共的 assertLive 边界——agent 须为 ctx.agents 中当前登记的
        # live 实例（陈旧/重复实例拒绝；未安装 agents 服务的裸装配不强制）。
        assert_live_agent(agent)
        cache = self._cache(agent.session)
        self._sync(agent.session, cache)
        return cache

    def _expect_current(self, cache: dict, ref: dict) -> dict:
        current = cache["state"]["goal"]
        if current is None:
            raise GoalError("no current goal", "GOAL_NOT_FOUND")
        if ref.get("id") != current["id"] or ref.get("revision") != current["revision"]:
            raise GoalError(
                f'stale goal ref "{ref.get("id")}" revision {ref.get("revision")}; '
                f'current is "{current["id"]}" revision {current["revision"]}',
                "GOAL_STALE_REVISION")
        return current

    def _next_mutation_time(self, cache: dict) -> int:
        updated_at = cache["state"]["updatedAt"]
        if updated_at is None:
            raise GoalError("current goal cache lacks updatedAt", "GOAL_INVALID_STATE")
        return max(int(time.time() * 1000), updated_at)

    def _commit(self, agent, cache: dict, change: dict, activation: str) -> None:
        ref = goal_change_ref(change)
        cache["pendingActivation"] = {"seq": agent.session.seq, "activation": activation}
        try:
            agent.session.append("goal/change", change)
            self._sync(agent.session, cache)
        finally:
            cache["pendingActivation"] = None
        goal = self._view(cache)
        notification = {"operation": change["operation"], "ref": dict(ref)}
        if goal is not None:
            notification["goal"] = goal
        # 上游经 agentEvents(ctx, agent) 载波派发（监听器收 {agent, change}）；
        # mini 载体：agent 进 payload（goal driver 的 host-pause 边界需要它）
        self._ctx.emit("goal/changed", {"change": notification, "agent": agent})

    def _commit_snapshot(self, agent, cache: dict, operation: str, goal: dict,
                         rounds_started: int, created_at: int, updated_at: int,
                         activation: str) -> dict:
        change = {
            "kind": "goal/change",
            "version": GOAL_CHANGE_VERSION,
            "operation": operation,
            "goal": goal,
            "roundsStarted": rounds_started,
            "createdAt": created_at,
            "updatedAt": updated_at,
        }
        self._commit(agent, cache, change, activation)
        view = self._view(cache)
        if view is None:
            raise GoalError("snapshot commit cleared the goal unexpectedly", "GOAL_INVALID_STATE")
        return view

    def _commit_current(self, agent, cache: dict, operation: str, goal: dict,
                        activation: str) -> dict:
        created_at = cache["state"]["createdAt"]
        if created_at is None:
            raise GoalError("current goal cache lacks createdAt", "GOAL_INVALID_STATE")
        return self._commit_snapshot(
            agent, cache, operation, goal, cache["state"]["roundsStarted"],
            created_at, self._next_mutation_time(cache), activation)

    def _with_phase(self, current: dict, phase: str) -> dict:
        return {
            "id": current["id"],
            "revision": current["revision"] + 1,
            "objective": current["objective"],
            "phase": phase,
            "maxGoalRounds": current["maxGoalRounds"],
        }

    def _transition(self, agent, ref: dict, operation: str, allowed: tuple, phase: str,
                    activation: str) -> dict:
        cache = self._prepare_mutation(agent)
        current = self._expect_current(cache, ref)
        if current["phase"] not in allowed:
            raise GoalError(
                f'cannot {operation} goal "{current["id"]}" from phase "{current["phase"]}"; '
                f"expected {' or '.join(allowed)}",
                "GOAL_INVALID_TRANSITION")
        return self._commit_current(agent, cache, operation, self._with_phase(current, phase), activation)

    def _view(self, cache: dict) -> dict | None:
        goal = cache["state"]["goal"]
        created_at = cache["state"]["createdAt"]
        updated_at = cache["state"]["updatedAt"]
        if goal is None:
            return None
        if created_at is None or updated_at is None:
            raise GoalError(f"goal {goal['id']!r} cache lacks timestamps", "GOAL_INVALID_STATE")
        return {
            **goal,
            "roundsStarted": cache["state"]["roundsStarted"],
            "createdAt": created_at,
            "updatedAt": updated_at,
            "activation": cache["activation"],
        }

    # ---------- 公开 API（对齐上游 GoalService） ----------

    def get(self, agent) -> dict | None:
        """读取当前目标视图；无当前目标返回 None（上游 get）。"""
        cache = self._prepare_mutation(agent)
        return self._view(cache)

    def disarm(self, agent) -> dict | None:
        """移除进程内 continuation 权威，不改 durable 相位/revision（上游 disarm）。"""
        cache = self._prepare_mutation(agent)
        cache["activation"] = "disarmed"
        return self._view(cache)

    def create(self, agent, request: dict) -> dict:
        """创建并 armed 一个目标；已 complete 目标可替换（上游 create）。"""
        objective = _resolve_objective(request.get("objective"))
        max_goal_rounds = _resolve_max_goal_rounds(
            request.get("maxGoalRounds", self._default_max))
        cache = self._prepare_mutation(agent)
        current = cache["state"]["goal"]
        if current is not None and current["phase"] != "complete":
            raise GoalError(
                f'goal "{current["id"]}" already exists with phase "{current["phase"]}"',
                "GOAL_ALREADY_EXISTS")
        now = int(time.time() * 1000)
        goal = {
            "id": f"goal-{uuid.uuid4()}",
            "revision": 1,
            "objective": objective,
            "phase": "active",
            "maxGoalRounds": max_goal_rounds,
        }
        return self._commit_snapshot(agent, cache, "create", goal, 0, now, now, "armed")

    def edit(self, agent, ref: dict, request: dict) -> dict:
        """编辑 objective 和/或上限，不改相位（上游 edit）。"""
        cache = self._prepare_mutation(agent)
        current = self._expect_current(cache, ref)
        if request.get("objective") is None and request.get("maxGoalRounds") is None:
            raise GoalError("goal edit requires objective and/or maxGoalRounds", "GOAL_INVALID_EDIT")
        goal = {"id": current["id"], "revision": current["revision"] + 1,
                "objective": current["objective"], "phase": current["phase"],
                "maxGoalRounds": current["maxGoalRounds"]}
        if request.get("objective") is not None:
            goal["objective"] = _resolve_objective(request["objective"])
        if request.get("maxGoalRounds") is not None:
            goal["maxGoalRounds"] = _resolve_max_goal_rounds(request["maxGoalRounds"])
        if current.get("blockedReason") is not None:
            goal["blockedReason"] = current["blockedReason"]
        return self._commit_current(agent, cache, "edit", goal, cache["activation"])

    def pause(self, agent, ref: dict) -> dict:
        """暂停 active 目标并 disarm（上游 pause）。"""
        return self._transition(agent, ref, "pause", ("active",), "paused", "disarmed")

    def resume(self, agent, ref: dict) -> dict:
        """恢复并 armed，或重 arm 已 active 目标；round 预算需有余量（上游 resume）。"""
        cache = self._prepare_mutation(agent)
        current = self._expect_current(cache, ref)
        if current["phase"] not in ("active", "paused", "blocked"):
            raise GoalError(
                f'cannot resume goal "{current["id"]}" from phase "{current["phase"]}"; '
                "expected active or paused or blocked",
                "GOAL_INVALID_TRANSITION")
        if current["phase"] == "active" and cache["activation"] == "armed":
            raise GoalError(
                f'goal "{current["id"]}" is already active and armed', "GOAL_INVALID_TRANSITION")
        if cache["state"]["roundsStarted"] >= current["maxGoalRounds"]:
            raise GoalError(
                f'goal "{current["id"]}" exhausted {current["maxGoalRounds"]} goal rounds; '
                "increase maxGoalRounds before resuming",
                "GOAL_INVALID_TRANSITION")
        return self._commit_current(agent, cache, "resume", self._with_phase(current, "active"), "armed")

    def complete(self, agent, ref: dict) -> dict:
        """把当前非 complete 目标标记完成并 disarm（上游 complete）。"""
        return self._transition(agent, ref, "complete",
                                ("active", "paused", "blocked"), "complete", "disarmed")

    def block(self, agent, ref: dict, reason: dict) -> dict:
        """把 active 目标标记 blocked 并 disarm，携带 durable 原因（上游 block）。"""
        cache = self._prepare_mutation(agent)
        current = self._expect_current(cache, ref)
        if current["phase"] != "active":
            raise GoalError(
                f'cannot block goal "{current["id"]}" from phase "{current["phase"]}"; expected active',
                "GOAL_INVALID_TRANSITION")
        goal = {**self._with_phase(current, "blocked"), "blockedReason": _resolve_block_reason(reason)}
        return self._commit_current(agent, cache, "block", goal, "disarmed")

    def clear(self, agent, ref: dict) -> dict:
        """清空当前目标，保留 durable 墓碑与历史（上游 clear）。"""
        cache = self._prepare_mutation(agent)
        current = self._expect_current(cache, ref)
        tombstone = {"id": current["id"], "revision": current["revision"] + 1}
        change = {
            "kind": "goal/change",
            "version": GOAL_CHANGE_VERSION,
            "operation": "clear",
            "cleared": tombstone,
            "clearedAt": self._next_mutation_time(cache),
        }
        self._commit(agent, cache, change, "disarmed")
        return {"id": tombstone["id"], "revision": tombstone["revision"]}


def install_goals(ctx: Context, default_max_goal_rounds: int = DEFAULT_MAX_GOAL_ROUNDS) -> GoalService:
    """提供 ctx.goals 服务并返回 GoalService（幂等装配入口）。"""
    return GoalService(ctx, default_max_goal_rounds)
