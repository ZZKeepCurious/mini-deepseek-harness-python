"""PTC 模式 `run_code` 工具 + 子派发事件日志。

对照上游 `packages/core/tools/src/ptc.ts`（createRunCodeTool）：
程序经 `tools.<name>` 调用注册表工具，子派发落 `tool/ptc-dispatch-start` /
`tool/ptc-dispatch`，外层结果精心挑选。
"""
import unittest

from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.tools import Tool, ToolExec, ToolRegistry, ToolResult
from miniharness.ptc.run_code import (
    RUN_CODE_NAME,
    RunCodeFailedError,
    create_run_code_tool,
)
from miniharness.ptc_runtime import PythonPtcRuntime


class _Signal:
    aborted = False
    event = None

    def is_set(self):
        return False


def _registry_with_echo():
    ctx = Context(name="root")
    registry = ToolRegistry(ctx)

    def add(args, exec_):
        return ToolResult(ok=True, value={"sum": args["a"] + args["b"]})

    registry.register(Tool(name="add", description="add", parameters={}, execute=add))
    return ctx, registry


def _tool(runtime, registry, ctx, session, agent=None):
    tool = create_run_code_tool(registry, runtime=runtime, ctx=ctx, session=session)
    return tool


class RunCodeToolTest(unittest.TestCase):
    def setUp(self):
        self.session = Session("ptc")
        self.ctx, self.registry = _registry_with_echo()
        self.runtime = PythonPtcRuntime()
        self.tool = _tool(self.runtime, self.registry, self.ctx, self.session)

    def _exec(self):
        exec_ = ToolExec(agent=None, call_id="call-1", root_call_id="call-1")
        exec_.name = RUN_CODE_NAME
        exec_.signal = _Signal()
        return exec_

    def test_calls_registered_tool_and_returns_value(self):
        result = self.tool.execute({
            "code": "return await tools.add({'a': 2, 'b': 3})",
            "description": "add two numbers",
        }, self._exec())
        self.assertEqual(result["result"], {"sum": 5})

    def test_logs_dispatch_events(self):
        self.tool.execute({
            "code": "result = await tools.add({'a': 1, 'b': 1})\nreturn result",
            "description": "add",
        }, self._exec())
        types = [event["type"] for event in self.session.events]
        self.assertIn("tool/ptc-dispatch-start", types)
        self.assertIn("tool/ptc-dispatch", types)
        dispatch = next(e for e in self.session.events if e["type"] == "tool/ptc-dispatch")
        self.assertEqual(dispatch["data"]["name"], "add")
        self.assertEqual(dispatch["data"]["subCallId"], "call-1:ptc:1")
        self.assertFalse(dispatch["data"]["isError"])

    def test_program_exception_folds_to_code_run_failed(self):
        with self.assertRaises(RunCodeFailedError) as cm:
            self.tool.execute({
                "code": "raise ValueError('nope')",
                "description": "boom",
            }, self._exec())
        self.assertIn("code run failed (exception)", str(cm.exception))
        self.assertIn("nope", str(cm.exception))

    def test_binding_failure_reaches_program(self):
        result = self.tool.execute({
            "code": (
                "try:\n"
                "    await tools.missing({})\n"
                "except Exception as e:\n"
                "    return 'caught'\n"
                "return 'missed'"),
            "description": "missing tool",
        }, self._exec())
        self.assertEqual(result["result"], "caught")

    def test_render_curates_output(self):
        output = {"logs": ["printed"], "result": {"sum": 5}}
        blocks = self.tool.render({"description": "x"}, output)
        self.assertEqual(len(blocks), 1)
        self.assertIn("printed", blocks[0]["text"])
        self.assertIn("\"sum\": 5", blocks[0]["text"])

    def test_render_empty(self):
        blocks = self.tool.render({}, {"logs": []})
        self.assertEqual(blocks[0]["text"], "(run_code completed with no output)")

    def test_run_code_is_excluded_from_bindings(self):
        # 注册 run_code 自身后，程序里不应出现 tools.run_code 绑定
        self.registry.register(Tool(
            name=RUN_CODE_NAME, description="self", parameters={},
            execute=lambda args, e: ToolResult(ok=True, value={})))
        result = self.tool.execute({
            "code": (
                "try:\n"
                "    await tools.run_code({})\n"
                "except Exception:\n"
                "    return 'blocked'\n"
                "return 'leaked'"),
            "description": "self bind",
        }, self._exec())
        self.assertEqual(result["result"], "blocked")

    def test_invalid_description_rejected(self):
        with self.assertRaises(ValueError):
            self.tool.execute({"code": "return 1", "description": "  "}, self._exec())

    def test_invalid_timeout_rejected(self):
        with self.assertRaises(ValueError):
            self.tool.execute({"code": "return 1", "description": "x",
                               "timeoutMs": -5}, self._exec())

    def test_flavor_reflects_runtime_language(self):
        self.assertIn("Python program", self.tool.description)
        ts_runtime = _FakeTsRuntime()
        ts_tool = create_run_code_tool(self.registry, runtime=ts_runtime,
                                       ctx=self.ctx, session=self.session)
        self.assertIn("TypeScript program", ts_tool.description)

    def test_unknown_language_fails_loud(self):
        runtime = _FakeTsRuntime(language="rust")
        with self.assertRaises(ValueError):
            create_run_code_tool(self.registry, runtime=runtime,
                                 ctx=self.ctx, session=self.session)


class _FakeTsRuntime:
    def __init__(self, language="typescript"):
        self.language = language

    @property
    def timeout(self):
        return None

    @property
    def sandbox_mode(self):
        return None


class RunCodeImageDeferTest(unittest.TestCase):
    """成功的含图子调用结果 defer 为 ptc-mode user 消息（上游 ptc.ts:639-644）。"""

    def setUp(self):
        self.session = Session("ptc-image")
        self.ctx = Context(name="root")
        self.registry = ToolRegistry(self.ctx)

        def snap(args, exec_):
            return ToolResult(ok=True, content=[
                {"type": "text", "text": "shot"},
                {"type": "image", "attachment": {"attachmentId": "abc"}},
            ])

        def text_only(args, exec_):
            return ToolResult(ok=True, content=[{"type": "text", "text": "plain"}])

        def snap_error(args, exec_):
            return ToolResult(ok=False, is_error=True, error="snap failed", content=[
                {"type": "text", "text": "boom"},
            ])

        self.registry.register(
            Tool(name="snap", description="snap", parameters={}, execute=snap))
        self.registry.register(
            Tool(name="text_only", description="text_only",
                 parameters={}, execute=text_only))
        self.registry.register(
            Tool(name="snap_error", description="snap_error",
                 parameters={}, execute=snap_error))
        self.runtime = PythonPtcRuntime()
        self.tool = create_run_code_tool(self.registry, runtime=self.runtime,
                                         ctx=self.ctx, session=self.session)

    def _exec(self):
        exec_ = ToolExec(agent=None, call_id="call-1", root_call_id="call-1")
        exec_.name = RUN_CODE_NAME
        exec_.signal = _Signal()
        return exec_

    def test_image_result_deferred_as_ptc_mode(self):
        exec_ = self._exec()
        self.tool.execute({
            "code": "await tools.snap({})",
            "description": "screenshot",
        }, exec_)
        contexts = exec_.additional_contexts
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]["role"], "user")
        self.assertEqual(contexts[0]["source"], {"kind": "ptc-mode"})
        self.assertEqual(contexts[0]["content"], [
            {"type": "text", "text": "shot"},
            {"type": "image", "attachment": {"attachmentId": "abc"}},
        ])

    def test_text_only_result_not_deferred(self):
        exec_ = self._exec()
        self.tool.execute({
            "code": "await tools.text_only({})",
            "description": "plain",
        }, exec_)
        self.assertEqual(exec_.additional_contexts, [])

    def test_failed_image_result_not_deferred(self):
        exec_ = self._exec()
        with self.assertRaises(RunCodeFailedError):
            self.tool.execute({
                "code": "await tools.snap_error({})",
                "description": "boom",
            }, exec_)
        self.assertEqual(exec_.additional_contexts, [])


if __name__ == "__main__":
    unittest.main()
