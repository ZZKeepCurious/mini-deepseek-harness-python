"""Agent Teams（P2-22）验收：投影折叠、task board CAS、mailbox 投递/ack、
TeamService 门面、错误码闭集、9 工具装配。

上游对照：packages/experimental/agent-team/src/*.ts、
packages/experimental/tool-agent-team/src/index.ts。

运行：python -m unittest tests.test_agent_team -v
"""
import tempfile
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.agents import install_agents
from miniharness.core.scope import Context
from miniharness.core.session import Session, create_message, text_block
from miniharness.core.session.persistence import JsonlPersistence
from miniharness.core.session_store import install_sessions
from miniharness.core.tools import ToolRegistry
from miniharness.llm import FakeLlmAdapter
from miniharness.seams.agent_team import install_agent_team
from miniharness.seams.agent_team.activity import TeamActivity
from miniharness.seams.agent_team.error import TeamError
from miniharness.seams.agent_team.journal import TeamJournal
from miniharness.seams.agent_team.lifecycle import TeamRuntimeLifecycle
from miniharness.seams.agent_team.projection import (
    TeamState,
    fold_team_state,
)
from miniharness.seams.agent_team.roster import read_persisted_session
from miniharness.seams.agent_team.service import TeamService
from miniharness.seams.agent_team.task_board import TeamTaskBoard
from miniharness.seams.agent_team.tools import install_agent_team_tools
from miniharness.seams.agent_team.types import (
    Config,
    SendTeamMessageRequest,
    SpawnTeammateRequest,
    UpdateTeamTaskRequest,
    TeamMessageSnapshot,
    TeamMemberSnapshot,
    TeamTaskSnapshot,
)
from miniharness.seams.subagent.continuation import SubagentContinuationManager


def _parent_loop(session_id="root"):
    ctx = Context()
    install_sessions(ctx)
    install_agents(ctx)
    reg = ToolRegistry(ctx)
    loop = AgentLoop(Session(session_id), FakeLlmAdapter(final_text="父响应"),
                     reg, ctx, system_prompt="你是 Team Lead。")
    loop.publish()
    return loop, ctx, reg


class _Harness:
    def __init__(self, **cfg):
        self.tmp = tempfile.TemporaryDirectory()
        self.persistence = JsonlPersistence(self.tmp.name)
        self.root, self.ctx, self.reg = _parent_loop()
        self.ctx.provide("sessionPersistence", self.persistence)
        # 镜像真实组合（protocol/acp.py `_install_persistence_hook`）：装配层把
        # 顶层（Lead）会话逐条持久化（session/event）、session/flush 落盘——
        # 否则团队事件不落盘，重启重建无从谈起。continuation 的子会话由其自身
        # _persist_delta 持久化（同款分区）——hook 只认 root 会话，避免子会话
        # 被双写造成 seq 间隙。
        def on_event(payload):
            session = payload.get("session") if isinstance(payload, dict) else payload
            event = payload.get("event") if isinstance(payload, dict) else None
            if getattr(session, "session_id", None) != self.root.session.session_id:
                return
            # 发布时 event 是 mappingproxy（非 dict 子类）——鸭子检查（acp.py 同款）
            if getattr(event, "get", None) is not None and event.get("type"):
                self.persistence.append(self.root.session.session_id, dict(event))

        def on_flush(payload=None):
            self.persistence.flush()

        self.ctx.on("session/event", on_event)
        self.ctx.on("session/flush", on_flush)
        self.manager = SubagentContinuationManager(self.root, self.persistence)
        self.service = install_agent_team(
            self.ctx, self.manager, Config(disposalTimeoutMs=5000, **cfg)
        )

    def cleanup(self):
        self.tmp.cleanup()

    def spawn(self, name="worker", prompt="do the work", context="fresh"):
        return self.service.spawn_teammate(self.root, SpawnTeammateRequest(
            name=name, description="worker desc", prompt=({"type": "text", "text": prompt},),
            context=context, provider="fake",
        ))


class TestProjection(unittest.TestCase):
    def test_fold_team_state_from_event_log(self):
        root_id = "root"
        events = [
            _member_event(root_id, TeamMemberSnapshot(
                id="c1", name="w", description="d", provider="in-process",
                context="fresh", phase="provisioning",
            )),
            _member_event(root_id, TeamMemberSnapshot(
                id="c1", name="w", description="d", provider="in-process",
                context="fresh", phase="active",
            )),
            _task_event(root_id, TeamTaskSnapshot(
                id="task-1", revision=1, subject="s", description="d",
                status="pending",
            )),
            {
                "type": "team/message/queued",
                "data": {
                    "version": 2, "teamId": root_id,
                    "message": {
                        "id": "team-message-1", "senderId": "root",
                        "senderName": "lead", "targetId": "c1",
                        "content": [{"type": "text", "text": "hi"}],
                    },
                },
            },
        ]
        state = fold_team_state(events, root_id)
        self.assertEqual(len(state.members), 1)
        self.assertEqual(state.members[0].phase, "active")
        self.assertEqual(state.tasks[0].id, "task-1")
        self.assertEqual(state.tasks[0].status, "pending")
        self.assertEqual(state.next_task_number, 2)
        self.assertEqual(state.messages[0].id, "team-message-1")
        self.assertNotIn("team-message-1", state.delivered)

    def test_next_task_number_advances_from_task_ids(self):
        events = [
            _task_event("root", TeamTaskSnapshot(id="task-1", revision=1,
                                                 subject="s", description="d", status="pending")),
            _task_event("root", TeamTaskSnapshot(id="task-5", revision=1,
                                                 subject="s", description="d", status="pending")),
        ]
        state = fold_team_state(events, "root")
        self.assertEqual(state.next_task_number, 6)

    def test_delivered_requires_queued_target_match(self):
        state = TeamState(id="root")
        with self.assertRaises(ValueError):
            fold_team_state([{
                "type": "team/message/delivered",
                "data": {"version": 2, "teamId": "root",
                         "messageId": "team-message-1", "targetId": "c1"},
            }], "root")

    def test_invalid_version_fails_closed(self):
        events = [{
            "type": "team/member",
            "data": {"version": 1, "teamId": "root", "member": {
                "id": "c1", "name": "w", "description": "d", "provider": "p",
                "context": "fresh", "phase": "active",
            }},
        }]
        with self.assertRaises(ValueError):
            fold_team_state(events, "root")


def _member_event(team_id, member):
    return {"type": "team/member", "data": {
        "version": 2, "teamId": team_id, "member": member.to_dict(),
    }}


def _task_event(team_id, task):
    return {"type": "team/task", "data": {
        "version": 2, "teamId": team_id, "task": task.to_dict(),
    }}


class TestTaskBoard(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def tearDown(self):
        self.h.cleanup()

    def _membership(self):
        return self.h.service.membership(self.h.root)

    def test_create_list_get_and_cas_claim_complete(self):
        svc = self.h.service
        root = self.h.root
        view = svc.create_task(root, _create_req("task a", "do"))
        self.assertEqual(view.revision, 1)
        self.assertEqual(view.status, "pending")
        self.assertEqual(view.ready, True)
        # CAS claim with correct revision
        claimed = svc.update_task(root, _update_req(view.id, 1, "claim"))
        self.assertEqual(claimed.status, "in_progress")
        self.assertEqual(claimed.ownerName, "lead")
        self.assertEqual(claimed.revision, 2)
        # complete
        done = svc.update_task(root, _update_req(view.id, 2, "complete"))
        self.assertEqual(done.status, "completed")
        self.assertEqual(done.revision, 3)

    def test_cas_stale_revision_rejected(self):
        svc = self.h.service
        root = self.h.root
        view = svc.create_task(root, _create_req("task a", "do"))
        with self.assertRaises(TeamError) as cm:
            svc.update_task(root, _update_req(view.id, 99, "claim"))
        self.assertEqual(cm.exception.code, "TEAM_TASK_STALE_REVISION")

    def test_claim_blocked_when_blocker_not_completed(self):
        svc = self.h.service
        root = self.h.root
        blocker = svc.create_task(root, _create_req("blocker", "do"))
        task = svc.create_task(root, _create_req("dep", "dep", blocked_by=(blocker.id,)))
        self.assertFalse(task.ready)
        with self.assertRaises(TeamError) as cm:
            svc.update_task(root, _update_req(task.id, 1, "claim"))
        self.assertEqual(cm.exception.code, "TEAM_TASK_BLOCKED")

    def test_deleted_task_cannot_claim(self):
        svc = self.h.service
        root = self.h.root
        view = svc.create_task(root, _create_req("task", "do"))
        svc.update_task(root, _update_req(view.id, 1, "delete"))
        with self.assertRaises(TeamError) as cm:
            svc.update_task(root, _update_req(view.id, 2, "claim"))
        self.assertEqual(cm.exception.code, "TEAM_TASK_DELETED")

    def test_reassign_requires_lead(self):
        svc = self.h.service
        root = self.h.root
        self.h.spawn(name="worker")
        view = svc.create_task(root, _create_req("task", "do"))
        # 非 Lead（workers 无 membership/同名 root 不存在）→ TEAM_NOT_MEMBER
        other = _parent_loop("stranger")[0]
        with self.assertRaises(TeamError) as cm:
            svc.update_task(other, _update_req(view.id, 1, "reassign", owner="worker"))
        self.assertEqual(cm.exception.code, "TEAM_NOT_MEMBER")


def _create_req(subject, description, blocked_by=()):
    from miniharness.seams.agent_team.types import CreateTeamTaskRequest
    return CreateTeamTaskRequest(subject=subject, description=description,
                                 blockedBy=blocked_by, writeScopes=())


def _update_req(task_id, revision, action, owner=None):
    return UpdateTeamTaskRequest(taskId=task_id, expectedRevision=revision,
                                 action=action, owner=owner)


class TestMailbox(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()
        self.svc = self.h.service

    def tearDown(self):
        self.h.cleanup()

    def _fresh(self, **cfg):
        h = _Harness(**cfg)
        self.addCleanup(h.cleanup)
        return h

    def test_delivered_ack_written_to_lead_log(self):
        spawned = self.h.spawn(name="worker", prompt="start")
        member = spawned.member
        sent = self.svc.send_message(
            self.h.root,
            SendTeamMessageRequest(target="worker", content=({"type": "text", "text": "hi"},)),
        )
        self.assertEqual(sent.status, "accepted")
        state = self.h.service.journal.state(self.h.root)
        self.assertEqual(len(state.delivered), 1)
        self.assertEqual(state.delivered[0], sent.messageId)
        # 交付帧已入 target 日志（前缀文本 + team-message source）
        target = self.svc.roster._manager._get_or_resume(member.id, self.h.root)["loop"]
        frames = [
            e for e in target.session.own_events()
            if e["type"] == "user/message"
            and (e.get("data") or {}).get("source", {}).get("kind") == "team-message"
        ]
        self.assertEqual(len(frames), 1)
        text = frames[0]["data"]["content"][0]["text"]
        self.assertTrue(text.startswith(f"Team message {sent.messageId} from lead:"))

    def test_restart_reconstructs_team_from_persistence(self):
        h = _Harness()
        self.addCleanup(h.cleanup)
        h.spawn(name="worker", prompt="start")
        h.service.send_message(h.root, SendTeamMessageRequest(
            target="worker", content=({"type": "text", "text": "m1"},)))
        # 模拟重启：同一 persistence 目录，先把 Lead 会话从磁盘物化再装配
        stored = read_persisted_session(h.persistence, "root")
        seed = Session("root", seed=stored["events"], meta=stored["header"],
                       inherited_event_count=stored["inheritedEventCount"],
                       mode="restore")
        ctx2 = Context()
        install_sessions(ctx2)
        install_agents(ctx2)
        reg2 = ToolRegistry(ctx2)
        root2 = AgentLoop(seed, FakeLlmAdapter(final_text="父响应"),
                          reg2, ctx2, system_prompt="你是 Team Lead。")
        root2.publish()
        manager2 = SubagentContinuationManager(root2, h.persistence)
        install_agent_team(ctx2, manager2)
        state = ctx2.get("agentTeams").journal.state(root2)
        self.assertEqual(len(state.members), 1)
        self.assertEqual(state.members[0].name, "worker")
        self.assertEqual(len(state.messages), 1)
        self.assertEqual(len(state.delivered), 1)

    def test_mailbox_full_rejected(self):
        h = self._fresh(maxPendingMessagesPerMember=1)
        h.spawn(name="worker", prompt="start")
        mailbox = h.service.mailbox
        membership = h.service.membership(h.root)
        root = membership.root
        first = mailbox._enqueue(
            root, h.root, "lead", "worker", [{"type": "text", "text": "m1"}])
        self.assertTrue(first.id.startswith("team-message-"))
        with self.assertRaises(TeamError) as cm:
            mailbox._enqueue(
                root, h.root, "lead", "worker", [{"type": "text", "text": "m2"}])
        self.assertEqual(cm.exception.code, "TEAM_MAILBOX_FULL")

    def test_message_too_large_rejected(self):
        h = self._fresh(maxMessageBytes=64)
        h.spawn(name="worker", prompt="start")
        with self.assertRaises(TeamError) as cm:
            h.service.send_message(h.root, SendTeamMessageRequest(
                target="worker", content=({"type": "text", "text": "x" * 200},)))
        self.assertEqual(cm.exception.code, "TEAM_MESSAGE_TOO_LARGE")

    def test_send_to_teammate_is_accepted_and_durable(self):
        spawned = self.h.spawn(name="worker", prompt="start")
        member = spawned.member
        sent = self.svc.send_message(
            self.h.root,
            SendTeamMessageRequest(target="worker", content=({"type": "text", "text": "hi"},)),
        )
        self.assertEqual(sent.status, "accepted")
        self.assertTrue(sent.messageId.startswith("team-message-"))
        # target session recorded the message with team-message source
        target_loop = self.svc.roster._manager._get_or_resume(
            member.id, self.h.root)["loop"]
        recorded = any(
            (e.get("data") or {}).get("source", {}).get("kind") == "team-message"
            for e in target_loop.session.own_events()
            if e["type"] == "user/message"
        )
        self.assertTrue(recorded)

    def test_self_message_rejected(self):
        with self.assertRaises(TeamError) as cm:
            self.svc.send_message(
                self.h.root,
                SendTeamMessageRequest(target="lead",
                                       content=({"type": "text", "text": "hi"},)),
            )
        self.assertEqual(cm.exception.code, "TEAM_SELF_MESSAGE")

    def test_unknown_target_raises_member_not_found(self):
        with self.assertRaises(TeamError) as cm:
            self.svc.send_message(
                self.h.root,
                SendTeamMessageRequest(target="nobody",
                                       content=({"type": "text", "text": "hi"},)),
            )
        self.assertEqual(cm.exception.code, "TEAM_MEMBER_NOT_FOUND")


class TestActivity(unittest.TestCase):
    def test_wait_timeout_range_validated(self):
        act = TeamActivity()
        with self.assertRaises(TeamError) as cm:
            act.wait_for("root", 100)
        self.assertEqual(cm.exception.code, "TEAM_INVALID_ARGUMENT")

    def test_wake_preserves_waiter_once(self):
        act = TeamActivity()
        result = act.wait_for("root", 10_000)
        self.assertTrue(result.timed_out)


class TestServiceAndTools(unittest.TestCase):
    def setUp(self):
        self.h = _Harness()

    def tearDown(self):
        self.h.cleanup()

    def test_install_provides_service_and_installs_tools_for_lead(self):
        svc = self.h.ctx.get("agentTeams")
        self.assertIs(svc, self.h.service)
        names = self.h.reg.names()
        for expected in ("spawn_teammate", "send_message", "list_agents",
                         "wait_agent", "interrupt_agent", "team_task_create",
                         "team_task_list", "team_task_get", "team_task_update"):
            self.assertIn(expected, names)

    def test_team_task_update_rejects_invalid_transition(self):
        svc = self.h.service
        root = self.h.root
        view = svc.create_task(root, _create_req("t", "do"))
        with self.assertRaises(TeamError) as cm:
            svc.update_task(root, _update_req(view.id, 1, "complete"))
        self.assertEqual(cm.exception.code, "TEAM_TASK_INVALID_TRANSITION")

    def test_member_name_invalid_rejected(self):
        with self.assertRaises(TeamError) as cm:
            self.h.spawn(name="Bad Name")
        self.assertEqual(cm.exception.code, "TEAM_INVALID_MEMBER_NAME")

    def test_disposed_service_rejects_admission(self):
        self.h.service.lifecycle.settle()
        with self.assertRaises(TeamError) as cm:
            self.h.spawn(name="later")
        self.assertEqual(cm.exception.code, "TEAM_DISPOSED")


if __name__ == "__main__":
    unittest.main()