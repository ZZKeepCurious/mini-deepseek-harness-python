"""glob / grep 检索工具验收（对齐 packages/fs/tool-fs-search 发现语义）。"""
import pathlib
import tempfile
import unittest

from miniharness.core.session.session import Session
from miniharness.core.scope import Context
from miniharness.core.tools import ToolExec, ToolRegistry
from miniharness.fs import install_fs_search_tools, install_local_fs


class _Agent:
    def __init__(self, session):
        self.session = session


class SearchToolsTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        (self.dir / "src").mkdir()
        (self.dir / "src" / "a.py").write_text("import os\nprint('hi')\n", encoding="utf-8")
        (self.dir / "src" / "b.ts").write_text("export const x = 1\n", encoding="utf-8")
        (self.dir / ".git").mkdir()
        (self.dir / ".git" / "config").write_text("hidden\n", encoding="utf-8")
        self.ctx = Context(name="root")
        ToolRegistry(self.ctx)
        install_local_fs(self.ctx, {"cwd": str(self.dir)})
        self.tools = install_fs_search_tools(self.ctx)

    def tearDown(self):
        self.ctx.dispose()
        self._tmp.cleanup()

    def _exec(self) -> ToolExec:
        return ToolExec(agent=_Agent(Session("s", meta={"cwd": str(self.dir)})))

    async def test_glob_matches_and_excludes_vcs(self):
        value = await self.tools["glob"].execute({"pattern": "**/*.py"}, self._exec())
        self.assertEqual(value["paths"], ["src/a.py"])
        text = self.tools["glob"].render({}, value)[0]["text"]
        self.assertIn("src/a.py", text)

    async def test_glob_basename_pattern_any_depth(self):
        value = await self.tools["glob"].execute({"pattern": "*.ts"}, self._exec())
        self.assertEqual(value["paths"], ["src/b.ts"])

    async def test_grep_grouped_with_include_filter(self):
        value = await self.tools["grep"].execute(
            {"pattern": "import", "include": "*.py"}, self._exec())
        self.assertEqual(value["matches"][0]["path"], "src/a.py")
        self.assertEqual(value["matches"][0]["lineNumber"], 1)
        text = self.tools["grep"].render({}, value)[0]["text"]
        self.assertIn("src/a.py:", text)
        self.assertIn("1: import os", text)

    async def test_grep_no_matches(self):
        value = await self.tools["grep"].execute({"pattern": "zzz"}, self._exec())
        self.assertEqual(value["matches"], [])
        self.assertEqual(self.tools["grep"].render({}, value)[0]["text"], "No matches found.")


if __name__ == "__main__":
    unittest.main()
