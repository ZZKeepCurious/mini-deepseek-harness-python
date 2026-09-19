"""fs-sandbox 验收（对齐 packages/fs/fs-sandbox：每次调用策略围栏）。"""
import os
import pathlib
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.fs import (
    FsEditRequest,
    FsError,
    SandboxedFileSystem,
    install_sandboxed_fs,
    is_path_under,
    writable_roots,
)


class TestContainment(unittest.TestCase):
    def test_lexical_and_identity(self):
        root = os.path.realpath(tempfile.gettempdir())
        self.assertTrue(is_path_under(root, root))
        self.assertTrue(is_path_under(os.path.join(root, "a", "b.txt"), root))
        self.assertFalse(is_path_under(os.path.dirname(root), root))

    def test_writable_roots_only_for_workspace_write(self):
        self.assertEqual(writable_roots({"mode": "read-only", "workspaceRoot": "/x"}), [])
        roots = writable_roots({"mode": "workspace-write", "workspaceRoot": "/x"})
        self.assertIn(os.path.realpath("/x"), roots)


class SandboxFsTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._outside = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.outside = pathlib.Path(self._outside.name)

    def tearDown(self):
        self._tmp.cleanup()
        self._outside.cleanup()

    async def test_read_only_denies_write_but_allows_read(self):
        ctx = Context(name="ro")
        try:
            fs = SandboxedFileSystem(ctx, {"cwd": str(self.dir)}, default_mode="read-only")
            self.assertEqual(fs.sandbox_mode, "read-only")
            (self.dir / "a.txt").write_text("hello", encoding="utf-8")
            inner = await fs.resolve("a.txt")
            self.assertEqual(await fs.read_text(inner), "hello")
            with self.assertRaises(FsError) as denied:
                await fs.write_text(await fs.resolve("b.txt"), "x")
            self.assertEqual(denied.exception.code, "FS_SANDBOX_DENIED")
            with self.assertRaises(FsError):
                await fs.edit_text(inner, FsEditRequest("hello", "hi", False))
        finally:
            ctx.dispose()

    async def test_workspace_write_fences_by_root(self):
        ctx = Context(name="ww")
        try:
            fs = SandboxedFileSystem(ctx, {"cwd": str(self.dir)})
            policy = {"mode": "workspace-write", "workspaceRoot": str(self.dir)}
            inside = await fs.resolve("inside.txt")
            outcome = await fs.write_text(inside, "ok", sandbox_policy=policy)
            self.assertEqual(outcome.operation, "create")
            outside = await fs.resolve(os.path.join(
                os.path.abspath(os.sep), "mini-fs-sandbox-outside", "no.txt"))
            with self.assertRaises(FsError) as denied:
                await fs.write_text(outside, "no", sandbox_policy=policy)
            self.assertEqual(denied.exception.code, "FS_SANDBOX_DENIED")
            with self.assertRaises(FsError) as denied_edit:
                await fs.edit_text(inside, FsEditRequest("ok", "no", False),
                                   sandbox_policy={"mode": "read-only"})
            self.assertEqual(denied_edit.exception.code, "FS_SANDBOX_DENIED")
        finally:
            ctx.dispose()

    async def test_danger_full_access_unfenced(self):
        ctx = Context(name="danger")
        try:
            fs = SandboxedFileSystem(ctx, {"cwd": str(self.dir)}, default_mode="danger-full-access")
            self.assertEqual(fs.sandbox_mode, "danger-full-access")
            outside = await fs.resolve(str(self.outside / "free.txt"))
            await fs.write_text(outside, "ok")
            self.assertEqual((self.outside / "free.txt").read_text(encoding="utf-8"), "ok")
        finally:
            ctx.dispose()

    async def test_install_is_idempotent(self):
        ctx = Context(name="install")
        try:
            fs = install_sandboxed_fs(ctx, {"cwd": str(self.dir)}, default_mode="read-only")
            self.assertIs(ctx.get("fs"), fs)
            self.assertIs(install_sandboxed_fs(ctx), fs)
        finally:
            ctx.dispose()


if __name__ == "__main__":
    unittest.main()
