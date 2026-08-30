"""web 审批桥测试：`tools/ask` 闸门 → `$events` approval/request waterfall → `$events/result`。

对齐上游 `packages/interaction/user-approval` + `packages/api/remotes`：审批问询
经 `$events` 流投递（waterfall 帧，agentId + request{toolName}），首个客户端经
`$events/result`（解析 `parse_remote_event_result_payload` 走 `gateway.receive_result`）
结算。`RemoteApprovalBridge.install` 把 async tools/ask answerer 挂在 loop.ctx，
每次问询自落审计对 approval/asked + approval/decided。
"""
import asyncio
import os
import unittest

from miniharness.core.scope import Context
from miniharness.core.tools import Tool, ToolRegistry
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import WebApi
from miniharness.web.stream_protocol import parse_remote_event_result_payload


def _run(coro):
    return asyncio.run(coro)


def _echo_tool(registry):
    registry.register(Tool(
        name="echo", description="echo text",
        execute=lambda args, exec_: {"echo": args.get("text", "")},
        parameters={"type": "object", "properties": {"text": {"type": "string"}}},
    ))


class ApprovalBridgeTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="test")
        self.tools = ToolRegistry(self.ctx)
        _echo_tool(self.tools)
        adapter = FakeLlmAdapter(tool_call={"name": "echo", "arguments": {"text": "hi"}})
        adapter.model = "fake-model"
        self.api = WebApi(self.ctx, adapter, self.tools)

    def tearDown(self):
        self.api.gateway.dispose()
        self.ctx.dispose()

    def _create(self, session_id="session-a"):
        response = self.api.dispatch("session.create", "r1",
                                     {"cwd": os.getcwd(), "sessionId": session_id})
        self.assertTrue(response["result"]["ok"])
        return response["result"]["value"]["sessionId"]

    def _arm_ask(self, loop):
        loop.ctx.on("tools/pre-execute", lambda payload, nxt: {"kind": "ask"})

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
    async def _next_waterfall(gen, timeout=200):
        for _ in range(timeout):
            frame = await gen.__anext__()
            if frame["type"] == "waterfall":
                return frame
        raise AssertionError("no waterfall frame")

    def _settle(self, client_id, event_id, outcome):
        result = parse_remote_event_result_payload({
            "args": {"clientId": client_id, "eventId": event_id, "outcome": outcome}})
        self.api.gateway.receive_result(result)

    def _audit(self, sid):
        session = self.api.store.get(sid)
        return [e for e in session.events if e["type"] in ("approval/asked", "approval/decided")]

    def _tool_result_is_error(self, sid):
        for event in self.api.store.get(sid).events:
            if event["type"] == "tool/result":
                block = event["data"]["message"]["content"][0]
                return bool(block.get("isError"))
        return None

    def _tool_result_present(self, sid):
        return any(e["type"] == "tool/result" for e in self.api.store.get(sid).events)

    def test_ask_flow_approved(self):
        async def go():
            sid = self._create()
            self._arm_ask(self.api._agents[sid])
            gen, client_id = await self._open_events(self.api.gateway)
            prompt = asyncio.create_task(self._prompt(sid))
            waterfall = await self._next_waterfall(gen)
            self.assertEqual(waterfall["event"], "approval/request")
            self.assertEqual(waterfall["agentId"], sid)
            self.assertEqual(waterfall["request"], {"toolName": "echo"})
            self._settle(client_id, waterfall["eventId"],
                         {"kind": "result", "value": "allowed-once"})
            await prompt
            await gen.aclose()
            audit = self._audit(sid)
            self.assertEqual(len(audit), 2)
            self.assertEqual(audit[0]["data"]["toolName"], "echo")
            self.assertEqual(audit[1]["data"]["outcome"], "allowed-once")
            self.assertTrue(self._tool_result_present(sid))
            self.assertFalse(self._tool_result_is_error(sid))

        _run(go())

    def test_ask_flow_rejected(self):
        async def go():
            sid = self._create()
            self._arm_ask(self.api._agents[sid])
            gen, client_id = await self._open_events(self.api.gateway)
            prompt = asyncio.create_task(self._prompt(sid))
            waterfall = await self._next_waterfall(gen)
            self._settle(client_id, waterfall["eventId"],
                         {"kind": "result", "value": "rejected"})
            await prompt
            await gen.aclose()
            audit = self._audit(sid)
            self.assertEqual(audit[1]["data"]["outcome"], "rejected")
            self.assertTrue(self._tool_result_present(sid))
            self.assertTrue(self._tool_result_is_error(sid))

        _run(go())

    def test_ask_flow_unavailable_value_fails_closed(self):
        async def go():
            sid = self._create()
            self._arm_ask(self.api._agents[sid])
            gen, client_id = await self._open_events(self.api.gateway)
            prompt = asyncio.create_task(self._prompt(sid))
            waterfall = await self._next_waterfall(gen)
            # 客户端给了非 APPROVAL_OUTCOMES 的 value → 归一化 'unavailable'
            self._settle(client_id, waterfall["eventId"],
                         {"kind": "result", "value": "bogus"})
            await prompt
            await gen.aclose()
            audit = self._audit(sid)
            self.assertEqual(audit[1]["data"]["outcome"], "unavailable")
            self.assertTrue(self._tool_result_is_error(sid))

        _run(go())

    def test_dispose_settles_cancelled(self):
        async def go():
            sid = self._create()
            self._arm_ask(self.api._agents[sid])
            gen, client_id = await self._open_events(self.api.gateway)
            prompt = asyncio.create_task(self._prompt(sid))
            waterfall = await self._next_waterfall(gen)
            self.assertIsNotNone(waterfall)
            self.api.gateway.dispose()   # 网关 dispose → 全 pending 'cancelled'
            await prompt
            await gen.aclose()
            audit = self._audit(sid)
            self.assertEqual(audit[1]["data"]["outcome"], "cancelled")
            self.assertTrue(self._tool_result_present(sid))
            self.assertTrue(self._tool_result_is_error(sid))

        _run(go())

    def test_events_dispose_sets_source_removed(self):
        """dispose 后 $events 打开仍可，但转发源已拆除（无 pending 结算）。"""
        async def go():
            self.api.gateway.dispose()
            gen = self.api.gateway.open_stream("$events", {"args": {}})
            ready = await gen.__anext__()
            await gen.aclose()
            return ready["type"]
        self.assertEqual(_run(go()), "ready")


if __name__ == "__main__":
    unittest.main()
