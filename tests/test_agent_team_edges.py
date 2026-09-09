"""Agent Teams（P2-22）边界与载体差异验收：错误码闭集、工具直呼、roster 恢复、
task board 全转移矩阵、mailbox 冷/热投递边缘、activity/lifecycle 簿记。

上游对照：packages/experimental/agent-team/src/*.ts、
packages/experimental/tool-agent-team/src/index.ts。
"""
import asyncio
import inspect
import os
import threading
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.agents import install_agents
from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.session_store import install_sessions
from miniharness.core.system_prompt import install_system_prompt
from miniharness.core.tools import ToolExec, ToolRegistry
from miniharness.seams.agent_team.activity import TeamActivity, TeamWaitResult
from miniharness.seams.agent_team.error import TeamError, error_message
from miniharness.seams.agent_team.journal import TeamJournal
from miniharness.seams.agent_team.lifecycle import TeamRuntimeLifecycle
from miniharness.seams.agent_team.projection import fold_team_state
from miniharness.seams.agent_team.roster import (
    pending_inbox_messages,
    read_persisted_session,
    resolve_active_member,
)
from miniharness.seams.agent_team.task_board import scopes_overlap
from miniharness.seams.agent_team.tools import install_agent_team_tools
from miniharness.seams.agent_team.types import (
    Config,
    CreateTeamTaskRequest,
    SendTeamMessageRequest,
    SpawnTeammateRequest,
    TeamMemberSnapshot,
    TeamMemberView,
    TeamMessageSnapshot,
    TeamMessageSource,
    TeamTaskSnapshot,
    TeamTaskView,
    UpdateTeamTaskRequest,
    generate_message_id,
    is_numeric_task_id,
    team_id,
    team_task_id,
    team_message_id,
)
from miniharness.seams.agent_team.validation import required_text, write_scope
from miniharness.seams.subagent.continuation import SubagentContinuationManager

from tests.test_agent_team import (
    _Harness,
    _parent_loop,
    _create_req,
    _update_req,
)


def _call(svc, name, args, agent):
    reg = agent.tools
    value = reg.resolve(name).execute(args, ToolExec(agent=agent))
    if inspect.isawaitable(value):
        return asyncio.run(value)
    return value


def _worker_loop(svc, member_id, root):
    return svc.roster._manager._get_or_resume(member_id, root)["loop"]


def _member_snapshot(member_id, name="worker", phase="active", provider="fake",
                     description="worker desc", context="fresh"):
    return TeamMemberSnapshot(
        id=member_id, name=name, description=description, provider=provider,
        context=context, phase=phase,
    )


class _FakeLogger:
    def __init__(self):
        self.warnings = []

    def warn(self, *args):
        self.warnings.append(" ".join(str(a) for a in args))


class TestTypesAndValidation(unittest.TestCase):
    def test_brand_helpers_are_identity(self):
        self.assertEqual(team_id("root"), "root")
        self.assertEqual(team_task_id("task-1"), "task-1")
        self.assertEqual(team_message_id("team-message-1"), "team-message-1")

    def test_is_numeric_task_id_edges(self):
        self.assertTrue(is_numeric_task_id("task-1"))
        self.assertTrue(is_numeric_task_id("task-0"))
        self.assertTrue(is_numeric_task_id("task-9007199254740991"))
        self.assertTrue(is_numeric_task_id("task-non-numeric"))
        self.assertFalse(is_numeric_task_id("task-9007199254740992"))

    def test_snapshot_to_dict_optional_fields(self):
        failed = _member_snapshot("c1", phase="failed")
        out = failed.to_dict()
        self.assertNotIn("error", out)
        with_error = TeamMemberSnapshot(
            id="c1", name="w", description="d", provider="p",
            context="fresh", phase="failed", error="boom")
        out = with_error.to_dict()
        self.assertEqual(out["error"], "boom")

        source = TeamMessageSource(kind="team-message", teamId="root",
                                   messageId="m1", senderId="root",
                                   senderName="lead")
        self.assertEqual(source.to_dict()["teamId"], "root")

        view = TeamMemberView(id="m", name="n", role="teammate", status="failed",
                              diagnostics=("boom",), description="d", provider="p",
                              context="fresh", model="fake")
        out = view.to_dict()
        self.assertEqual(out["diagnostics"], ["boom"])
        self.assertEqual(out["description"], "d")
        self.assertEqual(out["provider"], "p")
        self.assertEqual(out["context"], "fresh")
        self.assertEqual(out["model"], "fake")

        task_view = TeamTaskView(id="task-1", revision=2, subject="s",
                                 description="d", status="in_progress",
                                 ready=False, writeScopeWarnings=("w",),
                                 ownerName="worker")
        out = task_view.to_dict()
        self.assertEqual(out["ownerName"], "worker")
        self.assertEqual(out["writeScopeWarnings"], ["w"])

    def test_required_text_and_write_scope(self):
        with self.assertRaises(TeamError) as cm:
            required_text("   ", "subject", 200)
        self.assertEqual(cm.exception.code, "TEAM_INVALID_ARGUMENT")
        with self.assertRaises(TeamError) as cm:
            required_text("x" * 50, "subject", 20)
        self.assertEqual(cm.exception.code, "TEAM_INVALID_ARGUMENT")
        self.assertEqual(required_text("  hi  ", "subject", 200), "hi")

        self.assertEqual(write_scope("a/b/"), "a/b")
        self.assertEqual(write_scope("./a"), "a")
        self.assertEqual(write_scope("a\\b"), "a/b")
        for bad in ("", "/abs", "C:/abs", "a//b", "a/./b", "a/../b", ".."):
            with self.assertRaises(TeamError) as cm:
                write_scope(bad)
            self.assertEqual(cm.exception.code, "TEAM_INVALID_WRITE_SCOPE", bad)

    def test_error_message_fallbacks(self):
        self.assertEqual(error_message("plain"), "plain")
        self.assertEqual(error_message(42), "42")
        self.assertEqual(error_message(ValueError("m")), "m")
        err = TeamError("m", "X")
        self.assertEqual(err.name, "TeamError")
        self.assertEqual(err.code, "X")
        self.assertEqual(str(err), "m")

    def test_pending_inbox_messages_bounds(self):
        events = [
            {"type": "agent/inbox/spliced", "data": {
                "target": "next-turn", "start": 99, "removedCount": 0,
                "inserted": [{"id": "i1"}, {"id": "i2"}],
            }},
            {"type": "agent/inbox/spliced", "data": {
                "target": "next-step", "start": 0, "removedCount": 0,
                "inserted": [{"id": "i3"}],
            }},
            {"type": "other", "data": {}},
        ]
        ids = [m["id"] for m in pending_inbox_messages(events)]
        self.assertEqual(ids, ["i1", "i2", "i3"])

    def test_scopes_overlap(self):
        self.assertTrue(scopes_overlap("a", "a"))
        self.assertTrue(scopes_overlap("a", "a/b"))
        self.assertTrue(scopes_overlap("a/b", "a"))
        self.assertFalse(scopes_overlap("ab", "a"))


class TestServiceConfigAndInstall(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def tearDown(self):
        self.h.cleanup()

    def test_positive_limit_rejects_invalid(self):
        from miniharness.seams.agent_team.service import positive_limit
        with self.assertRaises(TeamError) as cm:
            positive_limit("maxMembers", 0)
        self.assertEqual(cm.exception.code, "TEAM_INVALID_CONFIG")

    def test_install_idempotent_returns_existing(self):
        from miniharness.seams.agent_team import install_agent_team
        again = install_agent_team(self.h.ctx, self.h.manager)
        self.assertIs(again, self.h.service)

    def test_install_falls_back_to_manager_persistence(self):
        from miniharness.seams.agent_team import install_agent_team
        ctx = Context()
        install_sessions(ctx)
        install_agents(ctx)
        manager = SubagentContinuationManager(_parent_loop()[0], _StubPersistence())
        install_agent_team(ctx, manager)
        self.assertIs(ctx.get("sessionPersistence"), None)

    def test_install_requires_services(self):
        from miniharness.seams.agent_team import install_agent_team
        with self.assertRaises(RuntimeError):
            install_agent_team(Context(), object())

    def test_events_without_agent_are_ignored(self):
        self.h.ctx.emit("agent/session-start", {"agent": None})
        self.h.ctx.emit("agent/status", {"agent": None})
        self.h.ctx.emit("agent/created", {"agent": None})

    def test_view_and_direct_service_calls(self):
        svc = self.h.service
        root = self.h.root
        spawned = self.h.spawn(name="worker")
        view = svc.view(root)
        self.assertEqual(len(view.members), 2)
        self.assertEqual(len(view.tasks), 0)
        listed = svc.list_members(root)
        names = {m.name for m in listed}
        self.assertIn("lead", names)
        self.assertIn("worker", names)
        tasks = svc.list_tasks(root)
        self.assertEqual(tasks, [])
        created = svc.create_task(root, _create_req("t", "d"))
        got = svc.get_task(root, created.id)
        self.assertEqual(got.id, created.id)


class _StubPersistence:
    def __init__(self):
        self.declared = set()

    def inspect(self, session_id):
        return {"meta": None, "events": []}


class TestRosterEdges(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def tearDown(self):
        self.h.cleanup()

    def test_try_membership_edges(self):
        svc = self.h.service
        # 未登记的 loop → None
        not_live = AgentLoop(Session("nope"), self.h.root.adapter, ToolRegistry(Context()),
                             Context(), system_prompt="x")
        self.assertIsNone(svc.try_membership(not_live))
        # 同一 registry 带 parent meta、无 descriptor、非团队成员 → Lead 自体
        child = AgentLoop(Session("child"), self.h.root.adapter,
                          self.h.reg, self.h.ctx, system_prompt="x")
        child.session.meta = {"parentSession": "root"}
        child.publish()
        self.assertEqual(svc.try_membership(child).role, "lead")

    def test_member_name_taken_and_limit(self):
        self.h.spawn(name="worker")
        with self.assertRaises(TeamError) as cm:
            self.h.spawn(name="worker")
        self.assertEqual(cm.exception.code, "TEAM_MEMBER_NAME_TAKEN")

    def test_member_limit_reached(self):
        h = _Harness(maxMembers=1)
        self.addCleanup(h.cleanup)
        h.spawn(name="worker")
        with self.assertRaises(TeamError) as cm:
            h.spawn(name="second")
        self.assertEqual(cm.exception.code, "TEAM_MEMBER_LIMIT")

    def test_spawn_non_lead_rejected(self):
        self.h.spawn(name="worker")
        worker = _worker_loop(self.h.service, self.h.service.journal.state(
            self.h.root).members[0].id, self.h.root)
        with self.assertRaises(TeamError) as cm:
            self.h.service.spawn_teammate(worker, _spawn_req("other"))
        self.assertEqual(cm.exception.code, "TEAM_LEAD_REQUIRED")

    def test_spawn_failure_records_failed_member(self):
        svc = self.h.service
        root = self.h.root
        # provider=fork 不可用（mini 仅内建 fake）→ start_continuable 失败
        with self.assertRaises(Exception) as cm:
            svc.spawn_teammate(root, SpawnTeammateRequest(
                name="broken", description="d",
                prompt=({"type": "text", "text": "hi"},),
                context="fork", provider="fork",
            ))
        self.assertIn("fork", str(cm.exception))
        state = svc.journal.state(root)
        self.assertEqual(len(state.members), 1)
        self.assertEqual(state.members[0].phase, "failed")

    def test_spawn_settle_conflict_on_failed(self):
        svc = self.h.service
        root = self.h.root
        original = svc.roster._settle_provisioning
        svc.roster._settle_provisioning = lambda root_, terminal: "failed"
        try:
            with self.assertRaises(TeamError) as cm:
                svc.spawn_teammate(root, _spawn_req("worker"))
        finally:
            svc.roster._settle_provisioning = original
        self.assertEqual(cm.exception.code, "TEAM_PROVISIONING_CONFLICT")

    def test_interrupt_edges(self):
        svc = self.h.service
        root = self.h.root
        spawned = self.h.spawn(name="worker")
        member_id = spawned.member.id
        # 对自己 → TEAM_INVALID_TARGET
        with self.assertRaises(TeamError) as cm:
            svc.interrupt(root, "lead")
        self.assertEqual(cm.exception.code, "TEAM_INVALID_TARGET")
        # 活体目标 → previousStatus
        prev = svc.interrupt(root, "worker")
        self.assertEqual(prev["previousStatus"], "inactive")
        # 非 Lead → TEAM_LEAD_REQUIRED
        other = _parent_loop("stranger")[0]
        with self.assertRaises(TeamError) as cm:
            svc.interrupt(other, "worker")
        self.assertEqual(cm.exception.code, "TEAM_NOT_MEMBER")
        # 未知目标
        with self.assertRaises(TeamError) as cm:
            svc.interrupt(root, "nobody")
        self.assertEqual(cm.exception.code, "TEAM_MEMBER_NOT_FOUND")
        # 不再 live → previousStatus inactive
        svc.roster._manager.drain_children(root, [member_id])
        prev = svc.interrupt(root, "worker")
        self.assertEqual(prev["previousStatus"], "inactive")

    def test_reconcile_provisioning_to_active(self):
        svc = self.h.service
        root = self.h.root
        manager = svc.roster._manager
        child_id = _new_child(manager, root, "boot")
        # 已结算活体 → unit 报错；构造 provisioning-only 前缀再去 durable 结算
        manager.drain_children(root, [child_id])
        svc.journal.append_and_flush(root, "team/member", {
            "version": 2, "teamId": root.id,
            "member": _member_snapshot(child_id, phase="provisioning").to_dict(),
        })
        svc.roster.reconcile_provisioning(root)
        state = svc.journal.state(root)
        self.assertEqual(state.members[0].phase, "active")

    def test_reconcile_provisioning_mismatch_failed(self):
        svc = self.h.service
        root = self.h.root
        manager = svc.roster._manager
        child_id = _new_child(manager, root, "boot")
        manager.drain_children(root, [child_id])
        wrong = _member_snapshot(child_id, phase="provisioning", provider="spawn")
        svc.journal.append_and_flush(root, "team/member", {
            "version": 2, "teamId": root.id, "member": wrong.to_dict(),
        })
        svc.roster.reconcile_provisioning(root)
        state = svc.journal.state(root)
        self.assertEqual(state.members[0].phase, "failed")
        self.assertIn("does not match", state.members[0].error)

    def test_reconcile_provisioning_read_failure_failed(self):
        svc = self.h.service
        root = self.h.root
        manager = svc.roster._manager
        child_id = _new_child(manager, root, "boot")
        manager.drain_children(root, [child_id])
        # 损坏 target 持久化文件 → 读失败 → 回落 failed
        target_file = os.path.join(self.h.tmp.name, "_no-cwd", child_id,
                                   "session.v3.jsonl.zstd")
        self.assertTrue(os.path.exists(target_file))
        with open(target_file, "wb") as fh:
            fh.write(b"not-a-zstd-frame")
        svc.journal.append_and_flush(root, "team/member", {
            "version": 2, "teamId": root.id,
            "member": _member_snapshot(child_id, phase="provisioning").to_dict(),
        })
        svc.roster.reconcile_provisioning(root)
        state = svc.journal.state(root)
        self.assertEqual(state.members[0].phase, "failed")
        self.assertIn("recovery failed", state.members[0].error)

    def test_settle_provisioning_conflicts(self):
        svc = self.h.service
        root = self.h.root
        self.h.spawn(name="worker")
        member = svc.journal.state(root).members[0]
        # 已非 provisioning → 返回既有 phase
        self.assertEqual(svc.roster._settle_provisioning(
            root, _member_snapshot(member.id, phase="active")), "active")
        # 找不到成员 → TEAM_PROVISIONING_CONFLICT
        with self.assertRaises(TeamError) as cm:
            svc.roster._settle_provisioning(
                root, _member_snapshot("ghost", phase="active"))
        self.assertEqual(cm.exception.code, "TEAM_PROVISIONING_CONFLICT")

    def test_live_children_by_root(self):
        svc = self.h.service
        root = self.h.root
        spawned = self.h.spawn(name="worker")
        # 仅已登记的（被 resume/publish）子代理计入
        _worker_loop(svc, spawned.member.id, root)
        teams = svc.roster.live_children_by_root()
        self.assertEqual(teams, {root: [spawned.member.id]})
        svc.roster._manager.drain_children(root, [spawned.member.id])
        self.assertEqual(svc.roster.live_children_by_root(), {})

    def test_read_persisted_session_missing_meta(self):
        info = read_persisted_session(_StubPersistence(), "ghost")
        self.assertEqual(info["header"], {})
        self.assertEqual(info["events"], [])

    def test_agent_model_none(self):
        from miniharness.seams.agent_team.roster import agent_model
        self.assertIsNone(agent_model(None))

    def test_resolve_active_member_nonactive_rejected(self):
        state = fold_team_state([], "root")
        with self.assertRaises(TeamError) as cm:
            resolve_active_member(self.h.root, state, "nobody")
        self.assertEqual(cm.exception.code, "TEAM_MEMBER_NOT_FOUND")


def _new_child(manager, parent, label):
    import uuid
    child_id = str(uuid.uuid4())
    from miniharness.core.session import create_message
    manager.start_continuable(
        label=label,
        prompt=create_message("user", [{"type": "text", "text": "boot"}],
                              {"kind": "user"}),
        parent=parent, child_id=child_id,
        agent_options={"provider": "fake"},
    )
    return child_id


def _spawn_req(name):
    return SpawnTeammateRequest(
        name=name, description="d", prompt=({"type": "text", "text": "hi"},),
        context="fresh", provider="fake",
    )


class TestTaskBoardEdges(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()
        self.svc = self.h.service
        self.root = self.h.root

    def tearDown(self):
        self.h.cleanup()

    def _worker(self, name="worker"):
        spawned = self.h.spawn(name=name)
        return spawned.member, _worker_loop(self.svc, spawned.member.id, self.root)

    def _req(self, **kw):
        return UpdateTeamTaskRequest(taskId=kw["task_id"],
                                     expectedRevision=kw.setdefault("expected", None) or 0,
                                     action=kw["action"],
                                     subject=kw.get("subject"),
                                     description=kw.get("description"),
                                     blockedBy=kw.get("blocked_by"),
                                     writeScopes=kw.get("write_scopes"),
                                     owner=kw.get("owner"))

    def test_update_missing_task_not_found(self):
        with self.assertRaises(TeamError) as cm:
            self.svc.update_task(self.root, self._req(
                task_id="task-999", expected=1, action="claim"))
        self.assertEqual(cm.exception.code, "TEAM_TASK_NOT_FOUND")

    def test_claim_already_owned_by_other(self):
        _, worker = self._worker()
        view = self.svc.create_task(self.root, _create_req("t", "d"))
        self.svc.update_task(worker, self._req(task_id=view.id, expected=1,
                                               action="claim"))
        with self.assertRaises(TeamError) as cm:
            self.svc.update_task(self.root, self._req(task_id=view.id, expected=2,
                                                      action="claim"))
        self.assertEqual(cm.exception.code, "TEAM_TASK_ALREADY_CLAIMED")

    def test_release_only_in_progress(self):
        view = self.svc.create_task(self.root, _create_req("t", "d"))
        with self.assertRaises(TeamError) as cm:
            self.svc.update_task(self.root, self._req(task_id=view.id, expected=1,
                                                      action="release"))
        self.assertEqual(cm.exception.code, "TEAM_TASK_INVALID_TRANSITION")

    def test_release_and_complete_roundtrip(self):
        view = self.svc.create_task(self.root, _create_req("t", "d"))
        claimed = self.svc.update_task(self.root, self._req(task_id=view.id, expected=1,
                                                            action="claim"))
        released = self.svc.update_task(self.root, self._req(task_id=view.id, expected=2,
                                                             action="release"))
        self.assertEqual(released.status, "pending")
        self.assertIsNone(released.ownerName)
        claimed = self.svc.update_task(self.root, self._req(task_id=view.id, expected=3,
                                                            action="claim"))
        done = self.svc.update_task(self.root, self._req(task_id=view.id, expected=4,
                                                         action="complete"))
        self.assertEqual(done.status, "completed")

    def test_edit_requires_a_field(self):
        view = self.svc.create_task(self.root, _create_req("t", "d"))
        with self.assertRaises(TeamError) as cm:
            self.svc.update_task(self.root, self._req(task_id=view.id, expected=1,
                                                      action="edit"))
        self.assertEqual(cm.exception.code, "TEAM_INVALID_ARGUMENT")

    def test_edit_validates_and_applies(self):
        view = self.svc.create_task(self.root, _create_req("t", "d"))
        with self.assertRaises(TeamError) as cm:
            self.svc.update_task(self.root, self._req(
                task_id=view.id, expected=1, action="edit", subject="   "))
        self.assertEqual(cm.exception.code, "TEAM_INVALID_ARGUMENT")
        edited = self.svc.update_task(self.root, self._req(
            task_id=view.id, expected=1, action="edit",
            subject="new title", write_scopes=("src/", "src/")))
        self.assertEqual(edited.subject, "new title")
        self.assertEqual(list(edited.writeScopes), ["src"])
        self.assertEqual(edited.revision, 2)

    def test_set_dependencies_requires_and_applies(self):
        blocker = self.svc.create_task(self.root, _create_req("b", "d"))
        view = self.svc.create_task(self.root, _create_req("t", "d"))
        with self.assertRaises(TeamError) as cm:
            self.svc.update_task(self.root, self._req(task_id=view.id, expected=1,
                                                      action="set_dependencies"))
        self.assertEqual(cm.exception.code, "TEAM_INVALID_ARGUMENT")
        updated = self.svc.update_task(self.root, self._req(
            task_id=view.id, expected=1, action="set_dependencies",
            blocked_by=(blocker.id,)))
        self.assertEqual(list(updated.blockedBy), [blocker.id])
        self.assertFalse(updated.ready)

    def test_reopen_requires_completed(self):
        view = self.svc.create_task(self.root, _create_req("t", "d"))
        with self.assertRaises(TeamError) as cm:
            self.svc.update_task(self.root, self._req(task_id=view.id, expected=1,
                                                      action="reopen"))
        self.assertEqual(cm.exception.code, "TEAM_TASK_INVALID_TRANSITION")

    def test_reopen_completed(self):
        view = self.svc.create_task(self.root, _create_req("t", "d"))
        claimed = self.svc.update_task(self.root, self._req(task_id=view.id, expected=1,
                                                            action="claim"))
        self.svc.update_task(self.root, self._req(task_id=view.id, expected=2,
                                                  action="complete"))
        reopened = self.svc.update_task(self.root, self._req(task_id=view.id, expected=3,
                                                             action="reopen"))
        self.assertEqual(reopened.status, "pending")
        self.assertIsNone(reopened.ownerName)

    def test_reassign_transitions(self):
        member, worker = self._worker()
        view = self.svc.create_task(self.root, _create_req("t", "d"))
        reassigned = self.svc.update_task(self.root, self._req(
            task_id=view.id, expected=1, action="reassign", owner="worker"))
        self.assertEqual(reassigned.status, "in_progress")
        self.assertEqual(reassigned.ownerName, "worker")
        # 未指定 owner → 解除持有
        freed = self.svc.update_task(self.root, self._req(
            task_id=view.id, expected=2, action="reassign", owner=""))
        self.assertIsNone(freed.ownerName)
        self.assertEqual(freed.status, "pending")
        # 非 Lead 成员 reassign → TEAM_LEAD_REQUIRED
        with self.assertRaises(TeamError) as cm:
            self.svc.update_task(worker, self._req(
                task_id=view.id, expected=3, action="reassign", owner="worker"))
        self.assertEqual(cm.exception.code, "TEAM_LEAD_REQUIRED")

    def test_reassign_blocked_task(self):
        blocker = self.svc.create_task(self.root, _create_req("b", "d"))
        view = self.svc.create_task(self.root, _create_req(
            "t", "d", blocked_by=(blocker.id,)))
        with self.assertRaises(TeamError) as cm:
            self.svc.update_task(self.root, self._req(
                task_id=view.id, expected=1, action="reassign", owner="worker"))
        self.assertEqual(cm.exception.code, "TEAM_TASK_BLOCKED")

    def test_delete_with_dependents_blocked(self):
        a = self.svc.create_task(self.root, _create_req("a", "d"))
        b = self.svc.create_task(self.root, _create_req("b", "d", blocked_by=(a.id,)))
        with self.assertRaises(TeamError) as cm:
            self.svc.update_task(self.root, self._req(task_id=a.id, expected=1,
                                                      action="delete"))
        self.assertEqual(cm.exception.code, "TEAM_TASK_HAS_DEPENDENTS")
        deleted = self.svc.update_task(self.root, self._req(task_id=b.id, expected=1,
                                                            action="delete"))
        self.assertEqual(deleted.status, "deleted")
        # list 排除已删除
        listed = self.svc.list_tasks(self.root)
        self.assertEqual([t.id for t in listed], [a.id])

    def test_task_limit_reached(self):
        h = _Harness(maxTasks=1)
        self.addCleanup(h.cleanup)
        h.service.create_task(h.root, _create_req("t", "d"))
        with self.assertRaises(TeamError) as cm:
            h.service.create_task(h.root, _create_req("t2", "d"))
        self.assertEqual(cm.exception.code, "TEAM_TASK_LIMIT")

    def test_dependency_validation_errors(self):
        a = self.svc.create_task(self.root, _create_req("a", "d"))
        # 重复 blocker
        with self.assertRaises(TeamError) as cm:
            self.svc.create_task(self.root, _create_req("x", "d", blocked_by=(a.id, a.id)))
        self.assertEqual(cm.exception.code, "TEAM_INVALID_ARGUMENT")
        # 缺失 blocker
        with self.assertRaises(TeamError) as cm:
            self.svc.create_task(self.root, _create_req("x", "d",
                                                        blocked_by=("task-999",)))
        self.assertEqual(cm.exception.code, "TEAM_TASK_NOT_FOUND")
        # 自引用（set_dependencies 含自身 id）
        with self.assertRaises(TeamError) as cm:
            self.svc.update_task(self.root, self._req(
                task_id=a.id, expected=1, action="set_dependencies",
                blocked_by=(a.id,)))
        self.assertEqual(cm.exception.code, "TEAM_TASK_DEPENDENCY_CYCLE")

    def test_dependency_cycle_across_tasks(self):
        a = self.svc.create_task(self.root, _create_req("a", "d"))
        b = self.svc.create_task(self.root, _create_req("b", "d", blocked_by=(a.id,)))
        with self.assertRaises(TeamError) as cm:
            self.svc.update_task(self.root, self._req(
                task_id=a.id, expected=1, action="set_dependencies",
                blocked_by=(b.id,)))
        self.assertEqual(cm.exception.code, "TEAM_TASK_DEPENDENCY_CYCLE")

    def test_write_scope_warnings(self):
        from miniharness.seams.agent_team.types import CreateTeamTaskRequest
        a = self.svc.create_task(self.root, CreateTeamTaskRequest(
            subject="a", description="d", blockedBy=(), writeScopes=("src/",)))
        self.svc.update_task(self.root, self._req(task_id=a.id, expected=1,
                                                  action="claim"))
        b = self.svc.create_task(self.root, CreateTeamTaskRequest(
            subject="b", description="d", blockedBy=(), writeScopes=("src",)))
        self.assertIn("write scopes overlap with task-1", b.writeScopeWarnings)

    def test_owner_name_for_unknown_member(self):
        self.svc.journal.append_and_flush(self.root, "team/task", {
            "version": 2, "teamId": self.root.id,
            "task": TeamTaskSnapshot(
                id="task-1", revision=1, subject="s", description="d",
                status="in_progress", ownerId="unknown-owner").to_dict(),
        })
        view = self.svc.get_task(self.root, "task-1")
        self.assertIsNone(view.ownerName)


class TestMailboxEdges(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()
        self.svc = self.h.service
        self.root = self.h.root

    def tearDown(self):
        self.h.cleanup()

    def test_send_to_lead_accepted(self):
        self.h.spawn(name="worker")
        member = self.svc.journal.state(self.root).members[0]
        worker = _worker_loop(self.svc, member.id, self.root)
        sent = self.svc.send_message(worker, SendTeamMessageRequest(
            target="lead", content=({"type": "text", "text": "report"},)))
        self.assertEqual(sent.status, "accepted")
        # Lead 会话记录了 letter source
        recorded = any(
            e.get("data", {}).get("source", {}).get("kind") == "team-message"
            for e in self.root.session.own_events()
            if e["type"] == "user/message"
        )
        self.assertTrue(recorded)

    def test_redispatch_already_delivered_persisted(self):
        spawned = self.h.spawn(name="worker")
        sent = self.svc.send_message(self.root, SendTeamMessageRequest(
            target="worker", content=({"type": "text", "text": "hi"},)))
        member_id = spawned.member.id
        self.svc.roster._manager.drain_children(self.root, [member_id])
        accepted = self.svc.mailbox._try_dispatch(self.root, _queued_message(
            sent.messageId, worker_id=member_id))
        self.assertTrue(accepted)

    def test_dispatch_queued_when_target_unreadable(self):
        spawned = self.h.spawn(name="worker")
        member_id = spawned.member.id
        self.svc.roster._manager.drain_children(self.root, [member_id])
        # 损坏 target 持久化文件 → 读失败 → 保持 queued + 告警
        target_file = os.path.join(self.h.tmp.name, "_no-cwd", member_id,
                                   "session.v3.jsonl.zstd")
        self.assertTrue(os.path.exists(target_file))
        with open(target_file, "wb") as fh:
            fh.write(b"not-a-zstd-frame")
        logger = _FakeLogger()
        self.h.ctx.get("agents").logger = logger
        sent = self.svc.mailbox._enqueue(
            self.root, self.root, "lead", "worker",
            [{"type": "text", "text": "hi"}])
        accepted = self.svc.mailbox._try_dispatch(self.root, sent)
        self.assertFalse(accepted)
        self.assertTrue(any("cannot read Team message target" in w
                            for w in logger.warnings))

    def test_dispatch_steer_failure_keeps_queued(self):
        self.h.spawn(name="worker")
        sent = self.svc.mailbox._enqueue(
            self.root, self.root, "lead", "worker",
            [{"type": "text", "text": "hi"}])
        origin = self.svc.mailbox._manager.steer_host_subagent
        self.svc.mailbox._manager.steer_host_subagent = (
            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        try:
            logger = _FakeLogger()
            self.h.ctx.get("agents").logger = logger
            accepted = self.svc.mailbox._try_dispatch(self.root, sent)
        finally:
            self.svc.mailbox._manager.steer_host_subagent = origin
        self.assertFalse(accepted)
        self.assertTrue(any("remains queued" in w for w in logger.warnings))

    def test_send_after_dispose_reverts_to_queued(self):
        h = _Harness()
        self.addCleanup(h.cleanup)
        h.spawn(name="worker")
        mailbox = h.service.mailbox
        message = mailbox._enqueue(h.root, h.root, "lead", "worker",
                                   [{"type": "text", "text": "hi"}])
        mailbox._lifecycle.settle()
        self.assertFalse(mailbox._try_dispatch(h.root, message))

    def test_recover_for_nonmember_noop(self):
        stranger = _parent_loop("stranger")[0]
        self.svc.mailbox.recover_for(stranger)

    def test_mailbox_recover_redelivers_pending(self):
        self.h.spawn(name="worker")
        pending = self.svc.mailbox._enqueue(
            self.root, self.root, "lead", "worker",
            [{"type": "text", "text": "hi"}])
        state = self.svc.journal.state(self.root)
        self.assertNotIn(pending.id, state.delivered)
        self.svc.mailbox.recover_for(self.root)
        state = self.svc.journal.state(self.root)
        self.assertIn(pending.id, state.delivered)

    def test_observe_ack_failure_logged(self):
        service = self.h.service
        bogus = Session("bogus")
        logger = _FakeLogger()
        self.h.ctx.get("agents").logger = logger
        self.svc.mailbox.observe_session_event(bogus, {
            "type": "user/message",
            "data": {"source": {"kind": "team-message", "teamId": "root2"}},
        })
        self.svc.mailbox.observe_session_event(bogus, {
            "type": "user/message",
            "data": {"source": {}},
        })


def _queued_message(message_id, worker_id):
    return TeamMessageSnapshot(id=message_id, senderId="root", senderName="lead",
                               targetId=worker_id,
                               content=({"type": "text", "text": "hi"},))


class TestActivityAndLifecycle(unittest.TestCase):
    def test_wait_join_wake(self):
        act = TeamActivity()
        result_holder = {}
        def waiter():
            result_holder["result"] = act.wait_for("root", 10_000)
        thread = threading.Thread(target=waiter)
        thread.start()
        threading.Event().wait(0.05)
        act.wake("root")
        thread.join(timeout=5)
        self.assertFalse(result_holder["result"].timed_out)

    def test_to_dict_and_discard(self):
        result = TeamWaitResult(timed_out=True)
        self.assertEqual(result.to_dict(), {"timedOut": True})
        act = TeamActivity()
        act._discard("nope", threading.Event())
        act._discard("root", _never_event())

    def test_lifecycle_track(self):
        life = TeamRuntimeLifecycle(5000)
        self.assertTrue(life.signal_aborted is False)
        pending = []
        class Done:
            def __init__(self):
                self.cb = None
            def add_done_callback(self, cb):
                self.cb = cb
            def finish(self):
                self.cb(None)
        d = Done()
        life.track(d)
        self.assertEqual(life.pending_operations(), (d,))
        d.finish()
        self.assertEqual(life.pending_operations(), ())
        life.track(lambda: None)
        self.assertEqual(len(life.pending_operations()), 1)
        life.settle()
        with self.assertRaises(TeamError) as cm:
            life.assert_admitting()
        self.assertEqual(cm.exception.code, "TEAM_DISPOSED")

    def test_journal_rejects_non_team_event(self):
        class _Sessions:
            def flush(self, session):
                pass
        journal = TeamJournal(_Sessions(), lambda root: None)
        from miniharness.core.agent_loop.agent import AgentLoop
        from miniharness.llm import FakeLlmAdapter
        loop = AgentLoop(Session("root"), FakeLlmAdapter(), ToolRegistry(Context()),
                         Context(), system_prompt="x")
        with self.assertRaises(ValueError):
            journal.append_and_flush(loop, "nope", {})


def _never_event():
    ev = threading.Event()
    ev.clear()
    return ev


class TestToolsExec(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()
        self.svc = self.h.service
        self.root = self.h.root

    def tearDown(self):
        self.h.cleanup()

    def test_spawn_teammate_tool(self):
        out = _call(self.svc, "spawn_teammate", {
            "name": "worker", "description": "d", "prompt": "do work",
            "context": "fresh",
        }, self.root)
        self.assertEqual(out["member"]["name"], "worker")
        self.assertEqual(out["member"]["role"], "teammate")
        self.assertEqual(out["member"]["provider"], "fake")
        self.assertIn("status", out["member"])
        self.assertIn("description", out["member"])
        self.assertIn("context", out["member"])

    def test_spawn_teammate_tool_fork_provider_fails(self):
        # mini 仅内建 fake；fork 触发 provider 分支并落入失败结算
        with self.assertRaises(Exception):
            _call(self.svc, "spawn_teammate", {
                "name": "fb", "description": "d", "prompt": "do work",
                "context": "fork",
            }, self.root)

    def test_send_message_tool(self):
        self.h.spawn(name="worker")
        out = _call(self.svc, "send_message", {
            "target": "worker", "message": "hey",
        }, self.root)
        self.assertTrue(out["messageId"].startswith("team-message-"))
        self.assertEqual(out["status"], "accepted")

    def test_list_agents_tool(self):
        self.h.spawn(name="worker")
        out = _call(self.svc, "list_agents", {}, self.root)
        names = {m["name"] for m in out}
        self.assertIn("lead", names)
        self.assertIn("worker", names)

    def test_wait_agent_no_active_peer(self):
        self.h.spawn(name="worker")
        out = _call(self.svc, "wait_agent", {}, self.root)
        self.assertEqual(out["noProgress"]["reason"], "no-active-peer")

    def test_wait_agent_invalid_timeout(self):
        with self.assertRaises(TeamError) as cm:
            _call(self.svc, "wait_agent", {"timeout_ms": 5000}, self.root)
        self.assertEqual(cm.exception.code, "TEAM_INVALID_ARGUMENT")

    def test_interrupt_agent_tool(self):
        self.h.spawn(name="worker")
        out = _call(self.svc, "interrupt_agent", {"target": "worker"}, self.root)
        self.assertIn("previousStatus", out)

    def test_team_task_tools_roundtrip(self):
        created = _call(self.svc, "team_task_create", {
            "subject": "t", "description": "d", "blocked_by": [], "write_scopes": ["src/"],
        }, self.root)
        task_id = created["id"]
        listed = _call(self.svc, "team_task_list", {"status": "pending"}, self.root)
        self.assertEqual([t["id"] for t in listed["tasks"]], [task_id])
        got = _call(self.svc, "team_task_get", {"task_id": task_id}, self.root)
        self.assertEqual(got["revision"], 1)
        updated = _call(self.svc, "team_task_update", {
            "task_id": task_id, "expected_revision": 1, "action": "claim",
        }, self.root)
        self.assertEqual(updated["status"], "in_progress")

    def test_team_task_list_validation(self):
        with self.assertRaises(RuntimeError):
            _call(self.svc, "team_task_list", {"cursor": -1}, self.root)
        with self.assertRaises(RuntimeError):
            _call(self.svc, "team_task_list", {"limit": 0}, self.root)

    def test_team_task_list_pagination(self):
        for i in range(3):
            _call(self.svc, "team_task_create", {
                "subject": f"t{i}", "description": "d",
            }, self.root)
        page = _call(self.svc, "team_task_list", {"limit": 2}, self.root)
        self.assertEqual(len(page["tasks"]), 2)
        self.assertEqual(page["nextCursor"], 2)
        page2 = _call(self.svc, "team_task_list", {"limit": 2, "cursor": 2}, self.root)
        self.assertEqual(len(page2["tasks"]), 1)
        self.assertNotIn("nextCursor", page2)

    def test_team_task_update_missing_required(self):
        created = _call(self.svc, "team_task_create", {
            "subject": "t", "description": "d",
        }, self.root)
        with self.assertRaises(TeamError):
            _call(self.svc, "team_task_update", {
                "task_id": created["id"], "expected_revision": 1, "action": "edit",
                "blocked_by": [], "subject": None,
            }, self.root)

    def test_tool_without_agent_rejected(self):
        with self.assertRaises(RuntimeError):
            asyncio.run(self.root.tools.resolve("list_agents").execute({}, ToolExec()))

    def test_policy_section_registration(self):
        captured = {}
        class StubPrompt:
            def section(self, name, order, text):
                captured[name] = text
        from miniharness.seams.agent_team.tools import POLICY
        ctx = Context()
        ctx.provide("systemPrompt", StubPrompt())
        reg = ToolRegistry(ctx)
        install_agent_team_tools(ctx, reg, self.svc)
        fn = captured["team:policy"]
        self.assertEqual(fn({"agent": None}), POLICY)
        self.assertIn("Your Team role is lead", fn({"agent": self.root}))
        stranger = _parent_loop("stranger")[0]
        self.assertEqual(fn({"agent": stranger}), POLICY)

    def test_install_tools_into_agent_ctx_with_system_prompt(self):
        from miniharness.seams.agent_team.tools import POLICY
        self.h.spawn(name="worker")
        member = self.svc.journal.state(self.root).members[0]
        install_system_prompt(self.h.ctx)
        worker = _worker_loop(self.svc, member.id, self.root)
        svcp = self.h.ctx.get("systemPrompt")
        self.assertIsNotNone(svcp)


if __name__ == "__main__":
    unittest.main()