"""web 会话服务测试：WebApi 七个 unary 方法（对齐 packages/host/apiproxy/src/api/sessions.ts）。

运行方式：unittest 发现；无第三方依赖（api 层 stdlib-only）。
"""
import asyncio
import os
import unittest

from miniharness.commands import install_commands
from miniharness.core.scope import Context
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import WebApi, canonical_client_time_zone

CWD = os.getcwd()


def _run(coro):
    return asyncio.run(coro)


def _fake(model: str = "fake-model") -> FakeLlmAdapter:
    adapter = FakeLlmAdapter()
    adapter.model = model
    return adapter


class WebApiTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="test")
        self.api = WebApi(self.ctx, _fake())

    def tearDown(self):
        self.ctx.dispose()

    def _create(self, rpc_id="r1", cwd=CWD, session_id=None, agent_preset=None, workspace_id=None):
        payload = {"cwd": cwd}
        if session_id is not None:
            payload["sessionId"] = session_id
        if agent_preset is not None:
            payload["agentPreset"] = agent_preset
        if workspace_id is not None:
            payload["workspaceId"] = workspace_id
        return self.api.dispatch("session.create", rpc_id, payload)

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def _error(self, response):
        self.assertFalse(response["result"]["ok"])
        return response["result"]["error"]

    def _prompt_and_settle(self, session_id, text, mode="queue", rpc_id="rp1"):
        async def go():
            response = self.api.dispatch("session.prompt", rpc_id, {
                "sessionId": session_id, "mode": mode,
                "content": [{"type": "text", "text": text}],
            })
            self.assertEqual(self._value(response), {"accepted": True})
            await self.api._agents[session_id].when_idle_async()
            return response

        return _run(go())


class TestHostDescribe(WebApiTest):
    def test_describe_empty_payload(self):
        response = self.api.dispatch("host.describe", "rid", {})
        self.assertEqual(response["rpcId"], "rid")
        value = self._value(response)
        self.assertEqual(value["cwd"], CWD)
        self.assertEqual(value["provider"], "fake")
        self.assertEqual(value["model"], "fake-model")
        self.assertEqual(value["attachedSessions"], 0)
        self.assertIs(value["canOpenPath"], False)
        self.assertIsInstance(value["version"], str)

    def test_describe_counts_attached(self):
        self._create()
        value = self._value(self.api.dispatch("host.describe", "rid", {}))
        self.assertEqual(value["attachedSessions"], 1)


class TestSessionCreate(WebApiTest):
    def test_create_returns_session(self):
        value = self._value(self._create())
        self.assertTrue(value["sessionId"].startswith("session-"))
        self.assertNotIn("agentPreset", value)

    def test_create_echoes_agent_preset(self):
        value = self._value(self._create(agent_preset="standard"))
        self.assertEqual(value["agentPreset"], "standard")

    def test_create_preallocated_retry_same_cwd(self):
        value = self._value(self._create(session_id="session-abc"))
        self.assertEqual(value["sessionId"], "session-abc")
        again = self._value(self._create(session_id="session-abc"))
        self.assertEqual(again["sessionId"], "session-abc")
        self.assertEqual(len(self.api.store.list()), 1)

    def test_create_preallocated_conflict(self):
        self._create(session_id="session-abc", cwd=CWD)
        error = self._error(self._create(session_id="session-abc", cwd=os.path.dirname(CWD)))
        self.assertEqual(error["code"], "session-conflict")
        self.assertEqual(error["details"]["sessionId"], "session-abc")
        self.assertEqual(error["details"]["requestedCwd"], os.path.dirname(CWD))
        self.assertEqual(error["details"]["existingCwd"], CWD)

    def test_create_workspace_id_rejected(self):
        error = self._error(self._create(workspace_id="w-1"))
        self.assertEqual(error["code"], "workspace-not-found")
        self.assertEqual(error["details"], {"workspaceId": "w-1"})

    def test_create_invalid_cwd(self):
        error = self._error(self._create(cwd="relative/path"))
        self.assertEqual(error["code"], "bad-request")

    def test_create_bad_payload(self):
        response = self.api.dispatch("session.create", "rid", "nonsense")
        self.assertEqual(response["rpcId"], "rid")
        self.assertEqual(self._error(response)["code"], "bad-request")


class TestSessionPrompt(WebApiTest):
    def test_prompt_runs_turn(self):
        session_id = self._value(self._create())["sessionId"]
        self._prompt_and_settle(session_id, "hello", rpc_id="rp1")
        types = [ev["type"] for ev in self.api.store.get(session_id).events]
        self.assertIn("turn/start", types)
        self.assertIn("user/message", types)
        self.assertIn("assistant/message", types)

    def test_prompt_source_carries_rpc_id_and_time_zone(self):
        session_id = self._value(self._create())["sessionId"]
        async def go():
            response = self.api.dispatch("session.prompt", "rp-tz", {
                "sessionId": session_id, "mode": "queue",
                "content": [{"type": "text", "text": "hi"}], "clientTimeZone": "UTC",
            })
            self.assertEqual(self._value(response), {"accepted": True})
            await self.api._agents[session_id].when_idle_async()
        _run(go())
        events = list(self.api.store.get(session_id).events)
        user = next(ev for ev in events if ev["type"] == "user/message")
        self.assertEqual(user["data"]["source"]["kind"], "user")
        self.assertEqual(user["data"]["source"]["rpcId"], "rp-tz")
        self.assertEqual(user["data"]["source"]["clientTimeZone"], "UTC")

    def test_prompt_steer_mode(self):
        session_id = self._value(self._create())["sessionId"]
        async def go():
            response = self.api.dispatch("session.prompt", "rp1", {
                "sessionId": session_id, "mode": "steer",
                "content": [{"type": "text", "text": "steer me"}],
            })
            self.assertEqual(self._value(response), {"accepted": True})
            await self.api._agents[session_id].when_idle_async()
        _run(go())
        self.assertIn("user/message",
                      [ev["type"] for ev in self.api.store.get(session_id).events])

    def test_prompt_unknown_session(self):
        error = self._error(self.api.dispatch("session.prompt", "rid", {
            "sessionId": "session-nope", "mode": "queue",
            "content": [{"type": "text", "text": "hi"}],
        }))
        self.assertEqual(error["code"], "session-not-found")
        self.assertEqual(error["details"], {"sessionId": "session-nope"})

    def test_prompt_invalid_mode(self):
        error = self._error(self.api.dispatch("session.prompt", "rid", {
            "sessionId": "x", "mode": "explode",
            "content": [{"type": "text", "text": "hi"}],
        }))
        self.assertEqual(error["code"], "bad-request")

    def test_prompt_invalid_time_zone(self):
        session_id = self._value(self._create())["sessionId"]
        error = self._error(self.api.dispatch("session.prompt", "rid", {
            "sessionId": session_id, "mode": "queue",
            "content": [{"type": "text", "text": "hi"}], "clientTimeZone": "Not/AZone",
        }))
        self.assertEqual(error["code"], "invalid-time-zone")
        self.assertEqual(error["details"], {"value": "Not/AZone"})

    def test_prompt_image_not_supported_by_model(self):
        session_id = self._value(self._create())["sessionId"]
        adapter = _fake()
        adapter.resolve_model_info = lambda: {
            "provider": "fake", "model": "fake-model", "input_modalities": ["text"],
        }
        self.api.adapter = adapter
        error = self._error(self.api.dispatch("session.prompt", "rid", {
            "sessionId": session_id, "mode": "queue",
            "content": [{"type": "image", "mediaType": "image/png", "data": "AA=="}],
        }))
        self.assertEqual(error["code"], "attachment-error")
        self.assertEqual(error["details"], {"reason": "MODEL_DOES_NOT_SUPPORT_IMAGES"})

    def test_prompt_image_no_attachment_service(self):
        session_id = self._value(self._create())["sessionId"]
        error = self._error(self.api.dispatch("session.prompt", "rid", {
            "sessionId": session_id, "mode": "queue",
            "content": [{"type": "image", "mediaType": "image/png", "data": "AA=="}],
        }))
        self.assertEqual(error["code"], "attachment-error")
        self.assertEqual(error["details"], {"reason": "ATTACHMENT_UNAVAILABLE"})


class TestSlashCommand(WebApiTest):
    def test_unknown_command_without_registry(self):
        session_id = self._value(self._create())["sessionId"]
        error = self._error(self.api.dispatch("session.prompt", "rid", {
            "sessionId": session_id, "mode": "queue",
            "content": [{"type": "text", "text": "/greet world"}],
        }))
        self.assertEqual(error["code"], "unknown-command")

    def test_registered_command_runs(self):
        install_commands(self.ctx).register(
            "greet", "greet someone",
            lambda agent, raw: {"kind": "success", "text": f"hi {raw.strip()}"})
        session_id = self._value(self._create())["sessionId"]
        value = self._value(self.api.dispatch("session.prompt", "rid", {
            "sessionId": session_id, "mode": "queue",
            "content": [{"type": "text", "text": "/greet world"}],
        }))
        self.assertEqual(value, {"accepted": True,
                                 "command": {"kind": "success", "text": "hi world"}})
        # 命令不进模型：日志有 command/run+done，无 user/message
        types = [ev["type"] for ev in self.api.store.get(session_id).events]
        self.assertIn("command/run", types)
        self.assertIn("command/done", types)
        self.assertNotIn("user/message", types)

    def test_registered_command_error(self):
        install_commands(self.ctx).register(
            "boom", "fails", lambda agent, raw: {"kind": "error", "text": "kaput"})
        session_id = self._value(self._create())["sessionId"]
        error = self._error(self.api.dispatch("session.prompt", "rid", {
            "sessionId": session_id, "mode": "queue",
            "content": [{"type": "text", "text": "/boom now"}],
        }))
        self.assertEqual(error["code"], "command-error")
        self.assertEqual(error["message"], "kaput")

    def test_unknown_command_name(self):
        install_commands(self.ctx).register(
            "greet", "greet", lambda agent, raw: {"kind": "success", "text": "ok"})
        session_id = self._value(self._create())["sessionId"]
        error = self._error(self.api.dispatch("session.prompt", "rid", {
            "sessionId": session_id, "mode": "queue",
            "content": [{"type": "text", "text": "/nope arg"}],
        }))
        self.assertEqual(error["code"], "unknown-command")


class TestSessionList(WebApiTest):
    def test_list_blank_before_turn(self):
        self._create(session_id="session-blank")
        item = self._value(self.api.dispatch("session.list", "rid", {}))["items"][0]
        self.assertEqual(item["sessionId"], "session-blank")
        self.assertIs(item["blank"], True)
        self.assertIs(item["running"], False)
        self.assertEqual(item["updatedAt"], self.api.store.get("session-blank").created_at)

    def test_list_after_turn(self):
        session_id = self._value(self._create())["sessionId"]
        self._prompt_and_settle(session_id, "hello")
        item = self._value(self.api.dispatch("session.list", "rid", {}))["items"][0]
        self.assertIs(item["blank"], False)
        self.assertIs(item["running"], False)
        user = next(ev for ev in self.api.store.get(session_id).events
                    if ev["type"] == "user/message")
        self.assertEqual(item["updatedAt"], user["time"])

    def test_list_meta_passthrough(self):
        self._create(session_id="session-meta", agent_preset="standard")
        item = self._value(self.api.dispatch("session.list", "rid", {}))["items"][0]
        self.assertEqual(item["cwd"], CWD)
        self.assertEqual(item["agentPreset"], "standard")

    def test_list_sorted_desc(self):
        self._create(session_id="session-a")
        self._create(session_id="session-b")
        items = self._value(self.api.dispatch("session.list", "rid", {}))["items"]
        self.assertEqual(len(items), 2)
        self.assertGreaterEqual(items[0]["updatedAt"], items[1]["updatedAt"])


class TestSessionHistory(WebApiTest):
    def _append(self, session_id, type_, surface_op="append", source_event_seqs=None, **data):
        session = self.api.store.get(session_id)
        if type_ == "user/message":
            payload = {"message": data, "turn": {"start": {"kind": "user"}}}
        elif type_ == "assistant/message":
            payload = {"message": data, "turn": {"end": {"kind": "stop"}}}
        else:
            payload = data
        if type_ in ("user/message", "assistant/message", "tool/result"):
            return session.append(type_, payload, surfaceOp=surface_op,
                                  sourceEventSeqs=source_event_seqs)
        return session.append(type_, payload)

    def test_history_unknown_session(self):
        error = self._error(self.api.dispatch("session.history", "rid",
                                              {"sessionId": "session-nope"}))
        self.assertEqual(error["code"], "session-not-found")

    def test_history_tail_page_boundaries(self):
        self._create(session_id="session-h")
        self._append("session-h", "user/message", message={"id": "m1"})
        self._append("session-h", "tool/call", tool_call={"id": "t1"})
        self._append("session-h", "tool/result", tool_result={"id": "r1"})
        self._append("session-h", "assistant/message", message={"id": "m2"})
        self._append("session-h", "user/message", message={"id": "m3"})
        # maxMessages=1 尾页 = 最后一整条消息（含其后的工具事件不会被截断）
        value = self._value(self.api.dispatch("session.history", "rid", {
            "sessionId": "session-h", "maxMessages": 1}))
        self.assertEqual(len(value["events"]), 1)
        self.assertEqual(value["events"][0]["event"]["type"], "user/message")
        self.assertTrue(value["hasMore"])

    def test_history_all_when_unbounded(self):
        self._create(session_id="session-h")
        self._append("session-h", "user/message", message={"id": "m1"})
        self._append("session-h", "assistant/message", message={"id": "m2"})
        value = self._value(self.api.dispatch("session.history", "rid",
                                              {"sessionId": "session-h"}))
        self.assertEqual(len(value["events"]), 2)
        self.assertFalse(value["hasMore"])
        self.assertEqual(value["events"][0]["event"]["type"], "user/message")

    def test_history_before_seq(self):
        self._create(session_id="session-h")
        self._append("session-h", "user/message", message={"id": "m1"})
        self._append("session-h", "assistant/message", message={"id": "m2"})
        self._append("session-h", "user/message", message={"id": "m3"})
        # beforeSeq=2 → 窗口 seq<2 = [m1, m2]；maxMessages=1 → 最后一整条消息 m2
        value = self._value(self.api.dispatch("session.history", "rid", {
            "sessionId": "session-h", "beforeSeq": 2, "maxMessages": 1}))
        self.assertEqual(len(value["events"]), 1)
        self.assertEqual(value["events"][0]["event"]["type"], "assistant/message")
        self.assertTrue(value["hasMore"])
        # 再往前一页：窗口 seq<1 = [m1]，无更早页
        earlier = self._value(self.api.dispatch("session.history", "rid", {
            "sessionId": "session-h", "beforeSeq": 1, "maxMessages": 1}))
        self.assertEqual(len(earlier["events"]), 1)
        self.assertEqual(earlier["events"][0]["event"]["type"], "user/message")
        self.assertFalse(earlier["hasMore"])

    def test_history_replacement_consumes_no_quota(self):
        self._create(session_id="session-h")
        self._append("session-h", "user/message", message={"id": "m1"})
        self._append("session-h", "assistant/message", message={"id": "m2"})
        self._append("session-h", "assistant/message", message={"id": "m2r"},
                     surface_op={"op": "replace", "start": 0, "end": 1},
                     source_event_seqs=[1])
        value = self._value(self.api.dispatch("session.history", "rid", {
            "sessionId": "session-h", "maxMessages": 1}))
        # 替换副本不计消息配额，与其源消息同页
        self.assertEqual([ev["event"]["type"] for ev in value["events"]],
                         ["assistant/message", "assistant/message"])
        self.assertTrue(value["hasMore"])

    def test_history_rejects_bad_params(self):
        error = self._error(self.api.dispatch("session.history", "rid", {
            "sessionId": "x", "beforeSeq": -1}))
        self.assertEqual(error["code"], "bad-request")
        error = self._error(self.api.dispatch("session.history", "rid", {
            "sessionId": "x", "maxMessages": 0}))
        self.assertEqual(error["code"], "bad-request")


class TestSessionCancel(WebApiTest):
    def test_cancel_accepted(self):
        session_id = self._value(self._create())["sessionId"]
        value = self._value(self.api.dispatch("session.cancel", "rid",
                                              {"sessionId": session_id}))
        self.assertEqual(value, {"accepted": True})

    def test_cancel_unknown_session(self):
        error = self._error(self.api.dispatch("session.cancel", "rid",
                                              {"sessionId": "session-nope"}))
        self.assertEqual(error["code"], "session-not-found")

    def test_cancel_keeps_inbox(self):
        session_id = self._value(self._create())["sessionId"]
        self.api.dispatch("session.cancel", "rid", {"sessionId": session_id})
        # 队列 prompt 未运行：driver 空闲，followup 已入 inbox
        self._prompt_and_settle(session_id, "after cancel")
        types = [ev["type"] for ev in self.api.store.get(session_id).events]
        self.assertIn("user/message", types)


class TestSessionModels(WebApiTest):
    def test_models(self):
        session_id = self._value(self._create())["sessionId"]
        value = self._value(self.api.dispatch("session.models", "rid",
                                              {"sessionId": session_id}))
        self.assertEqual(value["current"], {"provider": "fake", "model": "fake-model"})
        self.assertIs(value["routable"], True)
        self.assertEqual(len(value["groups"]), 1)
        self.assertEqual(value["groups"][0]["id"], "fake")
        self.assertEqual(value["groups"][0]["models"], [{"id": "fake-model", "name": "fake-model"}])
        self.assertEqual(value["failures"], [])

    def test_models_unknown_session(self):
        error = self._error(self.api.dispatch("session.models", "rid",
                                              {"sessionId": "session-nope"}))
        self.assertEqual(error["code"], "session-not-found")


class TestDispatch(WebApiTest):
    def test_unknown_method(self):
        self.assertIsNone(self.api.dispatch("session.nope", "rid", {}))

    def test_methods_set(self):
        self.assertEqual(self.api.methods(), frozenset({
            "host.describe", "session.list", "session.create", "session.prompt",
            "session.history", "session.cancel", "session.models"}))

    def test_bad_payload_shape(self):
        error = self._error(self.api.dispatch("session.list", "rid", "nope"))
        self.assertEqual(error["code"], "bad-request")


class TestTimeZone(WebApiTest):
    def test_canonical(self):
        self.assertEqual(canonical_client_time_zone("UTC"), "UTC")
        self.assertIsNone(canonical_client_time_zone("Not/AZone"))
        self.assertIsNone(canonical_client_time_zone(""))
        self.assertIsNone(canonical_client_time_zone(42))
        try:
            from zoneinfo import ZoneInfo

            ZoneInfo("Asia/Shanghai")
            available = True
        except Exception:  # noqa: BLE001 - Windows 无 tzdata 时非 UTC 拒绝
            available = False
        if available:
            self.assertEqual(canonical_client_time_zone("Asia/Shanghai"), "Asia/Shanghai")


class TestToolTurn(WebApiTest):
    def setUp(self):
        from miniharness.cli.default_tools import default_tools

        self.ctx = Context(name="tool-test")
        adapter = FakeLlmAdapter(tool_call={"name": "bash", "arguments": {"command": "echo hi"}})
        adapter.model = "fake-model"
        self.api = WebApi(self.ctx, adapter, default_tools(self.ctx))

    def test_tool_call_turn(self):
        session_id = self._value(self._create())["sessionId"]
        async def go():
            response = self.api.dispatch("session.prompt", "rp1", {
                "sessionId": session_id, "mode": "queue",
                "content": [{"type": "text", "text": "run it"}],
            })
            self.assertEqual(self._value(response), {"accepted": True})
            await self.api._agents[session_id].when_idle_async()
        _run(go())
        types = [ev["type"] for ev in self.api.store.get(session_id).events]
        self.assertIn("tool/call", types)
        self.assertIn("tool/result", types)


if __name__ == "__main__":
    unittest.main()