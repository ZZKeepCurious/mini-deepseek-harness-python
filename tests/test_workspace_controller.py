"""workspace-controller：命令、注册表状态与 follow 增量。

对齐 packages/api/workspace-controller（commands / feed 的确定性面）。
"""

import os
import tempfile
import unittest

from miniharness.core.agent_loop.resident_loop import run_on_resident
from miniharness.core.scope import Context
from miniharness.core.session_store import SessionStore
from miniharness.workspace import (
    SessionActivity,
    SessionActivityItem,
    install_workspaces,
)
from miniharness.workspace_controller import WorkspaceFault, install_workspace_controller


class WorkspaceControllerCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._home = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.addCleanup(self._home.cleanup)
        self.root = self._tmp.name
        self.ctx = Context(name="workspace-controller-test")
        self.addCleanup(self.ctx.dispose)
        self.store = SessionStore(self.ctx)
        install_workspaces(self.ctx, root=self._home.name)
        self.controller = install_workspace_controller(
            self.ctx, {"documentsDirectory": self.root})

    def _dir(self, name):
        path = os.path.join(self.root, name)
        os.makedirs(path, exist_ok=True)
        return path

    def _session(self, session_id, cwd):
        self.store.create(session_id, {"meta": {"cwd": cwd}})
        return session_id

    def _code(self, call):
        with self.assertRaises(WorkspaceFault) as caught:
            call()
        return caught.exception.code

    def test_create_is_idempotent_by_path(self):
        path = self._dir("proj")
        created = self.controller.create({"path": path})
        self.assertTrue(created["created"])
        self.assertEqual(created["workspace"]["title"], "proj")
        again = self.controller.create({"path": path})
        self.assertFalse(again["created"])
        self.assertEqual(again["workspace"]["workspaceId"], created["workspace"]["workspaceId"])

    def test_create_rejects_invalid_path(self):
        self.assertEqual(self._code(
            lambda: self.controller.create({"path": os.path.join(self.root, "nope")})),
            "workspace/invalid-path")

    def test_rename_validates_and_detects_conflicts(self):
        first = self.controller.create({"path": self._dir("a")})["workspace"]["workspaceId"]
        second = self.controller.create({"path": self._dir("b")})["workspace"]["workspaceId"]
        renamed = self.controller.rename({"workspaceId": first, "title": "  Alpha  "})
        self.assertEqual(renamed["workspace"]["title"], "Alpha")
        self.assertEqual(self._code(
            lambda: self.controller.rename({"workspaceId": second, "title": "Alpha"})),
            "workspace/name-conflict")
        self.assertEqual(self._code(
            lambda: self.controller.rename({"workspaceId": first, "title": "   "})),
            "gateway/bad-request")
        self.assertEqual(self._code(
            lambda: self.controller.rename({"workspaceId": "missing", "title": "X"})),
            "workspace/not-found")

    def test_delete_and_order_mutations(self):
        a = self.controller.create({"path": self._dir("a")})["workspace"]["workspaceId"]
        b = self.controller.create({"path": self._dir("b")})["workspace"]["workspaceId"]
        c = self.controller.create({"path": self._dir("c")})["workspace"]["workspaceId"]
        ordered = self.controller.insert_before({"workspaceId": c, "beforeWorkspaceId": a})
        self.assertEqual(ordered["workspaceIds"], [c, a, b])
        self.assertEqual(self._code(
            lambda: self.controller.insert_before({"workspaceId": "missing"})),
            "workspace/not-found")
        self.assertEqual(self.controller.delete({"workspaceId": a}), {"deleted": True})
        self.assertEqual(self._code(lambda: self.controller.delete({"workspaceId": a})),
                         "workspace/not-found")

    def test_insert_session_before_rejects_unaccounted(self):
        path = self._dir("proj")
        workspace_id = self.controller.create({"path": path})["workspace"]["workspaceId"]
        self._session("s1", path)
        workspace = self.ctx.get("workspaces").get(workspace_id)
        run_on_resident(workspace.attach_session("s1"))
        moved = self.controller.insert_session_before(
            {"workspaceId": workspace_id, "sessionId": "s1"})
        self.assertEqual(moved["workspace"]["sessionIds"], ["s1"])
        self.assertEqual(self._code(lambda: self.controller.insert_session_before(
            {"workspaceId": workspace_id, "sessionId": "s2"})),
            "workspace/move-invalid")

    def test_archive_and_unarchive_sessions(self):
        path = self._dir("proj")
        self.controller.create({"path": path})
        self._session("s1", path)
        archived = self.controller.archive_session({"sessionId": "s1"})
        self.assertEqual(archived["archivedSessionIds"], ["s1"])
        self.assertEqual(self._code(
            lambda: self.controller.archive_session({"sessionId": "ghost"})),
            "session/not-found")
        self.assertEqual(self.controller.unarchive_session({"sessionId": "s1"}),
                         {"archivedSessionIds": []})

    def test_initialize_default_creates_and_validates(self):
        created = self.controller.initialize_default(
            {"directoryName": "proj", "title": "My Project"})
        expected = os.path.realpath(
            os.path.join(self.root, "deepseek-harness", "proj"))
        self.assertEqual(created["workspace"]["title"], "My Project")
        self.assertEqual(created["workspace"]["path"], expected)
        self.assertTrue(os.path.isdir(expected))
        # 重复请求复用持久身份
        again = self.controller.initialize_default(
            {"directoryName": "other", "title": "Other"})
        self.assertEqual(again["workspace"]["workspaceId"],
                         created["workspace"]["workspaceId"])
        self.assertEqual(self._code(lambda: self.controller.initialize_default(
            {"directoryName": "a/b", "title": "X"})), "gateway/bad-request")
        self.assertEqual(self._code(lambda: self.controller.initialize_default(
            {"directoryName": "proj2", "title": "   "})), "gateway/bad-request")

    def test_archive_session_reports_activity_and_stop(self):
        path = self._dir("proj")
        self.controller.create({"path": path})
        self._session("s1", path)

        async def activity(payload, nxt):
            return [SessionActivity("schedule", [SessionActivityItem("sched-1")]),
                    *await nxt(payload)]

        self.ctx.on("workspace/session-activity", activity)
        with self.assertRaises(WorkspaceFault) as caught:
            self.controller.archive_session({"sessionId": "s1"})
        self.assertEqual(caught.exception.code, "workspace/session-active")
        self.assertEqual(caught.exception.details["sessionId"], "s1")
        self.assertEqual(caught.exception.details["activity"],
                         [{"kind": "schedule", "items": [{"id": "sched-1"}]}])
        self.assertEqual(self.ctx.get("workspaces").archivedSessionIds, [])

        stopped = []
        self.ctx.on("workspace/session-stop",
                    lambda payload: stopped.append(payload["sessionId"]))
        archived = self.controller.archive_session(
            {"sessionId": "s1", "stopActivity": True})
        self.assertEqual(archived["archivedSessionIds"], ["s1"])
        self.assertEqual(stopped, ["s1"])

    def test_pin_and_unpin_sessions(self):
        path = self._dir("proj")
        self.controller.create({"path": path})
        self._session("s1", path)
        self._session("s2", path)
        self.assertEqual(self.controller.pin_session({"sessionId": "s1"}),
                         {"pinnedSessionIds": ["s1"]})
        self.assertEqual(self.controller.pin_session({"sessionId": "s2"}),
                         {"pinnedSessionIds": ["s2", "s1"]})
        self.assertEqual(self.controller.unpin_session({"sessionId": "s1"}),
                         {"pinnedSessionIds": ["s2"]})
        self.assertEqual(self._code(
            lambda: self.controller.pin_session({"sessionId": "ghost"})),
            "session/not-found")
        # 归档会丢弃 pin，且归档后不可置顶
        self.controller.archive_session({"sessionId": "s2"})
        self.assertEqual(self.controller.baseline()["pinnedSessionIds"], [])
        self.assertEqual(self._code(
            lambda: self.controller.pin_session({"sessionId": "s2"})),
            "gateway/bad-request")

    def test_follow_starts_with_baseline_then_ordered_increments(self):
        follow = self.controller.follow()
        self.assertEqual(follow.baseline["type"], "baseline")
        self.assertEqual(follow.baseline["value"]["items"], [])
        path = self._dir("proj")
        created = self.controller.create({"path": path})["workspace"]["workspaceId"]
        upsert = follow.pop()
        self.assertEqual(upsert["type"], "upsert")
        self.assertEqual(upsert["workspace"]["workspaceId"], created)
        self.controller.rename({"workspaceId": created, "title": "Renamed"})
        self.assertEqual(follow.pop()["workspace"]["title"], "Renamed")
        self.controller.delete({"workspaceId": created})
        self.assertEqual(follow.pop(), {"type": "remove", "workspaceId": created})
        follow.close()
        self.assertIsNone(follow.pop())


if __name__ == "__main__":
    unittest.main()
