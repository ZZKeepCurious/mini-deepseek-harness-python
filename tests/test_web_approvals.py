"""web 审批桥测试：tools/ask 问询 → approval/requested|resolved mux 帧 + respond 路由。

对齐上游 apiproxy approval 通道：approval/requested 是可应答 server-request
（rpcId = pending 稳定 id），respond 回显 rpcId，RpcReceipt 表达成败；
网关 dispose → 全 pending 'cancelled'。运行方式：unittest 发现（api/streams
层 stdlib-only，无第三方依赖）。
"""
import asyncio
import os
import unittest

from miniharness.core.scope import Context
from miniharness.core.tools import Tool, ToolRegistry
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import WebApi
from miniharness.web.streams import StreamHub


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
        self.hub = StreamHub(self.ctx, self.api)

    def tearDown(self):
        self.hub.dispose()
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
    async def _next_approval(mux, timeout=200):
        for _ in range(timeout):
            frame = await mux.__anext__()
            if frame["type"] == "approval/requested":
                return frame
        raise AssertionError("no approval/requested frame")

    def _answer(self, requested, sid, outcome):
        return self.api.approvals.respond({
            "type": "client-response",
            "rpcId": requested["rpcId"],
            "result": {"ok": True, "value": {
                "sessionId": sid, "approvalId": requested["approvalId"], "outcome": outcome,
            }},
        })

    def _audit(self, sid):
        session = self.api.store.get(sid)
        return [e for e in session.events if e["type"] in ("approval/asked", "approval/decided")]

    def _tool_result_is_error(self, sid):
        """首个 tool/result 的 tool-result 块 isError（块在 message.content[0]）。"""
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
            mux = self.hub.mux()
            await mux.__anext__()  # baseline subscribed
            prompt = asyncio.create_task(self._prompt(sid))
            requested = await self._next_approval(mux)
            self.assertEqual(requested["sessionId"], sid)
            self.assertEqual(requested["toolName"], "echo")
            receipt = self._answer(requested, sid, "allowed-once")
            self.assertEqual(receipt, {"accepted": True})
            resolved = None
            for _ in range(200):
                frame = await mux.__anext__()
                if frame["type"] == "approval/resolved":
                    resolved = frame
                    break
            self.assertEqual(resolved["outcome"], "allowed-once")
            self.assertEqual(resolved["rpcId"], requested["rpcId"])
            await prompt
            audit = self._audit(sid)
            self.assertEqual(len(audit), 2)
            self.assertEqual(audit[0]["data"]["toolName"], "echo")
            self.assertEqual(audit[1]["data"]["outcome"], "allowed-once")
            self.assertTrue(self._tool_result_present(sid))
            self.assertFalse(self._tool_result_is_error(sid))
            return requested, resolved

        requested, resolved = _run(go())
        # resolved 与 requested 复用同一 pending 稳定 rpcId
        self.assertEqual(requested["rpcId"], resolved["rpcId"])

    def test_ask_flow_rejected(self):
        async def go():
            sid = self._create()
            self._arm_ask(self.api._agents[sid])
            mux = self.hub.mux()
            await mux.__anext__()
            prompt = asyncio.create_task(self._prompt(sid))
            requested = await self._next_approval(mux)
            receipt = self._answer(requested, sid, "rejected")
            self.assertEqual(receipt, {"accepted": True})
            await prompt
            audit = self._audit(sid)
            self.assertEqual(audit[1]["data"]["outcome"], "rejected")
            self.assertTrue(self._tool_result_present(sid))
            self.assertTrue(self._tool_result_is_error(sid))

        _run(go())

    def test_respond_not_pending(self):
        receipt = self.api.approvals.respond({
            "type": "client-response", "rpcId": "no-such-pending",
            "result": {"ok": True, "value": {"sessionId": "s", "approvalId": "a",
                                             "outcome": "allowed-once"}},
        })
        self.assertEqual(receipt, {"accepted": False, "reason": "not-pending"})

    def test_respond_bad_response(self):
        async def go():
            sid = self._create()
            self._arm_ask(self.api._agents[sid])
            mux = self.hub.mux()
            await mux.__anext__()
            prompt = asyncio.create_task(self._prompt(sid))
            requested = await self._next_approval(mux)
            bridge = self.api.approvals
            # approvalId 不符
            bad = {"type": "client-response", "rpcId": requested["rpcId"],
                   "result": {"ok": True, "value": {"sessionId": sid,
                                                    "approvalId": "wrong",
                                                    "outcome": "allowed-once"}}}
            self.assertEqual(bridge.respond(bad), {"accepted": False, "reason": "bad-response"})
            # sessionId 不符
            bad = {"type": "client-response", "rpcId": requested["rpcId"],
                   "result": {"ok": True, "value": {"sessionId": "other",
                                                    "approvalId": requested["approvalId"],
                                                    "outcome": "allowed-once"}}}
            self.assertEqual(bridge.respond(bad), {"accepted": False, "reason": "bad-response"})
            # outcome 非词汇表
            bad = {"type": "client-response", "rpcId": requested["rpcId"],
                   "result": {"ok": True, "value": {"sessionId": sid,
                                                    "approvalId": requested["approvalId"],
                                                    "outcome": "bogus"}}}
            self.assertEqual(bridge.respond(bad), {"accepted": False, "reason": "bad-response"})
            # 非 ok 结果分支
            bad = {"type": "client-response", "rpcId": requested["rpcId"],
                   "result": {"ok": False, "error": {"code": "internal", "message": "x",
                                                     "details": {}}}}
            self.assertEqual(bridge.respond(bad), {"accepted": False, "reason": "bad-response"})
            # pending 仍挂起：合法应答收尾
            receipt = self._answer(requested, sid, "allowed-once")
            self.assertEqual(receipt, {"accepted": True})
            await prompt

        _run(go())

    def test_replay_after_reopen(self):
        async def go():
            sid = self._create()
            self._arm_ask(self.api._agents[sid])
            mux = self.hub.mux()
            await mux.__anext__()
            prompt = asyncio.create_task(self._prompt(sid))
            requested = await self._next_approval(mux)
            await mux.aclose()
            # 重连：基线应补发同一 pending（复用原 rpcId）
            mux2 = self.hub.mux()
            replay = None
            for _ in range(10):
                frame = await mux2.__anext__()
                if frame["type"] == "approval/requested":
                    replay = frame
                    break
            self.assertIsNotNone(replay)
            self.assertEqual(replay["rpcId"], requested["rpcId"])
            self.assertEqual(replay["approvalId"], requested["approvalId"])
            receipt = self._answer(replay, sid, "allowed-once")
            self.assertEqual(receipt, {"accepted": True})
            await prompt
            await mux2.aclose()

        _run(go())

    def test_dispose_settles_cancelled(self):
        async def go():
            sid = self._create()
            self._arm_ask(self.api._agents[sid])
            mux = self.hub.mux()
            await mux.__anext__()
            prompt = asyncio.create_task(self._prompt(sid))
            requested = await self._next_approval(mux)
            self.assertIsNotNone(requested)
            self.hub.dispose()   # 网关 dispose → 全部 pending 'cancelled'
            await prompt
            audit = self._audit(sid)
            self.assertEqual(audit[1]["data"]["outcome"], "cancelled")
            self.assertTrue(self._tool_result_present(sid))
            self.assertTrue(self._tool_result_is_error(sid))

        _run(go())

    def test_bridge_replay_no_pending(self):
        async def go():
            sid = self._create()
            mux = self.hub.mux()
            frame = await mux.__anext__()
            return frame
        # 无 pending：基线首个帧仍是 subscribed（不插入 approval/requested）
        frame = _run(go())
        self.assertEqual(frame["type"], "session/subscribed")


if __name__ == "__main__":
    unittest.main()