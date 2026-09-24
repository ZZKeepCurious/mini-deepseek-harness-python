"""M9 验收：ask_user_question 工具（interaction/tool_ask_user.py）。

镜像上游 packages/interaction/tool-ask-user/tests/tool-ask-user.spec.ts：
schema 形状（items 属性/选项属性/required 数组）、提问委托 + canonical 投影、
render 紧凑 JSON（对齐 JSON.stringify）、signal 透传、root 调用方透传、
结构化错误（NO_PROVIDER / DELEGATED_CALLER / EMPTY_QUESTIONS）、
无服务时不注册。
"""

import asyncio
import json
import unittest
from types import SimpleNamespace

from miniharness.core.agents import install_agents
from miniharness.core.dsh_scope import scope_target
from miniharness.core.scope import Context
from miniharness.core.tools import ToolExec, ToolRegistry, run_pipeline_async
from miniharness.interaction import (
    ASK_USER_QUESTION,
    DELEGATED_CALLER,
    EMPTY_QUESTIONS,
    NO_PROVIDER,
    register_ask_user_question,
    install_user_questions,
)

PROVIDER_ANSWERS = {"answers": [{"id": "pkg", "selected": ["pnpm"]}]}


def _run(coro):
    return asyncio.run(coro)


class _StubAgent:
    """测试用 stub agent：满足 AgentRegistry.register 的必须字段。"""

    def __init__(self, ctx, id_):
        self.id = id_
        self.session = SimpleNamespace(session_id=id_)
        self.scope = ctx.create_scope(f"agent:{id_}")
        self.ctx = self.scope
        self._carrier = scope_target(self, self.scope.scope_key)


def _setup():
    ctx = Context(name="t")
    install_agents(ctx)
    service = install_user_questions(ctx)
    reg = ToolRegistry(ctx)
    registered = register_ask_user_question(reg, ctx)
    return ctx, service, reg, registered


def _tool_run(ctx, reg, args, exec_=None):
    return _run(run_pipeline_async(ctx, reg.resolve(ASK_USER_QUESTION), args,
                                   exec_ or ToolExec()))


class SchemaShapeTest(unittest.TestCase):
    def test_question_and_option_properties(self):
        ctx, _service, reg, _tool = _setup()
        self.addCleanup(ctx.dispose)
        tool = reg.resolve(ASK_USER_QUESTION)
        props = tool.parameters["properties"]["questions"]["items"]["properties"]
        self.assertEqual(set(props), {"id", "question", "header", "options", "multi_select"})
        # mini schema 与上游 tool-ask-user/src/index.ts:31-54 逐字一致：properties
        # 恰为 id/question/header/options/multi_select，上游同样没有 value/
        # recommended/preview 等额外信号参数（回归护栏，防止误加非上游字段）。
        self.assertNotIn("value", props)
        self.assertNotIn("recommended", props)
        self.assertNotIn("preview", props)
        option_props = props["options"]["items"]["properties"]
        self.assertEqual(set(option_props), {"label", "description"})
        self.assertEqual(tool.parameters["required"], ["questions"])
        self.assertEqual(props["id"]["type"], "string")
        self.assertEqual(props["question"]["type"], "string")

    def test_not_registered_without_service(self):
        ctx = Context(name="t")
        self.addCleanup(ctx.dispose)
        reg = ToolRegistry(ctx)
        self.assertIsNone(register_ask_user_question(reg, ctx))


class ToolExecutionTest(unittest.TestCase):
    def setUp(self):
        self.ctx, self.service, self.reg, self.tool = _setup()
        self.observed = {}

        async def answerer(request, nxt=None):
            self.observed["request"] = request
            return PROVIDER_ANSWERS

        self.ctx.on("user-questions/request", answerer)

    def tearDown(self):
        self.ctx.dispose()

    def _answer(self, **overrides):
        args = _question_args()
        result = _tool_run(self.ctx, self.reg, args)
        return result, self.observed["request"], args

    def test_asks_provider_and_projects_structured_answers(self):
        result, request, _args = self._answer()
        self.assertTrue(result.ok)
        self.assertEqual(result.value, PROVIDER_ANSWERS)
        # canonical → 模型可见字符串 = 紧凑 JSON（对齐 JSON.stringify；mini
        # content 载体为 str，emit 不再包 repr）
        self.assertEqual(result.content,
                         json.dumps(PROVIDER_ANSWERS, separators=(",", ":"), ensure_ascii=False))
        question = request["questions"][0]
        self.assertEqual(question["id"], "pkg")
        self.assertEqual(question["question"], "Which package manager?")
        self.assertEqual([o["label"] for o in question["options"]],
                         ["pnpm", "npm"])
        self.assertNotIn("agent", request)

    def test_recommended_label_passes_through_unaltered(self):
        args = {"questions": [{
            "id": "pkg",
            "question": "Which?",
            "options": [
                {"label": "pnpm (Recommended)", "description": "fast"},
                {"label": "npm", "description": "default"},
            ],
        }]}

        async def pick_recommended(request, nxt=None):
            return {"answers": [{"id": "pkg", "selected": ["pnpm (Recommended)"]}]}

        # prepend：在 setUp 默认应答者之前短路，不触碰默认即可不派发到它
        self.ctx.on("user-questions/request", pick_recommended, prepend=True)
        result = _tool_run(self.ctx, self.reg, args)
        self.assertEqual(result.value["answers"][0]["selected"],
                         ["pnpm (Recommended)"])

    def test_custom_and_multi_select_projection(self):
        args = {"questions": [
            {"id": "q1", "question": "Target?", "multi_select": True},
        ]}
        _tool_run(self.ctx, self.reg, args)
        # 参数投影：multi_select → multiSelect（上游 execute 的 questions.map）
        self.assertEqual(self.observed["request"]["questions"][0]["multiSelect"], True)

        self.observed.clear()

        async def custom_answerer(request, nxt=None):
            return {"answers": [{"id": "q1", "selected": ["other"], "custom": "some text"}]}

        # answer 携 custom → execute 透传（上游 out 投影）
        self.ctx.on("user-questions/request", custom_answerer, prepend=True)
        result = _tool_run(self.ctx, self.reg, args)
        self.assertEqual(result.value["answers"][0],
                         {"id": "q1", "selected": ["other"], "custom": "some text"})

    def test_signal_passed_through(self):
        signal = SimpleNamespace(aborted=False)
        result = _tool_run(self.ctx, self.reg, _question_args(),
                           ToolExec(signal=signal))
        self.assertIs(self.observed["request"]["signal"], signal)

    def test_optional_header_and_root_agent_pass_through(self):
        root = _StubAgent(self.ctx, "root")
        self.ctx.get("agents").register(root)
        args = _question_args(header="Confirm")
        result = _tool_run(self.ctx, self.reg, args, ToolExec(agent=root))
        question = self.observed["request"]["questions"][0]
        self.assertEqual(question["header"], "Confirm")
        self.assertIs(self.observed["request"]["agent"], root)


class StructuredErrorTest(unittest.TestCase):
    def _setup_ctx(self):
        ctx = Context(name="t")
        install_agents(ctx)
        install_user_questions(ctx)
        reg = ToolRegistry(ctx)
        register_ask_user_question(reg, ctx)
        return ctx, reg

    def test_no_provider_is_structured_error(self):
        ctx, reg = self._setup_ctx()
        self.addCleanup(ctx.dispose)
        result = _tool_run(ctx, reg, _question_args())
        self.assertTrue(result.is_error)
        self.assertEqual(result.error_info,
                         {"name": "UserQuestionError", "code": NO_PROVIDER})
        self.assertEqual(result.error,
                         f"Error: no user-questions answerer accepted the request")

    def test_delegated_caller_is_structured_error(self):
        ctx, reg = self._setup_ctx()
        self.addCleanup(ctx.dispose)
        agents = ctx.get("agents")
        root = _StubAgent(ctx, "root")
        child = _StubAgent(ctx, "child")
        agents.register(root)
        agents.register(child, owner=root)
        result = _tool_run(ctx, reg, _question_args(), ToolExec(agent=child))
        self.assertTrue(result.is_error)
        self.assertEqual(result.error_info,
                         {"name": "UserQuestionError", "code": DELEGATED_CALLER})
        self.assertIn("human interaction is unavailable", result.error)

    def test_empty_batch_is_structured_error(self):
        ctx, reg = self._setup_ctx()
        self.addCleanup(ctx.dispose)
        result = _tool_run(ctx, reg, {"questions": []})
        self.assertTrue(result.is_error)
        self.assertEqual(result.error_info,
                         {"name": "UserQuestionError", "code": EMPTY_QUESTIONS})
        self.assertEqual(result.error,
                         "Error: ask_user_question requires at least one question")


def _question_args(header=None, **overrides):
    question = {
        "id": "pkg",
        "question": "Which package manager?",
        "options": [
            {"label": "pnpm", "description": "fast"},
            {"label": "npm", "description": "default"},
        ],
    }
    if header is not None:
        question["header"] = header
    question.update(overrides)
    return {"questions": [question]}


if __name__ == "__main__":
    unittest.main()