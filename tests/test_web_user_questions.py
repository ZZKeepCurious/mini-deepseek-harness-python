"""M9 验收：web 用户提问桥（web/questions.py）→ `$events` 转发与结算。

镜像上游 web-app bundle 的 ui-user-questions 应答者 + remotes 转发语义：
服务瀑布投递经 `$events` 流转发（waterfall 帧，agentId + request{questions,
agent}），客户端经 `$events/result` 结算；rejected 还原为结构化错误、
cancelled/dispose 归一为 ASK_ABORTED。

覆盖：请求投影（agent → id）、result 结算、rejected 结构化还原、dispose 中止。
"""

import asyncio
import json
import os
import unittest

from miniharness.core.scope import Context
from miniharness.core.tools import ToolRegistry
from miniharness.interaction import install_user_questions, register_ask_user_question
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import WebApi
from miniharness.web.stream_protocol import parse_remote_event_result_payload


def _run(coro):
    return asyncio.run(coro)


def _questions_args():
    return {"questions": [{"id": "continue", "question": "Continue to next step?"}]}


class QuestionBridgeTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="test")
        self.tools = ToolRegistry(self.ctx)
        install_user_questions(self.ctx)
        register_ask_user_question(self.tools, self.ctx)
        adapter = FakeLlmAdapter(
            tool_call={"name": "ask_user_question", "arguments": _questions_args()})
        adapter.model = "fake-model"
        self.api = WebApi(self.ctx, adapter, self.tools)

    def tearDown(self):
        self.api.gateway.dispose()
        self.ctx.dispose()

    def _create(self, session_id="session-q"):
        response = self.api.dispatch("session.create", "r1",
                                     {"cwd": os.getcwd(), "sessionId": session_id})
        self.assertTrue(response["result"]["ok"])
        return response["result"]["value"]["sessionId"]

    async def _prompt(self, sid):
        response = self.api.dispatch("session.prompt", "rp", {
            "sessionId": sid, "mode": "queue", "requestId": "req-" + sid,
            "content": [{"type": "text", "text": "hi"}],
        })
        self.assertTrue(response["result"]["ok"])
        await self.api._agents[sid].when_idle_async()

    @staticmethod
    async def _open_events(gateway):
        gen = gateway.open_stream("$events", {"args": {}})
        ready = await gen.__anext__()
        return gen, ready["clientId"]

    @staticmethod
    async def _next_waterfall(gen, timeout=300):
        for _ in range(timeout):
            frame = await gen.__anext__()
            if frame["type"] == "waterfall":
                return frame
        raise AssertionError("no waterfall frame")

    def _settle(self, client_id, event_id, outcome):
        result = parse_remote_event_result_payload(
            {"args": {"clientId": client_id, "eventId": event_id, "outcome": outcome}})
        self.api.gateway.receive_result(result)

    def _tool_result_event(self, sid):
        for event in self.api.store.get(sid).events:
            if event["type"] == "tool/result":
                return event
        raise AssertionError("no tool/result")

    def _tool_result_is_error(self, sid):
        # V4：role 'tool' 平铺 content + 顶层 isError
        return bool(self._tool_result_event(sid)["data"]["message"].get("isError"))

    def test_ask_flow_answered(self):
        async def go():
            sid = self._create()
            gen, client_id = await self._open_events(self.api.gateway)
            prompt = asyncio.create_task(self._prompt(sid))
            waterfall = await self._next_waterfall(gen)
            self.assertEqual(waterfall["event"], "user-questions/request")
            self.assertEqual(waterfall["agentId"], sid)
            # agent 投影为 id；questions 原样透传
            self.assertEqual(waterfall["request"],
                             {"questions": _questions_args()["questions"], "agent": sid})
            self._settle(client_id, waterfall["eventId"], {
                "kind": "result",
                "value": {"answers": [{"id": "continue", "selected": ["ok"]}]},
            })
            await prompt
            await gen.aclose()
            event = self._tool_result_event(sid)
            content = event["data"]["message"]["content"]
            self.assertFalse(self._tool_result_is_error(sid))
            # canonical 紧凑 JSON 文本（content 载体为 str，模型可见纯 JSON）
            text = content[0]["text"]
            self.assertEqual(
                text,
                '{"answers":[{"id":"continue","selected":["ok"]}]}')

        _run(go())

    def test_ask_flow_rejected_structured_error(self):
        async def go():
            sid = self._create()
            gen, client_id = await self._open_events(self.api.gateway)
            prompt = asyncio.create_task(self._prompt(sid))
            waterfall = await self._next_waterfall(gen)
            self._settle(client_id, waterfall["eventId"], {
                "kind": "rejected",
                "error": {"name": "UserQuestionError", "message": "the user cancelled ask_user_question",
                          "code": "ASK_CANCELLED"},
            })
            await prompt
            await gen.aclose()
            event = self._tool_result_event(sid)
            self.assertTrue(self._tool_result_is_error(sid))
            # 结构化错误 {name, code} 落 tool/result.error；文本带原消息
            self.assertEqual(event["data"]["error"],
                             {"name": "UserQuestionError", "code": "ASK_CANCELLED"})
            text = event["data"]["message"]["content"][0]["text"]
            self.assertTrue(text.startswith("Error: the user cancelled ask_user_question"))

        _run(go())

    def test_ask_flow_rejected_without_code_becomes_runtime_error(self):
        async def go():
            sid = self._create()
            gen, client_id = await self._open_events(self.api.gateway)
            prompt = asyncio.create_task(self._prompt(sid))
            waterfall = await self._next_waterfall(gen)
            # 无 code 的非域拒绝 → RuntimeError（非 UserQuestionError 形状）
            self._settle(client_id, waterfall["eventId"], {
                "kind": "rejected",
                "error": {"name": "InternalError", "message": "boom"},
            })
            await prompt
            await gen.aclose()
            event = self._tool_result_event(sid)
            self.assertTrue(self._tool_result_is_error(sid))
            # 非域错误不带 error {name, code} 字段（工具普通错误）
            self.assertNotIn("error", event["data"])

        _run(go())

    def test_dispose_settles_cancelled_as_aborted(self):
        async def go():
            sid = self._create()
            gen, client_id = await self._open_events(self.api.gateway)
            prompt = asyncio.create_task(self._prompt(sid))
            waterfall = await self._next_waterfall(gen)
            self.assertIsNotNone(waterfall)
            self.api.gateway.dispose()   # 网关 dispose → 全 pending 'cancelled'
            await prompt
            await gen.aclose()
            event = self._tool_result_event(sid)
            self.assertTrue(self._tool_result_is_error(sid))
            self.assertEqual(event["data"]["error"],
                             {"name": "UserQuestionError", "code": "ASK_ABORTED"})

        _run(go())


class UserQuestionsWireTest(unittest.TestCase):
    """dsh-v0.2.0-rc.2：userQuestions/answer unary + attachWait 流。"""

    def setUp(self):
        self.ctx = Context(name="uq-wire")
        install_user_questions(self.ctx)
        self.api = WebApi(self.ctx, FakeLlmAdapter())

    def tearDown(self):
        self.api.gateway.dispose()
        self.ctx.dispose()

    def _create(self):
        response = self.api.dispatch("session.create", "r1",
                                     {"cwd": os.getcwd(), "sessionId": "uq-s"})
        self.assertTrue(response["result"]["ok"])
        return "uq-s"

    def _seed_timed(self, sid):
        session = self.api.store.get(sid)
        session.append("request/header", {
            "header": {"tools": [{"name": "ask_user_question", "parameters": {
                "properties": {"timeout": {"type": "integer"}}}}]},
            "reason": "initial"})
        session.append("tool/call", {
            "callId": "c1", "name": "ask_user_question",
            "arguments": json.dumps({"questions": [{"id": "q1", "question": "?"}]})})
        session.append("tool/result", {"message": {
            "role": "tool", "toolCallId": "c1",
            "source": {"kind": "tool", "callId": "c1"},
            "content": [{"type": "text", "text": json.dumps(
                {"pending": True, "callId": "c1", "message": "pending"})}],
            "isError": False}}, surfaceOp="append")

    def test_answer_route_closes_question(self):
        sid = self._create()
        self._seed_timed(sid)
        response = self.api.dispatch("userQuestions/answer", "a1", {
            "agentId": sid, "callId": "c1",
            "answer": {"answers": [{"id": "q1", "selected": ["ok"]}]}})
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        self.assertTrue(response["result"]["value"])

    def test_answer_unknown_call_returns_false(self):
        sid = self._create()
        response = self.api.dispatch("userQuestions/answer", "a2", {
            "agentId": sid, "callId": "ghost", "answer": {"answers": []}})
        self.assertTrue(response["result"]["ok"])
        self.assertFalse(response["result"]["value"])

    def test_answer_not_mounted(self):
        ctx2 = Context(name="bare-uq")
        try:
            api = WebApi(ctx2, FakeLlmAdapter())
            response = api.dispatch("userQuestions/answer", "a3", {
                "agentId": "s", "callId": "c", "answer": {"answers": []}})
            self.assertFalse(response["result"]["ok"])
            self.assertEqual(response["result"]["error"]["code"],
                             "gateway/invocation-unavailable")
        finally:
            ctx2.dispose()

    def test_attach_wait_unknown_call_ends_immediately(self):
        async def go():
            sid = self._create()
            gen = self.api.gateway.open_stream(
                "userQuestions/attachWait", {"args": {"agentId": sid, "callId": "ghost"}})
            with self.assertRaises(StopAsyncIteration):
                await gen.__anext__()
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()