"""P2-26 其余联动缺口：mcp project_content、authorization commit、schedule
归档准入、hooks source kind（对齐 baseline item 26）。"""
import os
import unittest

from miniharness.core.scope import Context
from miniharness.core.tools import ToolRegistry, ToolExec


class TestMcpProjectContent(unittest.TestCase):
    """mcp 工具图片投影改挂 project_content（政策前）而非 finalize_content。"""

    def _tool(self):
        from miniharness.mcp.tools import create_mcp_tool_definition
        async def call(_args, _exec):
            return {"content": [
                {"type": "text", "text": "hello"},
                {"type": "image", "data": "base64img"},
            ]}
        return create_mcp_tool_definition(Context(name="mcp"), {
            "name": "mcp-test", "rawName": "mcp_test", "description": "d",
            "call": call})

    def test_hook_field_is_project_content(self):
        tool = self._tool()
        self.assertIsNotNone(tool.project_content)
        self.assertIsNone(tool.finalize_content)

    def test_project_content_applies_image_projection(self):
        from miniharness.mcp.tools import prepare_image_projection
        tool = self._tool()
        exec_ = ToolExec()
        import asyncio
        value = asyncio.run(tool.execute({}, exec_))
        self.assertIsNone(tool.project_content(exec_, {"is_error": True}))
        # 二次调用：projection 已被消耗 → None
        self.assertIsNone(tool.project_content(exec_, {"value": value}))


class TestAuthorizationCommit(unittest.TestCase):
    def setUp(self):
        from miniharness.seams.credentials_local import (
            LocalCredentialProvider, install_credentials)
        from miniharness.seams.authorization import install_authorization
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.ctx = Context(name="auth-commit")
        self.addCleanup(self.ctx.dispose)
        install_credentials(self.ctx, LocalCredentialProvider(
            os.path.join(self._tmp.name, "credentials.json"), read_env=False))
        self.auth = install_authorization(self.ctx)
        self.creds = self.ctx.get("credentials")

    def test_commit_writes_record_and_sets_committing(self):
        key = "test/cred"
        committed = []

        def run(session):
            session.commit({"kind": "grant", "payload": {"t": 1}})
            committed.append(True)

        self.auth.registerFlow({
            "key": key, "label": "t", "methods": [{"id": "m", "label": "M"}],
            "run": run,
        })
        outcome = self.auth.begin({"key": key, "interaction": None})
        self.assertEqual(outcome, {"status": "authorized"})
        self.assertTrue(committed)
        stored = self.creds.describe_record(key)
        self.assertTrue(stored.get("configured"))

    def test_cancel_during_commit_waits(self):
        # commit 置位后 cancel 不再 abort（提交中等待完成）——同步模型下
        # commit 已同步完成，cancel 是 no-op。
        key = "test/cred2"
        entered = []

        def run(session):
            session.commit({"kind": "grant", "payload": {"t": 2}})
            entered.append(True)
            self.auth.cancel(key)  # committing 中 → 不 abort

        self.auth.registerFlow({
            "key": key, "label": "t", "methods": [{"id": "m", "label": "M"}],
            "run": run,
        })
        outcome = self.auth.begin({"key": key, "interaction": None})
        self.assertEqual(outcome, {"status": "authorized"})

    def test_commit_after_cancel_rejected(self):
        key = "test/cred3"
        aborted = []

        def run(session):
            self.auth.cancel(key)
            # 撤销后 commit 抛 CANCELLED（attempt 不再活跃）
            from miniharness.seams.authorization import AuthorizationError
            with self.assertRaises(AuthorizationError):
                session.commit({"kind": "grant", "payload": {"t": 3}})
            aborted.append(True)

        self.auth.registerFlow({
            "key": key, "label": "t", "methods": [{"id": "m", "label": "M"}],
            "run": run,
        })
        outcome = self.auth.begin({"key": key, "interaction": None})
        self.assertEqual(outcome, {"status": "cancelled"})
        self.assertTrue(aborted)


class TestScheduleArchiveAdmission(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="sched-archive")
        self.addCleanup(self.ctx.dispose)
        from miniharness.core.session_store import install_sessions
        from miniharness.core.agents import install_agents
        from miniharness.schedule import install_schedule
        install_agents(self.ctx)
        install_sessions(self.ctx)
        install_schedule(self.ctx)
        self.session = self.ctx.get("sessions").create(
            "s1", {"meta": {"cwd": __import__("os").path.abspath(".")}})

    def _add_reminder(self):
        import time
        from miniharness.schedule.domain import (
            allocate_schedule_id, create_after_schedule_record, fold_schedule_events)
        folded = fold_schedule_events(list(self.session.events))
        rid = allocate_schedule_id(folded)
        record = create_after_schedule_record(rid, "remind me", 5,
                                              int(time.time() * 1000))
        self.session.append("schedule/change", {
            "version": 1, "operation": "create", "schedule": record})
        return rid

    def test_session_activity_reports_schedule_kind(self):
        import asyncio
        self._add_reminder()
        activities = asyncio.run(self.ctx.awaterfall(
            "workspace/session-activity", {"sessionId": "s1"},
            base=lambda p: []))
        kinds = [a.kind for a in (activities or [])]
        self.assertIn("schedule", kinds)
        sched = next(a for a in (activities or []) if a.kind == "schedule")
        self.assertTrue(sched.items)

    def test_session_stop_deletes_active_reminders(self):
        import asyncio
        rid = self._add_reminder()
        asyncio.run(self.ctx.aparallel("workspace/session-stop",
                                       {"sessionId": "s1"}))
        from miniharness.schedule.domain import fold_schedule_events
        folded = fold_schedule_events(list(self.session.events))
        active = [r["id"] for r in folded.get("active") or []]
        self.assertNotIn(rid, active)


class TestHooksSourceKind(unittest.TestCase):
    def test_post_tool_emits_hook_context_source(self):
        from miniharness.protocol.hooks import ClaudeCodeBridge
        bridge = ClaudeCodeBridge({
            "hooks": {
                "PostToolUse": [{
                    "matcher": "*",
                    "hooks": [{
                        "command": "echo",
                        "args": ["-n", "note"],
                        "decision": "pass",
                        "additionalContext": "from-hook",
                    }],
                }],
            },
        }, {"pluginRoot": ".", "projectDir": "."})
        decision = bridge.post_tool("Bash", run_fn=lambda hook, payload: (
            {"decision": "pass", "additionalContext": "from-hook"}, 5))
        self.assertIsNotNone(decision)
        contexts = decision.get("additionalContexts") or []
        self.assertTrue(contexts)
        self.assertEqual(contexts[0]["source"]["kind"], "hooks-claude-code")

    def test_stop_emits_hook_context_message(self):
        from miniharness.protocol.hooks import ClaudeCodeBridge
        bridge = ClaudeCodeBridge({
            "hooks": {
                "Stop": [{
                    "hooks": [{
                        "command": "echo",
                        "args": ["-n", "cont"],
                        "decision": "deny",
                        "continue": True,
                        "additionalContext": "keep-going",
                    }],
                }],
            },
        }, {"pluginRoot": ".", "projectDir": "."})
        result = bridge.stop(run_fn=lambda hook, payload: (
            {"decision": "deny", "continue": True,
             "additionalContext": "keep-going"}, 5))
        self.assertTrue(result["continue"])
        self.assertEqual(result["message"]["source"]["kind"], "hooks-claude-code")


if __name__ == "__main__":
    unittest.main()