"""deliverables/present：工具声明交付 + durable 事件 + 会话格式读侧校验。"""
import asyncio
import os
import tempfile
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import Session, thaw
from miniharness.core.session.released.payload_validation import (
    assert_released_payload_semantics,
)
from miniharness.core.system_prompt import install_system_prompt
from miniharness.core.tools import ToolExec, ToolRegistry
from miniharness.deliverables import register_present_tool
from miniharness.fs import install_local_fs
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.session_projection import install_session_projections
from miniharness.working_directory import install_working_directory


class PresentToolTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.work = os.path.join(self._tmp.name, "work")
        os.makedirs(self.work)
        with open(os.path.join(self.work, "report.md"), "w", encoding="utf-8") as handle:
            handle.write("# report\n")
        os.makedirs(os.path.join(self.work, "folder"))
        self.ctx = Context(name="present-test")
        self.addCleanup(self.ctx.dispose)
        install_system_prompt(self.ctx)
        install_local_fs(self.ctx, {"cwd": self.work})
        install_session_projections(self.ctx)
        install_working_directory(self.ctx, {"defaultDirectory": self.work})
        self.registry = ToolRegistry(self.ctx)
        register_present_tool(self.registry, self.ctx)
        self.session = Session("present-session", meta={"cwd": self.work})
        self.loop = AgentLoop(self.session, FakeLlmAdapter(), self.registry, self.ctx)
        self.session.append("turn/start", {"turn": 1})
        self.tool = self.registry.resolve("present", self.loop.ctx)

    def _exec(self, call_id="present-1"):
        return ToolExec(agent=self.loop, name="present", call_id=call_id)

    def test_present_declares_files_and_appends_event(self):
        value = asyncio.run(self.tool.execute(
            {"files": [{"path": "report.md", "description": "Final report"}]},
            self._exec()))
        expected = os.path.join(os.path.realpath(self.work), "report.md")
        self.assertEqual(value["turn"], 1)
        self.assertEqual(value["files"],
                         [{"path": expected, "description": "Final report"}])
        event = self.session.events[-1]
        self.assertEqual(event["type"], "deliverables/presented")
        self.assertEqual(thaw(event["data"]), {
            "turn": 1, "callId": "present-1",
            "files": [{"path": expected, "description": "Final report"}],
        })

    def test_present_rejects_missing_non_file_and_empty(self):
        with self.assertRaisesRegex(ValueError, "file not found"):
            asyncio.run(self.tool.execute(
                {"files": [{"path": "missing.md"}]}, self._exec()))
        with self.assertRaisesRegex(ValueError, "not a regular file"):
            asyncio.run(self.tool.execute(
                {"files": [{"path": "folder"}]}, self._exec()))
        with self.assertRaisesRegex(ValueError, "accepts 1 to"):
            asyncio.run(self.tool.execute({"files": []}, self._exec()))

    def test_present_requires_an_open_turn(self):
        other = Session("no-turn", meta={"cwd": self.work})
        loop = AgentLoop(other, FakeLlmAdapter(), self.registry, self.ctx)
        with self.assertRaisesRegex(ValueError, "requires an open turn"):
            asyncio.run(self.tool.execute(
                {"files": [{"path": "report.md"}]},
                ToolExec(agent=loop, name="present", call_id="p")))

    def test_v4_payload_validation_accepts_presented(self):
        assert_released_payload_semantics({
            "type": "deliverables/presented", "seq": 0,
            "data": {"turn": 1, "callId": "present-1",
                     "files": [{"path": "report.md", "description": "Final report"}]},
        }, 4)


if __name__ == "__main__":
    unittest.main()