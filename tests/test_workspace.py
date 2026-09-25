"""workspace 域验收（对齐 packages/workspace/workspace）。"""
import os
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.core.session_store import SessionStore
from miniharness.core.tools import ToolRegistry
from miniharness.workspace import (
    SessionActivity,
    SessionActivityItem,
    WorkspaceActiveSessionError,
    WorkspaceArchivedSessionPinError,
    WorkspaceService,
    WorkspaceUnknownSessionError,
    default_workspace_title,
    fully_qualified_workspace_path,
    install_workspaces,
    realpath_normalize,
)


class TestPaths(unittest.TestCase):
    def test_qualified_and_title(self):
        self.assertTrue(fully_qualified_workspace_path(os.path.abspath(os.sep)))
        self.assertFalse(fully_qualified_workspace_path("relative/dir"))
        root = os.path.abspath(os.sep)
        self.assertEqual(default_workspace_title(os.path.join(root, "proj")), "proj")
        with self.assertRaises(TypeError):
            realpath_normalize("relative")


class WorkspaceCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.home = tempfile.TemporaryDirectory()
        self.root = self._tmp.name
        self.ws_dir = os.path.join(self.root, "proj")
        os.makedirs(self.ws_dir)
        self.ctx = Context(name="root")
        ToolRegistry(self.ctx)
        self.store = SessionStore(self.ctx)
        self.service = WorkspaceService(self.ctx, root=self.home.name)

    def tearDown(self):
        self.ctx.dispose()
        self._tmp.cleanup()
        self.home.cleanup()

    def _session(self, session_id: str, cwd: str):
        return self.store.create(session_id, {"meta": {"cwd": cwd}})

    async def test_create_default_title_and_duplicate(self):
        ws = await self.service.create(self.ws_dir)
        self.assertEqual(ws.title, "proj")
        self.assertEqual(ws.path, os.path.realpath(self.ws_dir))
        self.assertEqual(ws.status(), "ok")
        with self.assertRaisesRegex(ValueError, "already exists"):
            await self.service.create(self.ws_dir)

    async def test_create_requires_existing_directory(self):
        with self.assertRaises(FileNotFoundError):
            await self.service.create(os.path.join(self.root, "nope"))

    async def test_attach_requires_matching_cwd(self):
        ws = await self.service.create(self.ws_dir)
        self._session("s1", self.ws_dir)
        await ws.attach_session("s1")
        self.assertEqual(ws.sessionIds, ["s1"])
        other = os.path.join(self.root, "other")
        os.makedirs(other)
        self._session("s2", other)
        with self.assertRaisesRegex(ValueError, "does not match"):
            await ws.attach_session("s2")
        with self.assertRaisesRegex(ValueError, "unknown session"):
            await ws.attach_session("missing")

    async def test_attach_prepends_and_reorder(self):
        ws = await self.service.create(self.ws_dir)
        self._session("s1", self.ws_dir)
        self._session("s2", self.ws_dir)
        await ws.attach_session("s1")
        await ws.attach_session("s2")
        self.assertEqual(ws.sessionIds, ["s2", "s1"])
        await ws.insert_session_before("s2", "s1")
        self.assertEqual(ws.sessionIds, ["s2", "s1"])
        await ws.detach_session("s1")
        self.assertEqual(ws.sessionIds, ["s2"])
        await ws.detach_session("s1")  # idempotent

    async def test_persistence_and_status(self):
        ws = await self.service.create(self.ws_dir, title="My Project")
        ctx2 = Context(name="reload")
        try:
            ToolRegistry(ctx2)
            reloaded = WorkspaceService(ctx2, root=self.home.name)
            again = reloaded.get(ws.id)
            self.assertEqual(again.title, "My Project")
            self.assertEqual([w.id for w in reloaded.list()], [ws.id])
            os.rmdir(self.ws_dir)
            self.assertEqual(again.status(), "missing-dir")
        finally:
            ctx2.dispose()

    async def test_remove_and_install_idempotent(self):
        ws = await self.service.create(self.ws_dir)
        await self.service.remove(ws.id)
        self.assertIsNone(self.service.get(ws.id))
        ctx = Context(name="install")
        try:
            first = install_workspaces(ctx, root=self.home.name)
            self.assertIs(ctx.get("workspaces"), first)
            self.assertIs(install_workspaces(ctx), first)
        finally:
            ctx.dispose()

    async def test_initialize_default_only_when_empty(self):
        target = os.path.join(self.root, "default-proj")

        async def resolve():
            return {"path": target, "title": "Default"}

        ws = await self.service.initialize_default(resolve)
        self.assertIsNotNone(ws)
        self.assertEqual(ws.title, "Default")
        self.assertTrue(os.path.isdir(target))
        # 重复请求复用持久身份，且不再调用 resolve_directory
        async def other():
            return {"path": os.path.join(self.root, "other"), "title": "Other"}

        again = await self.service.initialize_default(other)
        self.assertEqual(again.id, ws.id)
        # 删除默认登记后永久禁用自动创建
        self.service.delete(ws.id)
        self.assertIsNone(await self.service.initialize_default(other))

    async def test_initialize_default_skips_when_registry_or_sessions_present(self):
        async def resolve():
            return {"path": os.path.join(self.root, "new"), "title": "New"}

        await self.service.create(self.ws_dir)
        self.assertIsNone(await self.service.initialize_default(resolve))
        self.service.delete(self.service.ids()[0])
        self._session("s1", self.ws_dir)
        self.assertIsNone(await self.service.initialize_default(resolve))

    async def test_default_workspace_id_persisted(self):
        target = os.path.join(self.root, "default-proj")

        async def resolve():
            return {"path": target, "title": "Default"}

        ws = await self.service.initialize_default(resolve)
        ctx2 = Context(name="reload-default")
        try:
            ToolRegistry(ctx2)
            SessionStore(ctx2)
            reloaded = WorkspaceService(ctx2, root=self.home.name)
            self.assertEqual(reloaded._default_workspace_id, ws.id)
            self.assertEqual(reloaded.get(ws.id).title, "Default")
        finally:
            ctx2.dispose()

    async def test_archive_admission_reports_activity_and_stop(self):
        await self.service.create(self.ws_dir)
        self._session("s1", self.ws_dir)
        stopped = []

        async def activity(payload, nxt):
            return [SessionActivity("turn"),
                    SessionActivity("job", [SessionActivityItem("j1", "Build")]),
                    *await nxt(payload)]

        self.ctx.on("workspace/session-activity", activity)
        with self.assertRaises(WorkspaceActiveSessionError) as caught:
            await self.service.archive_session("s1")
        self.assertEqual(caught.exception.session_id, "s1")
        self.assertEqual([entry.kind for entry in caught.exception.activity], ["turn", "job"])
        self.assertEqual(self.service.archivedSessionIds, [])

        self.ctx.on("workspace/session-stop",
                    lambda payload: stopped.append(payload["sessionId"]))
        # 带 stopActivity：跳过活动检查、写入后派发停止
        await self.service.archive_session("s1", {"stopActivity": True})
        self.assertEqual(self.service.archivedSessionIds, ["s1"])
        self.assertEqual(stopped, ["s1"])

    async def test_archive_without_providers_archives_freely(self):
        await self.service.create(self.ws_dir)
        self._session("s1", self.ws_dir)
        await self.service.archive_session("s1")
        self.assertEqual(self.service.archivedSessionIds, ["s1"])

    async def test_pin_order_idempotence_and_unknown(self):
        await self.service.create(self.ws_dir)
        self._session("s1", self.ws_dir)
        self._session("s2", self.ws_dir)
        await self.service.pin_session("s1")
        await self.service.pin_session("s2")
        self.assertEqual(self.service.pinnedSessionIds, ["s2", "s1"])
        await self.service.pin_session("s2")  # 已置顶不重排
        self.assertEqual(self.service.pinnedSessionIds, ["s2", "s1"])
        with self.assertRaises(WorkspaceUnknownSessionError):
            await self.service.pin_session("ghost")
        await self.service.unpin_session("s1")
        self.assertEqual(self.service.pinnedSessionIds, ["s2"])
        await self.service.unpin_session("s1")  # 幂等
        self.assertEqual(self.service.pinnedSessionIds, ["s2"])

    async def test_archive_drops_pin_and_archived_cannot_pin(self):
        await self.service.create(self.ws_dir)
        self._session("s1", self.ws_dir)
        await self.service.pin_session("s1")
        self.assertEqual(self.service.pinnedSessionIds, ["s1"])
        await self.service.archive_session("s1")
        self.assertEqual(self.service.pinnedSessionIds, [])
        self.assertEqual(self.service.archivedSessionIds, ["s1"])
        with self.assertRaises(WorkspaceArchivedSessionPinError):
            await self.service.pin_session("s1")

    async def test_pin_persisted_across_reload(self):
        await self.service.create(self.ws_dir)
        self._session("s1", self.ws_dir)
        await self.service.pin_session("s1")
        ctx2 = Context(name="reload-pin")
        try:
            ToolRegistry(ctx2)
            reloaded = WorkspaceService(ctx2, root=self.home.name)
            self.assertEqual(reloaded.pinnedSessionIds, ["s1"])
        finally:
            ctx2.dispose()


if __name__ == "__main__":
    unittest.main()
