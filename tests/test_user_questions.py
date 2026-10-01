"""M9 验收：UserQuestionService 核心契约（interaction/user_questions.py）。

镜像上游 packages/interaction/user-questions/tests/user-questions.spec.ts：
代理委托、无应答者 NO_PROVIDER、HMR 双 dispose、组合委托、signal abort 竞态
（入口中止 / 在航中止归一化 / 域拒绝保留）、传输恢复、批量前置、调用者身份
校验（CALLER_NOT_LIVE / DELEGATED_CALLER / resumed-root）、BAD_INTENT。
"""

import asyncio
import json
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
    BAD_ANSWER,
    BAD_INTENT,
    BAD_TIMEOUT,
    CALLER_NOT_LIVE,
    DELEGATED_CALLER,
    DUPLICATE_WAIT,
    EMPTY_QUESTIONS,
    NO_PROVIDER,
    REPLY_QUEUED,
    UserQuestionError,
    fold_user_questions,
    install_user_questions,
)
from miniharness.session_projection import install_session_projections

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


class _Inbox:
    def __init__(self):
        self.next_turn = []
        self.next_step = []


class _TimedAgent:
    """满足 AgentRegistry + answer/steer 契约的测试 agent。"""

    def __init__(self, ctx, id_, session):
        self.id = id_
        self.session = session
        self.scope = ctx.create_scope(f"agent:{id_}")
        self.ctx = self.scope
        self._carrier = scope_target(self, self.scope.scope_key)
        self.inbox = _Inbox()
        self.steered = []

    def steer(self, message):
        self.steered.append(message)
        self.inbox.next_step.append(message)


class _TimedFixture:
    def __init__(self, name="timed"):
        self.ctx = Context(name=name)
        install_agents(self.ctx)
        self.store = install_sessions(self.ctx)
        self.registry = install_session_projections(self.ctx)
        self.service = install_user_questions(self.ctx)
        self.session = self.store.create("s-timed", {"meta": {}})
        self.agent = _TimedAgent(self.ctx, "s-timed", self.session)
        self.ctx.get("agents").register(self.agent)

    def dispose(self):
        self.ctx.dispose()

    def seed(self, *, timed=True, pending=True):
        tools = [{"name": "ask_user_question",
                  "parameters": {"properties": ({"timeout": {"type": "integer"}}
                                                 if timed else {})}}]
        self.session.append("request/header", {
            "header": {"tools": tools}, "reason": "initial"})
        self.session.append("tool/call", {
            "callId": "c1", "name": "ask_user_question",
            "arguments": json.dumps({"questions": [
                {"id": "q1", "question": "Continue?"}]})})
        if pending:
            content = json.dumps({"pending": True, "callId": "c1", "message": "pending"})
        else:
            content = json.dumps({"answers": [{"id": "q1", "selected": ["ok"]}]})
        self.session.append("tool/result", {"message": {
            "role": "tool", "toolCallId": "c1",
            "source": {"kind": "tool", "callId": "c1"},
            "content": [{"type": "text", "text": content}], "isError": False}},
            surfaceOp="append")


class UserQuestionProjectionTest(unittest.TestCase):
    def setUp(self):
        self.fx = _TimedFixture()
        self.addCleanup(self.fx.dispose)

    def test_timed_pending_call_stays_answerable(self):
        self.fx.seed(timed=True, pending=True)
        view = fold_user_questions(list(self.fx.session.events))
        self.assertEqual(len(view["active"]), 1)
        self.assertEqual(view["active"][0]["callId"], "c1")
        self.assertEqual(view["active"][0]["state"], "continued")
        self.assertEqual(view["settled"], [])

    def test_legacy_schema_call_is_never_tracked(self):
        self.fx.seed(timed=False, pending=True)
        view = fold_user_questions(list(self.fx.session.events))
        self.assertEqual(view, {"active": [], "settled": []})

    def test_answer_in_window_settles_with_batch(self):
        self.fx.seed(timed=True, pending=False)
        view = fold_user_questions(list(self.fx.session.events))
        self.assertEqual(view["active"], [])
        self.assertEqual(view["settled"],
                         [{"callId": "c1",
                           "answers": [{"id": "q1", "selected": ["ok"]}]}])

    def test_late_reply_settles_continued_question(self):
        self.fx.seed(timed=True, pending=True)
        self.fx.session.append("user/message", {
            "id": "m1", "role": "user", "source": {
                "kind": "user-question-reply", "callId": "c1", "outcome": "answered"},
            "content": [{"type": "text", "text": json.dumps(
                {"answers": [{"id": "q1", "selected": ["late"]}]})}]},
            surfaceOp="append")
        view = fold_user_questions(list(self.fx.session.events))
        self.assertEqual(view["active"], [])
        self.assertEqual(view["settled"][0]["answers"],
                         [{"id": "q1", "selected": ["late"]}])

    def test_registry_state_version_two(self):
        definition = self.fx.registry._registrations["userQuestions"]["def"]
        self.assertEqual(definition.state_version, 2)


class PtcQuestionProjectionTest(unittest.TestCase):
    """上游 projection.spec.ts:171-193：PTC 子调用的 pending 提问追踪。

    `tool/ptc-dispatch` 的 arguments 是已解析对象（spec:175
    `JSON.parse(toolArguments)`），fold 必须先序列化再解析（projection.ts:284）；
    请求头只有 run_code（timed=False）时照常追踪——ptc 分支不看 timed。
    """

    @staticmethod
    def _dispatch(**overrides):
        return {
            "rootCallId": "run_1", "parentCallId": "run_1",
            "subCallId": "run_1:ptc:1", "name": "ask_user_question",
            "arguments": {"questions": [
                {"id": "q1", "question": "Pick one",
                 "options": [{"label": "A"}, {"label": "B"}]}]},
            "isError": False,
            "content": [{"type": "text", "text": json.dumps(
                {"pending": True, "callId": "run_1:ptc:1"})}],
            **overrides,
        }

    @classmethod
    def _events(cls, *dispatches):
        header = {"seq": 0, "type": "request/header", "data": {"header": {
            "tools": [{"name": "run_code", "parameters": {"type": "object"}}]}}}
        return [header] + [
            {"seq": index + 1, "type": "tool/ptc-dispatch", "data": dispatch}
            for index, dispatch in enumerate(dispatches or (cls._dispatch(),))]

    def test_pending_ptc_subcall_is_tracked(self):
        view = fold_user_questions(self._events())
        self.assertEqual(view["active"], [{
            "callId": "run_1:ptc:1",
            "questions": [{"id": "q1", "question": "Pick one",
                           "options": [{"label": "A"}, {"label": "B"}]}],
            "state": "continued"}])
        self.assertEqual(view["settled"], [])

    def test_duplicate_dispatch_keeps_single_card(self):
        view = fold_user_questions(
            self._events(self._dispatch(), self._dispatch()))
        self.assertEqual(len(view["active"]), 1)
        self.assertEqual(view["active"][0]["callId"], "run_1:ptc:1")

    def test_unreadable_arguments_leave_no_card(self):
        view = fold_user_questions(self._events(
            self._dispatch(arguments={"questions": "invalid"})))
        self.assertEqual(view, {"active": [], "settled": []})

    def test_errored_or_settled_dispatch_leaves_no_card(self):
        view = fold_user_questions(self._events(self._dispatch(isError=True)))
        self.assertEqual(view, {"active": [], "settled": []})
        view = fold_user_questions(self._events(self._dispatch(content=[
            {"type": "text", "text": json.dumps({"answers": []})}])) )
        self.assertEqual(view, {"active": [], "settled": []})


class UserQuestionTimedServiceTest(unittest.TestCase):
    def setUp(self):
        self.fx = _TimedFixture()
        self.addCleanup(self.fx.dispose)

    def test_answer_steers_reply_and_closes_question(self):
        self.fx.seed(timed=True, pending=True)
        accepted = self.fx.service.answer(
            self.fx.agent, "c1", {"answers": [{"id": "q1", "selected": ["ok"]}]})
        self.assertTrue(accepted)
        self.assertEqual(len(self.fx.agent.steered), 1)
        message = self.fx.agent.steered[0]
        self.assertEqual(message["source"]["kind"], "user-question-reply")
        self.assertEqual(message["source"]["callId"], "c1")

    def test_answer_unknown_call_returns_false(self):
        self.fx.seed(timed=True, pending=True)
        self.assertFalse(self.fx.service.answer(
            self.fx.agent, "ghost", {"answers": []}))

    def test_answer_bad_batch_rejected(self):
        self.fx.seed(timed=True, pending=True)
        with self.assertRaises(UserQuestionError) as cm:
            self.fx.service.answer(
                self.fx.agent, "c1", {"answers": [{"id": "other", "selected": []}]})
        self.assertEqual(cm.exception.code, BAD_ANSWER)

    def test_answer_duplicate_reply_queued(self):
        self.fx.seed(timed=True, pending=True)
        self.fx.service.answer(
            self.fx.agent, "c1", {"answers": [{"id": "q1", "selected": ["ok"]}]})
        with self.assertRaises(UserQuestionError) as cm:
            self.fx.service.answer(
                self.fx.agent, "c1", {"answers": [{"id": "q1", "selected": ["ok"]}]})
        self.assertEqual(cm.exception.code, REPLY_QUEUED)

    def test_ask_timed_times_out_to_pending(self):
        self.fx.seed(timed=True, pending=True)
        result = _run(self.fx.service.ask_timed(
            {"questions": [{"id": "q1", "question": "?"}], "agent": self.fx.agent},
            "c1", 20))
        self.assertEqual(result, {"pending": True, "callId": "c1"})

    def test_ask_timed_returns_answer_within_window(self):
        self.fx.seed(timed=True, pending=True)
        _answerer(self.fx.ctx)
        result = _run(self.fx.service.ask_timed(
            {"questions": [{"id": "q1", "question": "?"}], "agent": self.fx.agent},
            "c1", 1000))
        self.assertEqual(result, PROVIDER)

    def test_ask_timed_bad_timeout(self):
        with self.assertRaises(UserQuestionError) as cm:
            _run(self.fx.service.ask_timed(
                {"questions": [{"id": "q1", "question": "?"}], "agent": self.fx.agent},
                "c1", 0))
        self.assertEqual(cm.exception.code, BAD_TIMEOUT)

    def test_ask_timed_duplicate_wait(self):
        self.fx.service._waits[self.fx.agent] = {"c1": object()}
        with self.assertRaises(UserQuestionError) as cm:
            _run(self.fx.service.ask_timed(
                {"questions": [{"id": "q1", "question": "?"}], "agent": self.fx.agent},
                "c1", 1000))
        self.assertEqual(cm.exception.code, DUPLICATE_WAIT)


class TimedBridgeSettlementTest(unittest.TestCase):
    """挂 web bridge 应答者时 ask_timed 的 Host 截止结算（上游 index.ts:261）。

    bridge 经 RemoteEventRegistry 把 `user-questions/request` 转给客户端；
    客户端不答时 Host 截止只置位 wait 的 signal——该 signal 必须结算 registry
    的挂起，否则 ask→ask_timed 永久等待，而不是按 deadline 落
    `{pending, callId}`（无应答者路径由 NO_PROVIDER→wait_done 覆盖，此处
    是有应答者但无人结算的路径）。
    """

    def setUp(self):
        self.fx = _TimedFixture()
        self.addCleanup(self.fx.dispose)
        from miniharness.web.events import RemoteEventRegistry
        from miniharness.web.questions import RemoteQuestionBridge
        self.registry = RemoteEventRegistry(home="C:/Users/me")
        self.addCleanup(self.registry.dispose)
        bridge = RemoteQuestionBridge(
            SimpleNamespace(events=self.registry, api=None))
        self.addCleanup(bridge.dispose)
        bridge.install(SimpleNamespace(session=self.fx.session, ctx=self.fx.ctx))

    def test_host_deadline_lands_pending_while_client_stalls(self):
        async def go():
            client = self.registry.open({"args": {}}).__aiter__()
            await client.__anext__()  # ready
            task = asyncio.ensure_future(self.fx.service.ask_timed(
                {"questions": [{"id": "q1", "question": "?"}],
                 "agent": self.fx.agent}, "c1", 60))
            frame = await client.__anext__()  # waterfall（bridge 转发）
            result = await asyncio.wait_for(task, timeout=5)
            cancel = await client.__anext__()
            return frame, result, cancel
        frame, result, cancel = _run(go())
        self.assertEqual(frame["event"], "user-questions/request")
        self.assertEqual(frame["request"]["questions"][0]["id"], "q1")
        self.assertEqual(result, {"pending": True, "callId": "c1"})
        self.assertEqual(cancel["type"], "cancel")


class TimedToolSchemaTest(unittest.TestCase):
    def test_timeout_parameter_present_in_timed_and_absent_in_legacy(self):
        from miniharness.core.tools import ToolRegistry
        from miniharness.interaction import register_ask_user_question
        ctx = Context(name="tools")
        self.addCleanup(ctx.dispose)
        install_user_questions(ctx)
        reg = ToolRegistry(ctx)

        legacy = register_ask_user_question(reg, ctx)
        legacy_tool = reg.resolve("ask_user_question")
        self.assertNotIn("timeout", legacy_tool.parameters["properties"])
        legacy()

        timed = register_ask_user_question(reg, ctx, mode="timed", timeout=30)
        timed_tool = reg.resolve("ask_user_question")
        self.assertIn("timeout", timed_tool.parameters["properties"])
        timed()


if __name__ == "__main__":
    unittest.main()