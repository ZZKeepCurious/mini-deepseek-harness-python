"""str_replace_editor 与 present 工具验收。"""
import pathlib
import tempfile
import unittest

from miniharness.core.session.session import Session
from miniharness.core.scope import Context
from miniharness.core.tools import ToolExec, ToolRegistry
from miniharness.fs import (
    FsError,
    install_local_fs,
    install_present_tool,
    install_str_replace_editor,
)


class _Agent:
    def __init__(self, session):
        self.session = session


class EditorPresentTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.ctx = Context(name="root")
        ToolRegistry(self.ctx)
        install_local_fs(self.ctx, {"cwd": str(self.dir)})
        self.editor = install_str_replace_editor(self.ctx)
        self.present = install_present_tool(self.ctx)

    def tearDown(self):
        self.ctx.dispose()
        self._tmp.cleanup()

    def _exec(self) -> ToolExec:
        return ToolExec(agent=_Agent(Session("s", meta={"cwd": str(self.dir)})))

    async def _view(self, path: str, **extra):
        return await self.editor.execute(
            {"command": "view", "path": path, **extra}, self._exec())

    async def test_view_file_numbered(self):
        (self.dir / "a.txt").write_text("one\ntwo\n", encoding="utf-8")
        value = await self._view(str(self.dir / "a.txt"))
        self.assertIn("     1\tone", value["output"])
        self.assertIn("     2\ttwo", value["output"])

    async def test_view_directory_two_levels(self):
        (self.dir / "sub").mkdir()
        (self.dir / "sub" / "f.txt").write_text("x", encoding="utf-8")
        value = await self._view(str(self.dir))
        self.assertIn("Listing non-hidden files and directories", value["output"])
        self.assertIn("sub", value["output"])

    async def test_requires_absolute_path(self):
        with self.assertRaisesRegex(ValueError, "not an absolute path"):
            await self._view("relative.txt")

    async def test_create_refuses_existing(self):
        path = self.dir / "new.txt"
        created = await self.editor.execute(
            {"command": "create", "path": str(path), "file_text": "hello\n"}, self._exec())
        self.assertIn("New file created successfully", created["output"])
        self.assertEqual(path.read_text(encoding="utf-8"), "hello\n")
        with self.assertRaises(FsError):
            await self.editor.execute(
                {"command": "create", "path": str(path), "file_text": "x"}, self._exec())

    async def test_str_replace_unique_and_ambiguous(self):
        path = self.dir / "s.txt"
        path.write_text("alpha beta alpha\n", encoding="utf-8")
        with self.assertRaises(FsError) as ambiguous:
            await self.editor.execute(
                {"command": "str_replace", "path": str(path), "old_str": "alpha",
                 "new_str": "A"}, self._exec())
        self.assertEqual(ambiguous.exception.code, "FS_AMBIGUOUS_EDIT")
        value = await self.editor.execute(
            {"command": "str_replace", "path": str(path), "old_str": "beta",
             "new_str": "B"}, self._exec())
        self.assertIn("edited successfully", value["output"])
        self.assertEqual(path.read_text(encoding="utf-8"), "alpha B alpha\n")

    async def test_str_replace_not_found(self):
        path = self.dir / "s.txt"
        path.write_text("hello\n", encoding="utf-8")
        with self.assertRaises(FsError) as missing:
            await self.editor.execute(
                {"command": "str_replace", "path": str(path), "old_str": "zzz",
                 "new_str": "y"}, self._exec())
        self.assertEqual(missing.exception.code, "FS_EDIT_NOT_FOUND")

    async def test_insert_line(self):
        path = self.dir / "i.txt"
        path.write_text("a\nc\n", encoding="utf-8")
        await self.editor.execute(
            {"command": "insert", "path": str(path), "insert_line": 1, "new_str": "b"},
            self._exec())
        self.assertEqual(path.read_text(encoding="utf-8"), "a\nb\nc\n")

    async def test_present_existing_files_and_missing(self):
        a = self.dir / "a.txt"
        a.write_text("x", encoding="utf-8")
        b = self.dir / "b.txt"
        b.write_text("y", encoding="utf-8")
        value = await self.present.execute(
            {"files": [{"path": str(a), "description": "out"},
                       {"path": str(a)}, {"path": str(b)}]}, self._exec())
        self.assertEqual([f["path"] for f in value["files"]], [str(a), str(b)])
        text = self.present.render({}, value)[0]["text"]
        self.assertIn(str(a), text)
        with self.assertRaises(FsError):
            await self.present.execute(
                {"files": [{"path": str(self.dir / "missing.txt")}]}, self._exec())


if __name__ == "__main__":
    unittest.main()
