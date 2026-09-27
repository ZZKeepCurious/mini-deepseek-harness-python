"""tool-workflow（M13 模型工具）验收。

对齐上游 `packages/workflow/tool-workflow`：工具 schema（script/meta/args/
run_in_background）、前台/后台结局、渲染、durable 记录事件。
"""
import tempfile
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.session.persistence import JsonlPersistence
from miniharness.core.session_store import install_sessions
from miniharness.core.tools import ToolExec, ToolRegistry
from miniharness.jobs import install_jobs
from miniharness.llm import FakeLlmAdapter
from miniharness.ptc_runtime import install_ptc_runtime
from miniharness.seams.subagent import SubagentContinuationManager
from miniharness.tool_workflow import create_workflow_tool, resolve_config
from miniharness.workflow_ptc import install_workflow_engine

from tests.test_continuation import _parent_loop


class ToolWorkflowConfigTest(unittest.TestCase):
    def test_defaults(self):
        self.assertEqual(resolve_config(None), {
            "toolName": "workflow", "maxResultChars": 50_000,
            "enableRunInBackground": True})

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            resolve_config({"bogus": 1})

    def test_bad_max_chars(self):
        with self.assertRaises(ValueError):
            resolve_config({"maxResultChars": 0})


class ToolWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent, self.ctx, self.reg = _parent_loop(
            session_id="parent", adapter=FakeLlmAdapter(final_text="父响应"))
        install_sessions(self.ctx)
        self.persistence = JsonlPersistence(self.tmp.name)
        self.manager = SubagentContinuationManager(self.parent, self.persistence)
        install_ptc_runtime(self.ctx)
        self.ctx.provide("subagents", self.manager)
        install_workflow_engine(self.ctx, {"provider": "spawn"})
        self.tool = create_workflow_tool(self.ctx)
        self.reg.register(self.tool)

    def tearDown(self):
        self.ctx.dispose()

    def test_schema(self):
        self.assertEqual(self.tool.name, "workflow")
        self.assertIn("script", self.tool.parameters["properties"])
        self.assertIn("meta", self.tool.parameters["properties"])
        self.assertEqual(self.tool.parameters["required"], ["script", "meta"])

    def test_invalid_args(self):
        import asyncio
        exec_ = ToolExec(agent=self.parent)
        with self.assertRaises(ValueError):
            asyncio.run(self.tool.execute({"script": "", "meta": {}}, exec_))
        with self.assertRaises(ValueError):
            asyncio.run(self.tool.execute({"script": "return 1", "meta": {"name": ""}},
                                          exec_))

    def test_foreground_execute_records_events(self):
        import asyncio
        exec_ = ToolExec(agent=self.parent, parent=None)
        value = asyncio.run(self.tool.execute({
            "script": "return {'ok': True}",
            "meta": {"name": "audit", "description": "d"},
        }, exec_))
        self.assertEqual(value["kind"], "foreground")
        self.assertEqual(value["result"], {"ok": True})
        # durable 记录：run-start + run-end
        types = [e["type"] for e in self.parent.session.events
                 if e["type"].startswith("tool-workflow/")]
        self.assertEqual(types, ["tool-workflow/run-start", "tool-workflow/run-end"])

    def test_foreground_render(self):
        value = {"kind": "foreground", "runId": "r1", "agentsStarted": 0,
                 "result": {"findings": ["a", "b"]}}
        rendered = self.tool.render({"meta": {"name": "audit"}}, value)
        text = rendered[0]["text"]
        self.assertIn('workflow "audit" completed', text)
        self.assertIn('"findings"', text)


if __name__ == "__main__":
    unittest.main()