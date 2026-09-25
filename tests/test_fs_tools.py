"""tool-fs（read/write/edit）验收（对齐 packages/fs/tool-fs）。"""
import pathlib
import tempfile
import unittest

from miniharness.core.agent_loop.tool_calls import append_tool_call, emit_tool_result
from miniharness.core.session.session import Session
from miniharness.core.scope import Context
from miniharness.core.tools import ToolExec, ToolRegistry, ToolResult, run_pipeline_async
from miniharness.fs import FsError, install_local_fs, install_fs_observation_policy
from miniharness.fs.tools import install_fs_tools


class _Agent:
    def __init__(self, session):
        self.session = session


class FsToolsTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.ctx = Context(name="root")
        self.registry = ToolRegistry(self.ctx)
        self.fs = install_local_fs(self.ctx, {"cwd": str(self.dir)})
        self.tools = install_fs_tools(self.ctx)

    def tearDown(self):
        self.ctx.dispose()
        self._tmp.cleanup()

    def _exec(self, cwd: str | None = None) -> ToolExec:
        session = Session("s1", meta={"cwd": cwd} if cwd else {})
        return ToolExec(agent=_Agent(session))

    async def test_read_line_numbers_and_window(self):
        (self.dir / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
        value = await self.tools["read"].execute({"file_path": "a.txt"}, self._exec())
        self.assertEqual(value["totalLines"], 3)
        self.assertEqual([line["number"] for line in value["lines"]], [1, 2, 3])
        text = self.tools["read"].render({"file_path": "a.txt"}, value)[0]["text"]
        self.assertIn("1: one", text)
        self.assertIn("(End of file - total 3 lines)", text)
        windowed = await self.tools["read"].execute(
            {"file_path": "a.txt", "offset": 2, "limit": 1}, self._exec())
        self.assertEqual([line["text"] for line in windowed["lines"]], ["two"])
        self.assertIn("Use offset=3 to continue", self.tools["read"].render({}, windowed)[0]["text"])

    async def test_read_missing_and_offset_out_of_range(self):
        with self.assertRaises(FsError) as missing:
            await self.tools["read"].execute({"file_path": "nope.txt"}, self._exec())
        self.assertEqual(missing.exception.code, "FS_NOT_FOUND")
        (self.dir / "a.txt").write_text("one\n", encoding="utf-8")
        with self.assertRaises(FsError) as out:
            await self.tools["read"].execute({"file_path": "a.txt", "offset": 5}, self._exec())
        self.assertEqual(out.exception.code, "FS_NOT_FOUND")

    async def test_read_uses_session_cwd(self):
        session_dir = self.dir / "ws"
        session_dir.mkdir()
        (session_dir / "f.txt").write_text("hi\n", encoding="utf-8")
        value = await self.tools["read"].execute({"file_path": "f.txt"}, self._exec(str(session_dir)))
        self.assertEqual(value["path"], str(session_dir / "f.txt"))

    async def test_write_create_and_update_without_policy(self):
        created = await self.tools["write"].execute(
            {"file_path": "out.txt", "content": "a\n"}, self._exec())
        self.assertEqual(created["operation"], "create")
        self.assertIsNone(created["before"])
        updated = await self.tools["write"].execute(
            {"file_path": "out.txt", "content": "b\n"}, self._exec())
        self.assertEqual(updated["operation"], "update")
        self.assertEqual(updated["before"], "a\n")
        self.assertEqual((self.dir / "out.txt").read_text(encoding="utf-8"), "b\n")
        text = self.tools["write"].render({}, updated)[0]["text"]
        self.assertIn("Updated file", text)

    async def test_observation_policy_guards_write_and_edit(self):
        install_fs_observation_policy(self.ctx)
        (self.dir / "g.txt").write_text("hello world\n", encoding="utf-8")
        exec_ = self._exec()
        with self.assertRaises(FsError) as unread:
            await self.tools["write"].execute(
                {"file_path": "g.txt", "content": "x\n"}, exec_)
        self.assertEqual(unread.exception.code, "FS_NOT_OBSERVED")
        self.assertIn("has not been read", str(unread.exception))
        read = await self.tools["read"].execute({"file_path": "g.txt"}, exec_)
        self.assertEqual(read["totalLines"], 1)
        edited = await self.tools["edit"].execute(
            {"file_path": "g.txt", "old_string": "hello", "new_string": "HI"}, exec_)
        self.assertEqual(edited["after"], "HI world\n")
        text = self.tools["edit"].render({"file_path": "g.txt"}, edited)[0]["text"]
        self.assertIn("has been updated successfully", text)

    async def test_edit_unconditional_without_policy(self):
        (self.dir / "e.txt").write_text("alpha beta alpha", encoding="utf-8")
        with self.assertRaises(FsError) as ambiguous:
            await self.tools["edit"].execute(
                {"file_path": "e.txt", "old_string": "alpha", "new_string": "A"}, self._exec())
        self.assertEqual(ambiguous.exception.code, "FS_AMBIGUOUS_EDIT")
        value = await self.tools["edit"].execute(
            {"file_path": "e.txt", "old_string": "alpha", "new_string": "A",
             "replace_all": True}, self._exec())
        self.assertEqual(value["after"], "A beta A")

    async def test_edit_argument_validation(self):
        with self.assertRaisesRegex(ValueError, "must differ"):
            await self.tools["edit"].execute(
                {"file_path": "x", "old_string": "a", "new_string": "a"}, self._exec())
        with self.assertRaisesRegex(ValueError, "old_string must be a non-empty"):
            await self.tools["edit"].execute(
                {"file_path": "x", "old_string": "", "new_string": "b"}, self._exec())

    async def test_write_presentation_meta_operation_and_diffs(self):
        tool = self.registry.resolve("write")
        created = await tool.execute(
            {"file_path": "m.txt", "content": "one\ntwo\n"}, self._exec())
        meta = tool.presentation_meta({"file_path": "m.txt"}, created)
        self.assertEqual(meta["operation"], "create")
        self.assertEqual(meta["diffs"], [])
        updated = await tool.execute(
            {"file_path": "m.txt", "content": "one\nTWO\n"}, self._exec())
        meta = tool.presentation_meta({"file_path": "m.txt"}, updated)
        self.assertEqual(meta["operation"], "update")
        self.assertTrue(meta["diffs"])
        self.assertEqual(meta["diffs"][0]["path"], "m.txt")

    async def test_pipeline_populates_write_meta(self):
        tool = self.registry.resolve("write")
        result = await run_pipeline_async(
            self.ctx, tool, {"file_path": "p.txt", "content": "hi\n"}, self._exec())
        self.assertTrue(result.ok)
        self.assertEqual(result.meta["operation"], "create")
        self.assertEqual(result.meta["diffs"], [])
        nested_exec = self._exec()
        nested_exec.parent = object()
        nested = await run_pipeline_async(
            self.ctx, tool, {"file_path": "q.txt", "content": "x"}, nested_exec)
        self.assertEqual(nested.meta, {})

    async def test_tool_result_event_carries_presentation_meta(self):
        session = Session("s-meta")
        call_seq = append_tool_call(session, 1, 1, "c1", "write", "{}")
        emit_tool_result(
            session, 1, 1, "c1",
            ToolResult(ok=True, content="ok", value={},
                       meta={"operation": "create", "diffs": []}),
            call_seq)
        event = session.events[-1]
        self.assertEqual(event["type"], "tool/result")
        meta = event["data"]["meta"]
        self.assertEqual(meta["operation"], "create")
        self.assertEqual(list(meta["diffs"]), [])


if __name__ == "__main__":
    unittest.main()
