"""M9 验收：UserQuestionService 核心契约（interaction/user_questions.py）。

镜像上游 packages/interaction/user-questions/tests/user-questions.spec.ts：
代理委托、无应答者 NO_PROVIDER、HMR 双 dispose、组合委托、signal abort 竞态
（入口中止 / 在航中止归一化 / 域拒绝保留）、传输恢复、批量前置、调用者身份
校验（CALLER_NOT_LIVE / DELEGATED_CALLER / resumed-root）、BAD_INTENT。
"""

import asyncio
import threading
import unittest
from types import SimpleNamespace

from miniharness.core.agents import install_agents
from miniharness.core.dsh_scope import scope_target
from miniharness.core.scope import Context
from miniharness.core.session_store import install_sessions
from miniharness.interaction import (
    ASK_ABORTED,
    ASK_CANCELLED,
    BAD_INTENT,
    CALLER_NOT_LIVE,
    DELEGATED_CALLER,
    EMPTY_QUESTIONS,
    NO_PROVIDER,
    UserQuestionError,
    install_user_questions,
)

PROVIDER = {"answers": [{"id": "q1", "selected": ["ok"]}]}


def _run(coro):
    return asyncio.run(coro)


def _service(ctx):
    return install_user_questions(ctx)


class _StubAgent:
    """测试用 stub agent：满足 AgentRegistry.register 的必须字段。"""

    def __init__(self, ctx, id_):
        self.id = id_
        self.session = SimpleNamespace(session_id=id_)
        self.scope = ctx.create_scope(f"agent:{id_}")
        self.ctx = self.scope
        self._carrier = scope_target(self, self.scope.scope_key)


def _install_agent(ctx, id_, owner=None):
    agents = ctx.get("agents") or install_agents(ctx)
    agent = _StubAgent(ctx, id_)
    agents.register(agent, owner=owner)
    return agent


def _answerer(ctx, fn=None):
    """注册 async answerer 到 root 瀑布；返回 (disposer, observed)。

    fn(request, nxt) -> value/coro；缺省返回 PROVIDER。
    """
    observed = {}

    async def listen(request, nxt=None):
        observed["request"] = request
        if fn is None:
            return PROVIDER
        return fn(request, nxt)

    disposer = ctx.on("user-questions/request", listen)
    return disposer, observed


class AskPipelineTest(unittest.TestCase):
    def _service(self):
        self.ctx = Context(name="t")
        self.addCleanup(self.ctx.dispose)
        return _service(self.ctx)

    def test_delegates_ask_to_provider(self):
        service = self._service()
        _, observed = _answerer(self.ctx)
        out = _run(service.ask({"questions": [{"id": "q1", "question": "ok?"}]}))
        self.assertEqual(out, PROVIDER)
        self.assertEqual(observed["request"]["questions"][0]["id"], "q1")
        # 无 agent 时请求原样透传（不深带 agent）
        self.assertNotIn("agent", observed["request"])

    def test_delegates_with_agent_carrier(self):
        service = self._service()
        agent = _install_agent(self.ctx, "r1")
        _, observed = _answerer(self.ctx)
        out = _run(service.ask({"questions": [{"id": "q1", "question": "ok?"}], "agent": agent}))
        self.assertEqual(out, PROVIDER)
        self.assertIs(observed["request"]["agent"], agent)

    def test_no_provider_when_no_answerer(self):
        service = self._service()
        with self.assertRaises(UserQuestionError) as cm:
            _run(service.ask({"questions": [{"id": "q1", "question": "ok?"}]}))
        self.assertEqual(cm.exception.code, NO_PROVIDER)

    def test_hmr_safe_double_dispose(self):
        service = self._service()
        disposer, _ = _answerer(self.ctx)
        disposer()
        disposer()
        with self.assertRaises(UserQuestionError):
            _run(service.ask({"questions": [{"id": "q1", "question": "ok?"}]}))

    def test_delegates_through_composed_answerers(self):
        service = self._service()
        calls = []

        async def delegator(request, nxt):
            calls.append("delegator")
            return await nxt()

        async def provider(request, nxt=None):
            calls.append("provider")
            return PROVIDER

        self.ctx.on("user-questions/request", delegator)
        self.ctx.on("user-questions/request", provider)
        out = _run(service.ask({"questions": [{"id": "q1", "question": "ok?"}]}))
        self.assertEqual(out, PROVIDER)
        self.assertEqual(calls, ["delegator", "provider"])

    def test_fails_before_reaching_provider_when_signal_aborted(self):
        service = self._service()
        _, observed = _answerer(self.ctx)
        signal = SimpleNamespace(aborted=True)
        with self.assertRaises(UserQuestionError) as cm:
            _run(service.ask({"questions": [{"id": "q1", "question": "ok?"}], "signal": signal}))
        self.assertEqual(cm.exception.code, ASK_ABORTED)
        self.assertNotIn("request", observed)

    def test_in_flight_signal_abort_normalizes_to_ask_aborted(self):
        service = self._service()
        signal = threading.Event()

        async def provider(request, nxt=None):
            signal.set()
            raise RuntimeError("provider failed")

        self.ctx.on("user-questions/request", provider)
        with self.assertRaises(UserQuestionError) as cm:
            _run(service.ask({"questions": [{"id": "q1", "question": "ok?"}], "signal": signal}))
        self.assertEqual(cm.exception.code, ASK_ABORTED)
        self.assertIsInstance(cm.exception.cause, RuntimeError)

    def test_preserves_domain_rejection_when_provider_aborts(self):
        service = self._service()
        signal = threading.Event()

        async def provider(request, nxt=None):
            signal.set()
            raise UserQuestionError("the user cancelled ask_user_question", ASK_CANCELLED)

        self.ctx.on("user-questions/request", provider)
        with self.assertRaises(UserQuestionError) as cm:
            _run(service.ask({"questions": [{"id": "q1", "question": "ok?"}], "signal": signal}))
        self.assertEqual(cm.exception.code, ASK_CANCELLED)

    def test_restores_transported_provider_rejection(self):
        service = self._service()

        async def provider(request, nxt=None):
            error = RuntimeError("the user cancelled ask_user_question")
            error.name = "UserQuestionError"
            error.code = "ASK_CANCELLED"
            error.message = "the user cancelled ask_user_question"
            raise error

        self.ctx.on("user-questions/request", provider)
        with self.assertRaises(UserQuestionError) as cm:
            _run(service.ask({"questions": [{"id": "q1", "question": "ok?"}]}))
        self.assertEqual(cm.exception.code, ASK_CANCELLED)
        self.assertEqual(str(cm.exception), "the user cancelled ask_user_question")

    def test_preserves_ordinary_error_from_provider(self):
        service = self._service()
        sentinel = RuntimeError("provider failed")

        async def provider(request, nxt=None):
            raise sentinel

        self.ctx.on("user-questions/request", provider)
        with self.assertRaises(RuntimeError) as cm:
            _run(service.ask({"questions": [{"id": "q1", "question": "ok?"}]}))
        self.assertIs(cm.exception, sentinel)

    def test_preserves_namesake_error_without_code(self):
        service = self._service()

        async def provider(request, nxt=None):
            error = RuntimeError("provider failed")
            error.name = "UserQuestionError"
            raise error

        self.ctx.on("user-questions/request", provider)
        with self.assertRaises(RuntimeError):
            _run(service.ask({"questions": [{"id": "q1", "question": "ok?"}]}))

    def test_rejects_empty_batch(self):
        service = self._service()
        with self.assertRaises(UserQuestionError) as cm:
            _run(service.ask({"questions": []}))
        self.assertEqual(cm.exception.code, EMPTY_QUESTIONS)


class CallerValidationTest(unittest.TestCase):
    def _service(self):
        self.ctx = Context(name="t")
        self.addCleanup(self.ctx.dispose)
        return _service(self.ctx)

    def test_reaches_provider_from_root_agent(self):
        service = self._service()
        agent = _install_agent(self.ctx, "resumed-root")
        _, observed = _answerer(self.ctx)
        _run(service.ask({"questions": [{"id": "q1", "question": "ok?"}], "agent": agent}))
        self.assertEqual(observed["request"]["questions"][0]["id"], "q1")

    def test_fails_for_delegated_caller(self):
        service = self._service()
        root = _install_agent(self.ctx, "root")
        child = _install_agent(self.ctx, "child", owner=root)
        with self.assertRaises(UserQuestionError) as cm:
            _run(service.ask({"questions": [{"id": "q1", "question": "ok?"}], "agent": child}))
        self.assertEqual(cm.exception.code, DELEGATED_CALLER)

    def test_caller_not_live_for_unattested(self):
        service = self._service()
        agent = _StubAgent(self.ctx, "unattested")
        with self.assertRaises(UserQuestionError) as cm:
            _run(service.ask({"questions": [{"id": "q1", "question": "ok?"}], "agent": agent}))
        self.assertEqual(cm.exception.code, CALLER_NOT_LIVE)

    def test_caller_not_live_for_stale_id(self):
        service = self._service()
        _install_agent(self.ctx, "shared-id")
        stale = _StubAgent(self.ctx, "shared-id")
        with self.assertRaises(UserQuestionError) as cm:
            _run(service.ask({"questions": [{"id": "q1", "question": "ok?"}], "agent": stale}))
        self.assertEqual(cm.exception.code, CALLER_NOT_LIVE)


class IntentValidationTest(unittest.TestCase):
    def _service(self):
        self.ctx = Context(name="t")
        self.addCleanup(self.ctx.dispose)
        return _service(self.ctx)

    def test_bad_intent_approve_label_names_no_option(self):
        service = self._service()
        question = {"id": "q1", "question": "X",
                    "intent": {"kind": "ki", "approve": "Nope"}}
        with self.assertRaises(UserQuestionError) as cm:
            _run(service.ask({"questions": [question]}))
        self.assertEqual(cm.exception.code, BAD_INTENT)
        self.assertIn("names none of its options", str(cm.exception))

    def test_bad_intent_without_detail(self):
        service = self._service()
        question = {"id": "q1", "question": "X",
                    "options": [{"label": "Approve"}, {"label": "Keep"}],
                    "intent": {"kind": "plan-review", "approve": "Approve"}}
        with self.assertRaises(UserQuestionError) as cm:
            _run(service.ask({"questions": [question]}))
        self.assertEqual(cm.exception.code, BAD_INTENT)
        self.assertIn("without the detail", str(cm.exception))

    def test_valid_intent_passes_through(self):
        service = self._service()
        _, observed = _answerer(self.ctx)
        question = {"id": "q1", "question": "X", "detail": "plan",
                    "options": [{"label": "Approve"}, {"label": "Keep"}],
                    "intent": {"kind": "plan-review", "approve": "Approve"}}
        out = _run(service.ask({"questions": [question]}))
        self.assertEqual(out, PROVIDER)
        self.assertEqual(observed["request"]["questions"][0]["intent"]["approve"], "Approve")


class InstallTest(unittest.TestCase):
    def test_install_is_idempotent(self):
        ctx = Context(name="t")
        self.addCleanup(ctx.dispose)
        first = install_user_questions(ctx)
        second = install_user_questions(ctx)
        self.assertIs(first, second)
        self.assertIs(ctx.get("userQuestions"), first)


if __name__ == "__main__":
    unittest.main()