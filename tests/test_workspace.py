"""workspace 域验收（对齐 packages/workspace/workspace）。"""
import os
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.core.session_store import SessionStore
from miniharness.core.tools import ToolRegistry
from miniharness.workspace import (
    WorkspaceService,
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


if __name__ == "__main__":
    unittest.main()
