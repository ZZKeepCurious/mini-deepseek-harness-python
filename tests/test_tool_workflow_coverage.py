"""tool-workflow 覆盖率补充：围绕 `miniharness/tool_workflow/__init__.py` 的
参数校验、结局映射、前台/后台执行、渲染与安装路径，用真实依赖（FakeLlmAdapter
驱动的子代理、真实 jobs 注册表、真实 workflow 引擎）驱动，不使用 mock。
"""
import asyncio
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.core.session.persistence import JsonlPersistence
from miniharness.core.session_store import install_sessions
from miniharness.core.tools import ToolExec
from miniharness.jobs import install_jobs
from miniharness.llm import FakeLlmAdapter
from miniharness.ptc_runtime import install_ptc_runtime
from miniharness.seams.subagent import SubagentContinuationManager
from miniharness.tool_workflow import (
    _render_result,
    _stop_reason_error,
    _validate_args,
    create_workflow_tool,
    install_tool_workflow,
    resolve_config,
)
from miniharness.workflow_ptc import install_workflow_engine

from tests.test_continuation import _parent_loop


class ToolWorkflowHelpersTest(unittest.TestCase):
    def test_resolve_config_rejects_unknown_key(self):
        with self.assertRaises(ValueError):
            resolve_config({"bogus": 1})

    def test_resolve_config_rejects_bad_max_chars(self):
        with self.assertRaises(ValueError):
            resolve_config({"maxResultChars": 0})

    def test_validate_args_rejects_empty_script(self):
        with self.assertRaises(ValueError):
            _validate_args({"script": "   ", "meta": {"name": "n"}})

    def test_validate_args_rejects_empty_meta_field(self):
        with self.assertRaises(ValueError):
            _validate_args({"script": "return 1", "meta": {"name": ""}})

    def test_validate_args_meta_not_object(self):
        with self.assertRaises(ValueError):
            _validate_args({"script": "return 1", "meta": "not-an-object"})

    def test_render_result_truncates_long_value(self):
        text = _render_result("audit", 2, {"blob": "x" * 400}, 24)
        self.assertIn('workflow "audit" completed (2 agent(s))', text)
        self.assertIn("[truncated:", text)

    def test_render_result_keeps_short_value(self):
        text = _render_result("audit", 0, {"ok": True}, 50_000)
        self.assertIn('"ok": true', text)
        self.assertNotIn("truncated", text)

    def test_stop_reason_completed(self):
        self.assertIsNone(_stop_reason_error({"stopReason": "completed"}))

    def test_stop_reason_cancelled_with_and_without_error(self):
        self.assertEqual(
            _stop_reason_error({"stopReason": "cancelled", "error": "user asked"}),
            "workflow run was cancelled (user asked)")
        self.assertEqual(_stop_reason_error({"stopReason": "cancelled"}),
                         "workflow run was cancelled")

    def test_stop_reason_error_with_and_without_message(self):
        self.assertEqual(
            _stop_reason_error({"stopReason": "error", "error": "boom"}),
            "workflow run failed: boom")
        self.assertEqual(_stop_reason_error({"stopReason": "error"}),
                         "workflow run failed: unknown error")

    def test_stop_reason_abnormal(self):
        self.assertEqual(_stop_reason_error({"stopReason": "weird"}),
                         "workflow run ended abnormally (weird)")


class ToolWorkflowExecuteCoverageTest(unittest.TestCase):
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
        self.engine = install_workflow_engine(self.ctx, {"provider": "spawn"})
        install_jobs(self.ctx)
        self.tool = create_workflow_tool(self.ctx)
        self.reg.register(self.tool)
        self.addCleanup(self.ctx.dispose)

    def _events(self):
        return [e["type"] for e in self.parent.session.events
                if e["type"].startswith("tool-workflow/")]

    def test_foreground_missing_agent_raises(self):
        exec_ = ToolExec(agent=None, parent=None)
        with self.assertRaises(RuntimeError):
            asyncio.run(self.tool.execute(
                {"script": "return 1", "meta": {"name": "n", "description": "d"}},
                exec_))

    def test_foreground_nested_exec_skips_durable_records(self):
        exec_ = ToolExec(agent=self.parent, parent=object())
        value = asyncio.run(self.tool.execute(
            {"script": "return {'ok': 1}", "meta": {"name": "nested", "description": "d"}},
            exec_))
        self.assertEqual(value["kind"], "foreground")
        self.assertEqual(value["result"], {"ok": 1})
        self.assertEqual(self._events(), [])

    def test_foreground_fatal_error_raises_and_records_end(self):
        exec_ = ToolExec(agent=self.parent, parent=None)
        with self.assertRaises(RuntimeError):
            asyncio.run(self.tool.execute(
                {"script": "await agent('x', {'effort': 'high'})\nreturn 1",
                 "meta": {"name": "fatal", "description": "d"}},
                exec_))
        events = self._events()
        self.assertIn("tool-workflow/run-start", events)
        self.assertIn("tool-workflow/run-end", events)

    def test_background_disabled_raises(self):
        tool = create_workflow_tool(self.ctx, {"enableRunInBackground": False})
        self.assertNotIn("run_in_background", tool.parameters["properties"])
        exec_ = ToolExec(agent=self.parent, parent=None)
        with self.assertRaises(RuntimeError):
            asyncio.run(tool.execute(
                {"script": "return 1", "meta": {"name": "n", "description": "d"},
                 "run_in_background": True}, exec_))

    def test_background_jobs_unavailable_raises(self):
        ctx2 = Context(name="wf-nojobs")
        self.addCleanup(ctx2.dispose)
        install_sessions(ctx2)
        install_ptc_runtime(ctx2)
        ctx2.provide("subagents", self.manager)
        install_workflow_engine(ctx2, {"provider": "spawn"})
        tool = create_workflow_tool(ctx2)
        exec_ = ToolExec(agent=self.parent, parent=None)
        with self.assertRaises(RuntimeError):
            asyncio.run(tool.execute(
                {"script": "return 1", "meta": {"name": "n", "description": "d"},
                 "run_in_background": True}, exec_))

    def test_render_background_kind(self):
        rendered = self.tool.render(
            {"meta": {"name": "bg"}},
            {"kind": "background", "jobId": "workflow-1", "runId": "r1"})
        self.assertIn("started in the background", rendered[0]["text"])
        self.assertIn("workflow-1", rendered[0]["text"])

    def test_render_foreground_kind(self):
        rendered = self.tool.render(
            {"meta": {"name": "fg"}},
            {"kind": "foreground", "agentsStarted": 2, "result": {"ok": True}})
        self.assertIn('workflow "fg" completed (2 agent(s))', rendered[0]["text"])

    def test_render_unknown_kind(self):
        rendered = self.tool.render(
            {"meta": {"name": "x"}},
            {"kind": "mystery", "agentsStarted": 1, "result": {"a": 1}})
        self.assertIn('workflow "x" completed', rendered[0]["text"])

    def test_present_call_and_result(self):
        call = self.tool.present_call(
            {"meta": {"name": "audit"}, "script": "return 1"})
        self.assertEqual(call["title"], "workflow: audit")
        self.assertEqual(call["rawInput"], "return 1")
        self.assertEqual(self.tool.present_result({"meta": {"name": "audit"}},
                                                 {"kind": "foreground"}),
                         {"card": "generic"})


class ToolWorkflowInstallCoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent, self.ctx, self.reg = _parent_loop(
            session_id="parent", adapter=FakeLlmAdapter(final_text="父响应"))
        self.addCleanup(self.ctx.dispose)

    def test_install_without_engine_returns_none(self):
        self.assertIsNone(install_tool_workflow(Context(name="bare")))

    def test_install_creates_registry_when_absent(self):
        ctx2 = Context(name="no-tools")
        self.addCleanup(ctx2.dispose)
        install_sessions(ctx2)
        install_ptc_runtime(ctx2)
        ctx2.provide("subagents",
                     SubagentContinuationManager(self.parent,
                                                 JsonlPersistence(self.tmp.name)))
        install_workflow_engine(ctx2, {"provider": "spawn"})
        self.assertIsNone(ctx2.get("tools"))
        tool = install_tool_workflow(ctx2)
        self.assertIsNotNone(tool)
        self.assertIsNotNone(ctx2.get("tools"))

    def test_install_registers_into_existing_registry(self):
        install_sessions(self.ctx)
        install_ptc_runtime(self.ctx)
        self.ctx.provide("subagents",
                         SubagentContinuationManager(self.parent,
                                                     JsonlPersistence(self.tmp.name)))
        install_workflow_engine(self.ctx, {"provider": "spawn"})
        self.assertIsNotNone(self.ctx.get("tools"))
        tool = install_tool_workflow(self.ctx)
        self.assertIsNotNone(tool)
        self.assertIn("workflow", self.ctx.get("tools").names())


if __name__ == "__main__":
    unittest.main()
