"""todo 域验收（对齐 packages/todo/tool-todo）。"""
import unittest

from miniharness.core.scope import Context
from miniharness.core.session.session import Session
from miniharness.core.tools import ToolExec, ToolRegistry
from miniharness.todo import fold_todos, install_todo_tool, to_todo_list


class _Agent:
    def __init__(self, session):
        self.session = session


class TestTodoList(unittest.TestCase):
    def test_validation(self):
        with self.assertRaisesRegex(ValueError, "non-empty string"):
            to_todo_list([{"content": "  ", "status": "pending"}], True)
        with self.assertRaisesRegex(ValueError, "duplicate content"):
            to_todo_list([{"content": "a", "status": "pending"},
                          {"content": "a", "status": "completed"}], True)
        with self.assertRaisesRegex(ValueError, "invalid todo status"):
            to_todo_list([{"content": "a", "status": "done"}], True)
        with self.assertRaisesRegex(ValueError, "at most one task may be in_progress"):
            to_todo_list([{"content": "a", "status": "in_progress"},
                          {"content": "b", "status": "in_progress"}], False)
        parallel = to_todo_list([{"content": "a", "status": "in_progress"},
                                 {"content": "b", "status": "in_progress"}], True)
        self.assertEqual(len(parallel), 2)

    def test_fold_latest_wins_and_turn_clears(self):
        self.assertIsNone(fold_todos([]))
        events = [
            {"type": "todo/write", "data": {"todos": [{"content": "a", "status": "pending"}]}},
            {"type": "todo/write", "data": {"todos": [{"content": "b", "status": "completed"}]}},
        ]
        self.assertEqual(fold_todos(events), [{"content": "b", "status": "completed"}])
        events.append({"type": "turn/start", "data": {"turn": 1}})
        self.assertIsNone(fold_todos(events))


class TestTodoTool(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        ToolRegistry(self.ctx)
        self.tool = install_todo_tool(self.ctx)
        self.session = Session("s1", meta={})
        self.exec = ToolExec(agent=_Agent(self.session))

    def tearDown(self):
        self.ctx.dispose()

    def test_write_appends_and_counts(self):
        value = self.tool.execute({"todos": [
            {"content": "do a", "status": "in_progress"},
            {"content": "do b", "status": "pending"},
        ]}, self.exec)
        self.assertEqual(value["counts"], {"pending": 1, "inProgress": 1, "completed": 0})
        self.assertEqual(self.session.events[-1]["type"], "todo/write")
        text = self.tool.render({}, value)[0]["text"]
        self.assertIn("1 pending, 1 in progress, 0 completed", text)

    def test_requires_agent(self):
        with self.assertRaisesRegex(RuntimeError, "requires an owning agent session"):
            self.tool.execute({"todos": [{"content": "x", "status": "pending"}]}, ToolExec())

    def test_parallel_disabled_rejects_two_active(self):
        ctx = Context(name="strict")
        try:
            ToolRegistry(ctx)
            tool = install_todo_tool(ctx, allow_parallel_in_progress=False)
            with self.assertRaisesRegex(ValueError, "at most one task"):
                tool.execute({"todos": [
                    {"content": "a", "status": "in_progress"},
                    {"content": "b", "status": "in_progress"},
                ]}, self.exec)
        finally:
            ctx.dispose()


if __name__ == "__main__":
    unittest.main()
