"""Team 成员身份、continuable 子代理供给、roster 属主拆除（对齐上游 roster.ts）。

mini 载体差异（verified-diffs §2.29）：
  * no-Await 同步编排：spawnTeammate / checkpointInitialPrompt / settleProvisioning
    同步直跑——同步载体下 start_continuable 带 prompt 即内联跑完首回合，初始
    prompt 已在子会话日志，checkpoint 首轮 flush 即收敛；
  * reconcileProvisioning 读取持久化 `persistence.inspect`（冷路径，同上游
    readPersistedSession）；宿主恢复的边缘仍由 agent/session-start 观察者触发。
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from typing import Any

from ...core.agent_loop.agent import AgentLoop
from ...core.session import create_message
from ...core.session.persistence import inherited_cut
from .error import TeamError, error_message
from .journal import TeamJournal
from .lifecycle import TeamRuntimeLifecycle
from .projection import TeamState
from .types import (
    TeamMemberSnapshot,
    TeamMemberView,
    TeamMembership,
    SpawnTeammateRequest,
    SpawnTeammateResult,
)
from .validation import required_text

__all__ = [
    "MEMBER_NAME",
    "TeamRoster",
    "resolve_active_member",
    "read_persisted_session",
    "message_accepted",
    "pending_inbox_messages",
    "agent_model",
]

MEMBER_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

MAX_MEMBER_NAME_LENGTH = 64
MAX_MEMBER_TEXT_LENGTH = 200
_CHECKPOINT_TIMEOUT_MS = 5_000
_CHECKPOINT_POLL_MS = 10


def resolve_active_member(
    root: AgentLoop, state: TeamState, raw_name: str
) -> dict:
    """按模型可见名解析一位 active Team 成员（含 Lead 伪行）。

    对齐上游 resolveActiveMember：'lead' → Lead 本人；否则查 active 成员，
    未命中抛 TEAM_MEMBER_NOT_FOUND。
    """
    name = raw_name.strip()
    if name == "lead":
        return {"id": root.id, "name": name}
    member = next((m for m in state.members if m.name == name), None)
    if member is None or member.phase != "active":
        raise TeamError(f'active teammate "{name}" not found', "TEAM_MEMBER_NOT_FOUND")
    return {"id": member.id, "name": name}


def pending_inbox_messages(events: list[dict]) -> list[dict]:
    """把 durable inbox 后缀折叠为仍待认领的消息（对齐上游 pendingInboxMessages）。"""
    inbox = {"next-turn": [], "next-step": []}
    for event in events:
        if event.get("type") != "agent/inbox/spliced":
            continue
        data = event.get("data") or {}
        target = data.get("target")
        if target not in inbox:
            continue
        pending = inbox[target]
        start = data.get("start", len(pending))
        removed = data.get("removedCount") or 0
        inserted = [dict(x) for x in (data.get("inserted") or [])]
        pending[start : start + removed] = inserted
    return list(inbox["next-turn"]) + list(inbox["next-step"])


def message_accepted(events: list[dict], predicate) -> bool:
    """一条消息是否模型可见或仍 durably pending（对齐上游 messageAccepted）。"""
    for event in events:
        if event.get("type") == "user/message" and predicate(event.get("data") or {}):
            return True
    return any(predicate(msg) for msg in pending_inbox_messages(events))


def read_persisted_session(persistence: Any, session_id: str) -> dict:
    """一次持久化会话读：{header(meta), inheritedEventCount, events}（对齐上游
    readPersistedSession 的关读语义；mini inspect 即一次性短读）。"""
    info = persistence.inspect(session_id)
    meta = info.get("meta") if isinstance(info, dict) else None
    events = info.get("events") or []
    if not isinstance(meta, dict):
        meta = {}
    cut = inherited_cut(meta, events)
    return {
        "header": dict(meta),
        "inheritedEventCount": cut,
        "events": list(events),
    }


def agent_model(loop: AgentLoop | None) -> str | None:
    """运行 agent 的当前模型名（上游 options.model / live?.options.model）。"""
    if loop is None:
        return None
    model = getattr(getattr(loop, "adapter", None), "model", None)
    return model if isinstance(model, str) and model else None


class TeamRoster:
    """Owns Team identities and the lifecycle of rostered continuable children."""

    def __init__(
        self,
        agents: Any,
        sessions: Any,
        persistence: Any,
        manager: Any,
        journal: TeamJournal,
        lifecycle: TeamRuntimeLifecycle,
        max_members: int,
    ):
        self._agents = agents
        self._sessions = sessions
        self._persistence = persistence
        self._manager = manager
        self._journal = journal
        self._lifecycle = lifecycle
        self._max_members = max_members

    # ---------- 身份 ----------

    def membership(self, agent: AgentLoop) -> TeamMembership:
        membership = self.try_membership(agent)
        if membership is None:
            raise TeamError(
                f'agent "{agent.id}" is not a member of an active Agent Team',
                "TEAM_NOT_MEMBER",
            )
        return membership

    def try_membership(self, agent: AgentLoop) -> TeamMembership | None:
        """不抛的成员探测（scoped 安装与生命周期观察者用）；stale/非 roster → None。"""
        try:
            if self._agents.get(agent.id) is not agent:
                return None
            parent_id = (agent.session.meta or {}).get("parentSession")
            if parent_id is not None:
                root = self._agents.get(parent_id)
                if root is not None:
                    member = next(
                        (m for m in self._journal.state(root).members if m.id == agent.id),
                        None,
                    )
                    if member is not None and member.phase in ("active", "provisioning"):
                        return TeamMembership(root=root, root_id=root.id,
                                              role="teammate", name=member.name)
                    if self._subagent_descriptor(agent):
                        return None
                    return TeamMembership(root=agent, root_id=agent.id,
                                          role="lead", name="lead")
            if self._subagent_descriptor(agent):
                return None
            return TeamMembership(root=agent, root_id=agent.id, role="lead", name="lead")
        except Exception:
            return None

    # ---------- 枚举 / 富化 ----------

    def list(self, membership: TeamMembership) -> list[TeamMemberView]:
        root = membership.root
        state = self._journal.state(root)
        result: list[TeamMemberView] = [
            TeamMemberView(
                id=root.id,
                name="lead",
                role="lead",
                status=getattr(root, "status", "idle") or "inactive",
                model=agent_model(root),
            )
        ]
        for member in state.members:
            live = self._agents.get(member.id)
            status: str
            if member.phase == "failed":
                status = "failed"
            elif member.phase == "provisioning":
                status = "provisioning"
            else:
                status = (live.status if live is not None else "inactive") or "inactive"
            result.append(TeamMemberView(
                id=member.id,
                name=member.name,
                role="teammate",
                status=status,
                description=member.description,
                provider=member.provider,
                context=member.context,
                model=agent_model(live),
                diagnostics=() if member.error is None else (member.error,),
            ))
        return result

    def live_children_by_root(self) -> dict[AgentLoop, list[str]]:
        """把 registry 中按当前 Lead 分组的 roster 在世子 agent id 集合化。"""
        teams: dict[AgentLoop, list[str]] = {}
        for loop in self._agents.list():
            parent_id = (loop.session.meta or {}).get("parentSession")
            if parent_id is None:
                continue
            root = self._agents.get(parent_id)
            if root is None:
                continue
            if not any(m.id == loop.id for m in self._journal.state(root).members):
                continue
            teams.setdefault(root, []).append(loop.id)
        return teams

    # ---------- 创建 / 结算 ----------

    def spawn(self, caller: AgentLoop, request: SpawnTeammateRequest) -> SpawnTeammateResult:
        self._lifecycle.assert_admitting()
        return self._spawn_admitted(caller, request)

    async def spawn_async(
        self, caller: AgentLoop, request: SpawnTeammateRequest,
    ) -> SpawnTeammateResult:
        """async 版 spawn：事件循环内创建 teammate（工具/驱动载体）。

        与同步版共享 admission 与结算；仅投递走 start_continuable_async +
        _checkpoint_initial_prompt_async（内联泵/让出式等待，不在循环内
        time.sleep 阻塞驱动，见 _spawn_admitted_async）。
        """
        self._lifecycle.assert_admitting()
        return await self._spawn_admitted_async(caller, request)

    def _spawn_admitted(
        self, caller: AgentLoop, request: SpawnTeammateRequest
    ) -> SpawnTeammateResult:
        membership = self.membership(caller)
        if membership.role != "lead":
            raise TeamError("only the Team Lead can create teammates", "TEAM_LEAD_REQUIRED")
        self._lifecycle.assert_admitting()
        root = membership.root
        name = self._member_name(request.name)
        description = required_text(request.description, "description", MAX_MEMBER_TEXT_LENGTH)
        provider = required_text(request.provider, "provider", MAX_MEMBER_TEXT_LENGTH)
        child_id = str(uuid.uuid4())
        member = TeamMemberSnapshot(
            id=child_id,
            name=name,
            description=description,
            provider=provider,
            context=request.context,
            phase="provisioning",
        )
        self._journal.transact(root.id, lambda: self._admit_provisioning(root, member))
        prompt_message = create_message("user", list(request.prompt), {"kind": "user"})
        try:
            started = self._manager.start_continuable(
                label=description, prompt=prompt_message, parent=root,
                child_id=child_id,
                agent_options={"provider": provider},
            )
            message_id = started["messageId"]
            self._checkpoint_initial_prompt(child_id, message_id, root)
        except BaseException as error:  # noqa: BLE001 - 失败边缘 + 终止结算聚合
            failed = TeamMemberSnapshot(
                id=member.id, name=member.name, description=member.description,
                provider=member.provider, context=member.context,
                phase="failed", error=error_message(error),
            )
            try:
                phase = self._settle_provisioning(root, failed)
                self.stop_teammates(root, [child_id])
                if phase == "active":
                    raise TeamError(
                        f'teammate "{name}" became active while its creator reported failure',
                        "TEAM_PROVISIONING_CONFLICT",
                    ) from error
            except TeamError:
                raise
            except BaseException as record_error:  # noqa: BLE001
                raise RuntimeError(
                    "teammate creation and durable failure recording both failed: "
                    f"{record_error}"
                ) from error
            raise error
        active = TeamMemberSnapshot(
            id=member.id, name=member.name, description=member.description,
            provider=member.provider, context=member.context, phase="active",
        )
        settled_phase = self._settle_provisioning(root, active)
        if settled_phase == "failed":
            conflict = TeamError(
                f'teammate "{name}" was reconciled as failed while creation was in progress',
                "TEAM_PROVISIONING_CONFLICT",
            )
            try:
                self.stop_teammates(root, [child_id])
            except BaseException as cleanup_error:  # noqa: BLE001
                raise RuntimeError(
                    "provisioning conflict cleanup failed: "
                    f"{cleanup_error}"
                ) from conflict
            raise conflict
        return SpawnTeammateResult(member=self._member_view(active))

    def _admit_provisioning(self, root: AgentLoop, member: TeamMemberSnapshot) -> None:
        state = self._journal.state(root)
        if any(m.name == member.name for m in state.members):
            raise TeamError(
                f'teammate name "{member.name}" was already used in this Team',
                "TEAM_MEMBER_NAME_TAKEN",
            )
        if len(state.members) >= self._max_members:
            raise TeamError(
                f"Team member limit {self._max_members} reached", "TEAM_MEMBER_LIMIT"
            )
        self._journal.append_and_flush(root, "team/member", {
            "version": 2, "teamId": root.id, "member": member.to_dict(),
        })

    def _checkpoint_initial_prompt(
        self, child_id: str, message_id: str, root: AgentLoop
    ) -> None:
        """在 Lead 提交 active 前，确认首条委托已 durable 被接受。

        同步载体：start_continuable 带 prompt 已内联泵完首回合，首轮 flush 即
        收敛；driver 载体异步投递未跑完回合 → 有界轮询（flush + 冷读）至
        收敛或超时，超时抛 TEAM_PROVISIONING_CONFLICT（对齐上游 await
        progress.promise 的等待语义，上限对抗编排死锁）。
        """
        deadline = time.monotonic() + _CHECKPOINT_TIMEOUT_MS / 1000
        while True:
            self._lifecycle.assert_admitting()
            self._sessions.flush(root.session)
            session = self._sessions.get(child_id)
            if session is not None:
                self._sessions.flush(session)
                if message_accepted(list(session.own_events()),
                                    lambda m: m.get("id") == message_id):
                    return
            else:
                stored = read_persisted_session(self._persistence, child_id)
                suffix = stored["events"][stored["inheritedEventCount"]:]
                if message_accepted(suffix, lambda m: m.get("id") == message_id):
                    return
            if time.monotonic() >= deadline:
                break
            time.sleep(_CHECKPOINT_POLL_MS / 1000)
        raise TeamError(
            f'teammate "{child_id}" initial prompt was not durably accepted',
            "TEAM_PROVISIONING_CONFLICT",
        )

    async def _spawn_admitted_async(
        self, caller: AgentLoop, request: SpawnTeammateRequest,
    ) -> SpawnTeammateResult:
        """async 版 _spawn_admitted：与同步版同一 admit→投递→checkpoint→结算
        次序；投递分支在循环内用 start_continuable_async（无 driver 父内联泵，
        有 driver 父 A8 上环），checkpoint 用 await asyncio.sleep 让出控制权，
        避免循环线程被 time.sleep 卡死导致子驱动永不推进（对齐上游 await
        progress.promise）。"""
        membership = self.membership(caller)
        if membership.role != "lead":
            raise TeamError("only the Team Lead can create teammates", "TEAM_LEAD_REQUIRED")
        self._lifecycle.assert_admitting()
        root = membership.root
        name = self._member_name(request.name)
        description = required_text(request.description, "description", MAX_MEMBER_TEXT_LENGTH)
        provider = required_text(request.provider, "provider", MAX_MEMBER_TEXT_LENGTH)
        child_id = str(uuid.uuid4())
        member = TeamMemberSnapshot(
            id=child_id,
            name=name,
            description=description,
            provider=provider,
            context=request.context,
            phase="provisioning",
        )
        self._journal.transact(root.id, lambda: self._admit_provisioning(root, member))
        prompt_message = create_message("user", list(request.prompt), {"kind": "user"})
        try:
            started = await self._manager.start_continuable_async(
                label=description, prompt=prompt_message, parent=root,
                child_id=child_id,
                agent_options={"provider": provider},
            )
            message_id = started["messageId"]
            await self._checkpoint_initial_prompt_async(child_id, message_id, root)
        except BaseException as error:  # noqa: BLE001 - 失败边缘 + 终止结算聚合
            failed = TeamMemberSnapshot(
                id=member.id, name=member.name, description=member.description,
                provider=member.provider, context=member.context,
                phase="failed", error=error_message(error),
            )
            try:
                phase = self._settle_provisioning(root, failed)
                self.stop_teammates(root, [child_id])
                if phase == "active":
                    raise TeamError(
                        f'teammate "{name}" became active while its creator reported failure',
                        "TEAM_PROVISIONING_CONFLICT",
                    ) from error
            except TeamError:
                raise
            except BaseException as record_error:  # noqa: BLE001
                raise RuntimeError(
                    "teammate creation and durable failure recording both failed: "
                    f"{record_error}"
                ) from error
            raise error
        active = TeamMemberSnapshot(
            id=member.id, name=member.name, description=member.description,
            provider=member.provider, context=member.context, phase="active",
        )
        settled_phase = self._settle_provisioning(root, active)
        if settled_phase == "failed":
            conflict = TeamError(
                f'teammate "{name}" was reconciled as failed while creation was in progress',
                "TEAM_PROVISIONING_CONFLICT",
            )
            try:
                self.stop_teammates(root, [child_id])
            except BaseException as cleanup_error:  # noqa: BLE001
                raise RuntimeError(
                    "provisioning conflict cleanup failed: "
                    f"{cleanup_error}"
                ) from conflict
            raise conflict
        return SpawnTeammateResult(member=self._member_view(active))

    async def _checkpoint_initial_prompt_async(
        self, child_id: str, message_id: str, root: AgentLoop
    ) -> None:
        """async 版 checkpoint：收敛条件与同步版一致，等待用 await asyncio.sleep
        让出控制（父有 driver 时子回合须借同一事件循环推进，time.sleep 会随
        循环线程一起卡死；对齐上游 await progress.promise 的异步等待语义）。"""
        deadline = time.monotonic() + _CHECKPOINT_TIMEOUT_MS / 1000
        while True:
            self._lifecycle.assert_admitting()
            self._sessions.flush(root.session)
            session = self._sessions.get(child_id)
            if session is not None:
                self._sessions.flush(session)
                if message_accepted(list(session.own_events()),
                                    lambda m: m.get("id") == message_id):
                    return
            else:
                stored = read_persisted_session(self._persistence, child_id)
                suffix = stored["events"][stored["inheritedEventCount"]:]
                if message_accepted(suffix, lambda m: m.get("id") == message_id):
                    return
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(_CHECKPOINT_POLL_MS / 1000)
        raise TeamError(
            f'teammate "{child_id}" initial prompt was not durably accepted',
            "TEAM_PROVISIONING_CONFLICT",
        )

    def recover_for(self, agent: AgentLoop) -> None:
        """一位成员 Session 启动后结算 provisioning-only 前缀（对齐上游 recoverFor）。

        仅 Lead 结算其 provisioning-only 成员（上游同判：role==='lead' 才
        reconcileProvisioning）。
        """
        membership = self.try_membership(agent)
        if membership is not None and membership.role == "lead":
            self.reconcile_provisioning(membership.root)

    def reconcile_provisioning(self, root: AgentLoop) -> None:
        """把 provisioning-only 成员从其独立 durable 子会话结算（agent/session-start）。"""
        for member in self._journal.state(root).members:
            if member.phase != "provisioning":
                continue
            if self._agents.get(member.id) is not None:
                continue  # 活体 child = 创建仍在进行，terminer 是创建者
            phase = "failed"
            failure = "provisioning did not leave a resumable child Session"
            try:
                loaded = read_persisted_session(self._persistence, member.id)
                suffix = loaded["events"][loaded["inheritedEventCount"]:]
                descriptor = self._fold_descriptor(suffix)
                accepted_initial = message_accepted(
                    suffix, lambda m: (m.get("source") or {}).get("kind") == "user"
                )
                if (
                    loaded["header"].get("parentSession") == root.id
                    and descriptor is not None
                    and descriptor.get("mode") == "continuable"
                    # mini 单一 in-process 续跑通道：descriptor.provider 恒为
                    # CONTINUATION_PROVIDER，请求方 provider 落在 agentProvider
                    # （见 verified-diffs §2.29 载体差异）——reconcile 对照
                    # agentProvider（上游对照 descriptor.provider === member.provider）
                    and descriptor.get("agentProvider") == member.provider
                    and accepted_initial
                ):
                    phase = "active"
                else:
                    failure = "persisted child Session does not match the provisioned continuation"
            except Exception as error:  # noqa: BLE001 - 读取失败视为 failed
                failure = f"child Session recovery failed: {error}"
            self._journal.transact(root.id, lambda: self._settle_reconcile(
                root, member, phase, failure,
            ))

    def _settle_reconcile(
        self, root: AgentLoop, member: TeamMemberSnapshot, phase: str, failure: str
    ) -> None:
        current = next((m for m in self._journal.state(root).members if m.id == member.id), None)
        if current is None or current.phase != "provisioning":
            return
        settled = TeamMemberSnapshot(
            id=current.id, name=current.name, description=current.description,
            provider=current.provider, context=current.context, phase=phase,
            error=failure if phase == "failed" else None,
        )
        self._journal.append_and_flush(root, "team/member", {
            "version": 2, "teamId": root.id, "member": settled.to_dict(),
        })

    def _settle_provisioning(
        self, root: AgentLoop, terminal: TeamMemberSnapshot
    ) -> str:
        def op() -> str:
            current = next(
                (m for m in self._journal.state(root).members if m.id == terminal.id), None
            )
            if current is None:
                raise TeamError(
                    f'provisioned teammate "{terminal.id}" disappeared',
                    "TEAM_PROVISIONING_CONFLICT",
                )
            if current.phase != "provisioning":
                return current.phase
            self._journal.append_and_flush(root, "team/member", {
                "version": 2, "teamId": root.id, "member": terminal.to_dict(),
            })
            return "active" if terminal.phase == "active" else "failed"
        return self._journal.transact(root.id, op)

    def _member_view(self, member: TeamMemberSnapshot) -> TeamMemberView:
        live = self._agents.get(member.id)
        return TeamMemberView(
            id=member.id,
            name=member.name,
            role="teammate",
            status=(live.status if live is not None else "inactive") or "inactive",
            description=member.description,
            provider=member.provider,
            context=member.context,
            model=agent_model(live),
        )

    # ---------- 中断 / 拆除 ----------

    def interrupt(self, caller: AgentLoop, target_name: str) -> dict:
        membership = self.membership(caller)
        if membership.role != "lead":
            raise TeamError(
                "only the Team Lead can interrupt teammates", "TEAM_LEAD_REQUIRED"
            )
        state = self._journal.state(membership.root)
        target = resolve_active_member(membership.root, state, target_name)
        if target["id"] == membership.root.id:
            raise TeamError(
                "the Team Lead cannot interrupt itself", "TEAM_INVALID_TARGET"
            )
        live = self._agents.get(target["id"])
        if live is None:
            return {"previousStatus": "inactive"}
        previous = live.status
        self._manager.interrupt(target["id"], {"kind": "ancestor", "agent": caller})
        return {"previousStatus": previous}

    def stop_teammates(self, root: AgentLoop, child_ids: list[str]) -> None:
        """通过 continuation 生命周期 owner 释放选中 teammate Activation。"""
        self._manager.drain_children(root, child_ids)

    # ---------- 内部 ----------

    def _member_name(self, value: str) -> str:
        if not MEMBER_NAME.match(value) or len(value) > MAX_MEMBER_NAME_LENGTH or value == "lead":
            raise TeamError(
                'teammate name must be lower-kebab-case, at most 64 characters, and not "lead"',
                "TEAM_INVALID_MEMBER_NAME",
            )
        return value

    def _fold_descriptor(self, suffix: list[dict]) -> dict | None:
        from ..subagent.descriptor import fold_subagent_descriptor

        return fold_subagent_descriptor(suffix)

    def _subagent_descriptor(self, loop: AgentLoop) -> bool:
        return self._fold_descriptor(list(loop.session.own_events())) is not None