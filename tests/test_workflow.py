"""workflow（M13 Python 功能对标）验收。

对齐上游 `packages/workflow/{workflow,workflow-ptc,tool-workflow}`：

- seam：validate_meta（违规聚合 META_INVALID）、WorkflowError 码闭集、
  is_fatal_workflow_error、WorkflowRunId、事件发射 contain
- 引擎：start 前置校验（META_INVALID/SCRIPT_PARSE/保留 meta 语句/provider/
  maxTotalAgents）、guest 六全局、并发槽/caps 逻辑、结果物化
- tool-workflow：工具 schema/渲染、记录事件、结局映射

载体差异（登记）：脚本语言 JS→Python；`structured` 子代理输出无载体
（schema 子代理按上游「缺 structured = 失败 → null」语义）。
"""
import asyncio
import tempfile
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.session.persistence import JsonlPersistence
from miniharness.core.session_store import install_sessions
from miniharness.core.tools import ToolRegistry
from miniharness.llm import FakeLlmAdapter
from miniharness.ptc_runtime import install_ptc_runtime
from miniharness.seams.subagent import SubagentContinuationManager
from miniharness.workflow import (
    WorkflowError,
    WorkflowRunId,
    is_fatal_workflow_error,
    validate_meta,
)
from miniharness.workflow_ptc import (
    PtcWorkflowEngine,
    PtcWorkflowRun,
    install_workflow_engine,
)

from tests.test_continuation import _parent_loop


def _parent():
    return _parent_loop(session_id="parent", adapter=FakeLlmAdapter(final_text="父响应"))


class WorkflowSeamTest(unittest.TestCase):
    def test_error_codes(self):
        error = WorkflowError("cap hit", "AGENT_CAP")
        self.assertEqual(error.code, "AGENT_CAP")
        self.assertTrue(error.fatal)
        self.assertTrue(is_fatal_workflow_error(error))
        self.assertFalse(is_fatal_workflow_error(RuntimeError("plain")))
        self.assertFalse(is_fatal_workflow_error("string"))
        non_fatal = WorkflowError("soft", "AGENT_RESULT", fatal=False)
        self.assertFalse(is_fatal_workflow_error(non_fatal))

    def test_validate_meta_ok(self):
        meta = validate_meta({"name": "audit", "description": "audit files",
                              "whenToUse": "large scans",
                              "phases": [{"title": "phase one", "detail": "d"}]})
        self.assertEqual(meta["name"], "audit")
        self.assertEqual(meta["phases"][0]["title"], "phase one")

    def test_validate_meta_aggregates(self):
        with self.assertRaises(WorkflowError) as raised:
            validate_meta({"name": "", "description": 3, "bogus": 1})
        self.assertEqual(raised.exception.code, "META_INVALID")
        self.assertIn("meta.name must be a non-empty string", str(raised.exception))
        self.assertIn("meta.description must be a non-empty string", str(raised.exception))
        self.assertIn("meta.bogus is not a recognized field", str(raised.exception))

    def test_validate_meta_phase_validation(self):
        with self.assertRaises(WorkflowError) as raised:
            validate_meta({"name": "x", "description": "y",
                           "phases": [{"title": ""}, {"detail": 1}]})
        self.assertIn("meta.phases[0].title must be a non-empty string", str(raised.exception))
        self.assertIn("meta.phases[1].detail must be a string", str(raised.exception))

    def test_run_id_brand(self):
        self.assertEqual(WorkflowRunId("abc"), "abc")


class WorkflowEngineStartTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent, self.ctx, self.reg = _parent()
        install_sessions(self.ctx)
        self.persistence = JsonlPersistence(self.tmp.name)
        self.manager = SubagentContinuationManager(self.parent, self.persistence)
        install_ptc_runtime(self.ctx)
        self.ctx.provide("subagents", self.manager)
        self.engine = install_workflow_engine(self.ctx, {"provider": "spawn"})

    def tearDown(self):
        self.ctx.dispose()

    def test_missing_runtime_fails(self):
        from miniharness.core.scope import Context as Ctx
        ctx2 = Ctx(name="no-ptc")
        try:
            parent, _, reg = _parent_loop(session_id="p2")
            with self.assertRaises(ValueError):
                PtcWorkflowEngine(ctx2, {})
        finally:
            ctx2.dispose()

    def test_meta_invalid(self):
        with self.assertRaises(WorkflowError) as raised:
            self.engine.start({"meta": {"name": ""}, "script": "return 1",
                               "parent": self.parent})
        self.assertEqual(raised.exception.code, "META_INVALID")

    def test_reserved_meta_statement(self):
        with self.assertRaises(WorkflowError) as raised:
            self.engine.start({"meta": {"name": "x", "description": "y"},
                               "script": "export const meta = {}",
                               "parent": self.parent})
        self.assertEqual(raised.exception.code, "SCRIPT_PARSE")

    def test_body_parse_error(self):
        with self.assertRaises(WorkflowError) as raised:
            self.engine.start({"meta": {"name": "x", "description": "y"},
                               "script": "def broken(:", "parent": self.parent})
        self.assertEqual(raised.exception.code, "SCRIPT_PARSE")

    def test_missing_parent(self):
        with self.assertRaises(WorkflowError):
            self.engine.start({"meta": {"name": "x", "description": "y"},
                               "script": "return 1"})

    def test_start_emits_workflow_start(self):
        events = []
        self.ctx.on("workflow/start", lambda info: events.append(info))
        run = self.engine.start({"meta": {"name": "audit", "description": "d"},
                                 "script": "return {'ok': True}", "parent": self.parent})
        self.assertIsInstance(run, PtcWorkflowRun)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["meta"]["name"], "audit")
        self.assertEqual(events[0]["id"], run.id)

    def test_max_total_agents_validation(self):
        with self.assertRaises(WorkflowError) as raised:
            self.engine.start({"meta": {"name": "x", "description": "y"},
                               "script": "return 1", "parent": self.parent,
                               "maxTotalAgents": 0})
        self.assertEqual(raised.exception.code, "INVALID_ARGUMENT")
        with self.assertRaises(WorkflowError):
            self.engine.start({"meta": {"name": "x", "description": "y"},
                               "script": "return 1", "parent": self.parent,
                               "maxTotalAgents": 99999})

    def test_guest_six_globals_isolated(self):
        # guest 程序在隔离环境里解析：六全局 + workflowHost 绑定 + args 注入
        guest = self.engine.guest_program()
        self.assertIn("async def agent(", guest)
        self.assertIn("async def parallel(", guest)
        self.assertIn("async def pipeline(", guest)
        self.assertIn("def phase(", guest)
        self.assertIn("def log(", guest)
        self.assertIn("args = init.get(\"args\")", guest)
        self.assertIn("workflowHost.begin", guest)

    def test_parallel_and_pipeline_semantics(self):
        # guest 的 parallel/pipeline 逻辑以纯函数验算（并发、drop item）
        script = (
            "results = await parallel([lambda: 1, lambda: 2])\n"
            "staged = await pipeline([1, 2], lambda prev, item, i: prev + item)\n"
            "return {'parallel': results, 'pipeline': staged}\n"
        )
        run = self.engine.start({"meta": {"name": "compute", "description": "d"},
                                 "script": script, "parent": self.parent})
        result = run.result()
        self.assertEqual(result["stopReason"], "completed")
        self.assertEqual(result["value"]["parallel"], [1, 2])
        self.assertEqual(result["value"]["pipeline"], [2, 4])

    def test_pipeline_drops_item_on_stage_throw(self):
        script = (
            "staged = await pipeline([1, 2, 3], lambda prev, item, i: "
            "(_ for _ in ()).throw(RuntimeError('boom')) if item == 2 else prev + item)\n"
            "return staged\n"
        )
        run = self.engine.start({"meta": {"name": "drop", "description": "d"},
                                 "script": script, "parent": self.parent})
        result = run.result()
        self.assertEqual(result["stopReason"], "completed")
        # item 2 的 stage 抛错 → 该 item 变 None，其余照常（item 3 prev=3 → 6）
        self.assertEqual(result["value"], [2, None, 6])


class WorkflowEngineAgentChildTest(unittest.TestCase):
    """agent() 绑定经 continuation manager 启动子代理并收首回合输出。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.parent, self.ctx, self.reg = _parent()
        install_sessions(self.ctx)
        self.persistence = JsonlPersistence(self.tmp.name)
        self.manager = SubagentContinuationManager(self.parent, self.persistence)
        install_ptc_runtime(self.ctx)
        self.ctx.provide("subagents", self.manager)
        self.engine = install_workflow_engine(self.ctx, {"provider": "spawn"})

    def tearDown(self):
        self.ctx.dispose()

    def test_agent_binding_runs_child_and_returns_output(self):
        events = []
        for name in ("workflow/agent-start", "workflow/agent-end"):
            self.ctx.on(name, lambda payload, n=name: events.append(
                (n, payload[1]["seq"], payload[1].get("outcome"))))
        script = (
            "text = await agent('Do a small thing', {'label': 'helper'})\n"
            "return {'text': text}\n"
        )
        run = self.engine.start({"meta": {"name": "one-agent", "description": "d"},
                                 "script": script, "parent": self.parent})
        result = run.result()
        self.assertEqual(result["stopReason"], "completed")
        self.assertIsInstance(result["value"], dict)
        # 子代理以 FakeLlmAdapter 返回 '任务完成。'
        self.assertIn("任务完成", result["value"].get("text", ""))
        self.assertGreaterEqual(result["agentsStarted"], 1)
        starts = [e for e in events if e[0] == "workflow/agent-start"]
        ends = [e for e in events if e[0] == "workflow/agent-end"]
        self.assertEqual(len(starts), 1)
        self.assertEqual(len(ends), 1)
        self.assertEqual(ends[0][2], "completed")

    def test_agent_schema_returns_null_without_structured(self):
        # mini 子代理无 structured 载体 → schema 子代理按上游「缺 structured =
        # 失败 → null」语义
        script = (
            "value = await agent('Do it', {'schema': {'type': 'object', "
            "'properties': {'a': {'type': 'integer'}}, 'required': ['a']}})\n"
            "return {'value': value}\n"
        )
        run = self.engine.start({"meta": {"name": "schema", "description": "d"},
                                 "script": script, "parent": self.parent})
        result = run.result()
        self.assertEqual(result["stopReason"], "completed")
        self.assertIsNone(result["value"]["value"])

    def test_agent_cap_fires(self):
        script = (
            "out = []\n"
            "for i in range(3):\n"
            "    out.append(await agent(f'Task {i}'))\n"
            "return out\n"
        )
        # 独立 ctx：幂等装配不能换配置
        from miniharness.core.scope import Context as Ctx
        from miniharness.ptc_runtime import install_ptc_runtime as _iptc
        ctx2 = Ctx(name="wf-cap")
        try:
            parent, _, reg = _parent_loop(session_id="cap-parent")
            install_sessions(ctx2)
            mgr2 = SubagentContinuationManager(parent, JsonlPersistence(self.tmp.name))
            _iptc(ctx2)
            ctx2.provide("subagents", mgr2)
            engine = install_workflow_engine(
                ctx2, {"provider": "spawn", "maxTotalAgents": 1})
            run = engine.start({"meta": {"name": "cap", "description": "d"},
                                "script": script, "parent": parent})
            result = run.result()
            self.assertEqual(result["stopReason"], "error")
            self.assertIn("total agent cap", result["error"])
        finally:
            ctx2.dispose()

    def test_cancel_stops_run(self):
        script = (
            "await agent('First')\n"
            "await agent('Second')\n"
            "return 'done'\n"
        )
        run = self.engine.start({"meta": {"name": "cancel", "description": "d"},
                                 "script": script, "parent": self.parent})
        run.cancel("user asked to stop")
        result = run.result()
        self.assertEqual(result["stopReason"], "cancelled")
        self.assertIn("user asked to stop", result["error"])


if __name__ == "__main__":
    unittest.main()