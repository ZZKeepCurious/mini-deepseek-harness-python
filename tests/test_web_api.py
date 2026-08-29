"""web 会话服务测试：WebApi unary + follow/control 流订阅（对齐 packages/api/session-controller）。

运行方式：unittest 发现；无第三方依赖（api 层 stdlib-only）。
"""
import asyncio
import base64
import os
import tempfile
import unittest

from miniharness.core.scope import Context
from miniharness.llm.fake import FakeLlmAdapter
from miniharness.web.api import (
    SESSION_SEARCH_RESULT_LIMIT,
    SESSION_SEARCH_SNIPPET_LENGTH,
    WebApi,
    canonical_client_time_zone,
    _truncate_code_points,
)

CWD = os.getcwd()

# 1x1 PNG（canonical base64）
TINY_PNG = ("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8"
            "z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


def _run(coro):
    return asyncio.run(coro)


def _fake(model: str = "fake-model") -> FakeLlmAdapter:
    adapter = FakeLlmAdapter()
    adapter.model = model
    return adapter


class WebApiTest(unittest.TestCase):
    """基类：无 attachment 服务的瘦 api（ATTACHMENT_UNAVAILABLE 路径）。"""

    with_attachments = False

    def setUp(self):
        self.ctx = Context(name="web-api-test")
        if self.with_attachments:
            tmp = tempfile.TemporaryDirectory()
            self._tmp = tmp
            self.addCleanup(tmp.cleanup)
            from miniharness.attachment.store import LocalAttachmentStore
            self.ctx.provide("attachments", LocalAttachmentStore(root=tmp.name, limits=None))
        self.api = WebApi(self.ctx, _fake())

    def tearDown(self):
        self.ctx.dispose()

    def _create(self, rpc_id="r1", cwd=CWD, session_id=None, agent_preset=None,
                workspace_id=None):
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

    def _prompt_and_settle(self, session_id, text, mode="queue", rpc_id="rp1",
                           request_id="req-1"):
        async def go():
            response = self.api.dispatch("session.prompt", rpc_id, {
                "sessionId": session_id, "mode": mode, "requestId": request_id,
                "content": [{"type": "text", "text": text}],
            })
            self.assertEqual(self._value(response), {"accepted": True})
            await self.api._agents[session_id].when_idle_async()
            return response

        return _run(go())

    def _drain(self, sub):
        frames = []
        while True:
            frame = sub.pull()
            if frame is None:
                return frames
            frames.append(frame)


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

    def test_create_preallocated_cwd_conflict(self):
        self._create(session_id="session-abc", cwd=CWD)
        error = self._error(self._create(session_id="session-abc",
                                         cwd=os.path.dirname(CWD)))
        self.assertEqual(error["code"], "session-conflict")
        self.assertIn('belongs to', error["message"])
        self.assertEqual(error["details"]["sessionId"], "session-abc")
        self.assertEqual(error["details"]["existingCwd"], CWD)

    def test_create_preset_conflict(self):
        self._create(session_id="session-abc", agent_preset="a")
        error = self._error(self._create(session_id="session-abc", agent_preset="b"))
        self.assertEqual(error["code"], "agent-preset-conflict")
        self.assertEqual(error["details"]["sessionId"], "session-abc")

    def test_create_workspace_plus_cwd_rejected(self):
        error = self._error(self._create(workspace_id="w-1", cwd=CWD))
        self.assertEqual(error["code"], "bad-request")
        self.assertIn("not both", error["message"])

    def test_create_workspace_only_not_found(self):
        error = self._error(self.api.dispatch("session.create", "r1",
                                              {"workspaceId": "w-1"}))
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
        self._prompt_and_settle(session_id, "hello")
        types = [ev["type"] for ev in self.api.store.get(session_id).events]
        self.assertIn("user/message", types)
        self.assertIn("assistant/message", types)

    def test_prompt_source_carries_request_id_and_time_zone(self):
        session_id = self._value(self._create())["sessionId"]
        async def go():
            response = self.api.dispatch("session.prompt", "rp-tz", {
                "sessionId": session_id, "mode": "queue", "requestId": "req-tz",
                "content": [{"type": "text", "text": "hi"}], "clientTimeZone": "UTC",
            })
            self.assertEqual(self._value(response), {"accepted": True})
            await self.api._agents[session_id].when_idle_async()
        _run(go())
        user = next(ev for ev in self.api.store.get(session_id).events
                    if ev["type"] == "user/message")
        self.assertEqual(user["data"]["source"]["kind"], "user")
        self.assertEqual(user["data"]["source"]["rpcId"], "req-tz")
        self.assertEqual(user["data"]["source"]["clientTimeZone"], "UTC")

    def test_prompt_steer_mode(self):
        session_id = self._value(self._create())["sessionId"]
        async def go():
            response = self.api.dispatch("session.prompt", "rp1", {
                "sessionId": session_id, "mode": "steer", "requestId": "req-steer",
                "content": [{"type": "text", "text": "steer me"}],
            })
            self.assertEqual(self._value(response), {"accepted": True})
            await self.api._agents[session_id].when_idle_async()
        _run(go())
        self.assertIn("user/message",
                      [ev["type"] for ev in self.api.store.get(session_id).events])

    def test_prompt_slash_text_is_plain_content(self):
        # 命令路由已随 apiproxy 契约整体移除：'/' 前缀文本就是普通提示词
        session_id = self._value(self._create())["sessionId"]
        install_commands = None
        try:
            from miniharness.commands import install_commands as _ic
            install_commands = _ic
        except ImportError:
            pass
        if install_commands is not None:
            install_commands(self.ctx)
        self._prompt_and_settle(session_id, "/greet world")
        types = [ev["type"] for ev in self.api.store.get(session_id).events]
        self.assertIn("user/message", types)
        self.assertNotIn("command/run", types)

    def test_prompt_unknown_session(self):
        error = self._error(self.api.dispatch("session.prompt", "rid", {
            "sessionId": "session-nope", "mode": "queue", "requestId": "req-1",
            "content": [{"type": "text", "text": "hi"}],
        }))
        self.assertEqual(error["code"], "session-not-found")
        self.assertEqual(error["details"], {"sessionId": "session-nope"})

    def test_prompt_requires_request_id(self):
        error = self._error(self.api.dispatch("session.prompt", "rid", {
            "sessionId": "x", "mode": "queue",
            "content": [{"type": "text", "text": "hi"}],
        }))
        self.assertEqual(error["code"], "bad-request")

    def test_prompt_invalid_mode(self):
        error = self._error(self.api.dispatch("session.prompt", "rid", {
            "sessionId": "x", "mode": "explode", "requestId": "req-1",
            "content": [{"type": "text", "text": "hi"}],
        }))
        self.assertEqual(error["code"], "bad-request")

    def test_prompt_invalid_time_zone(self):
        session_id = self._value(self._create())["sessionId"]
        error = self._error(self.api.dispatch("session.prompt", "rid", {
            "sessionId": session_id, "mode": "queue", "requestId": "req-1",
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
            "sessionId": session_id, "mode": "queue", "requestId": "req-img",
            "content": [{"type": "image", "mediaType": "image/png", "data": TINY_PNG}],
        }))
        self.assertEqual(error["code"], "attachment-error")
        self.assertEqual(error["details"], {"reason": "MODEL_DOES_NOT_SUPPORT_IMAGES"})

    def test_prompt_image_without_attachment_service(self):
        session_id = self._value(self._create())["sessionId"]
        error = self._error(self.api.dispatch("session.prompt", "rid", {
            "sessionId": session_id, "mode": "queue", "requestId": "req-img",
            "content": [{"type": "image", "mediaType": "image/png", "data": TINY_PNG}],
        }))
        self.assertEqual(error["code"], "attachment-error")
        self.assertEqual(error["details"], {"reason": "ATTACHMENT_UNAVAILABLE"})


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

    def test_list_has_no_agent_preset_field(self):
        self._create(session_id="session-meta", agent_preset="standard")
        item = self._value(self.api.dispatch("session.list", "rid", {}))["items"][0]
        self.assertEqual(item["cwd"], CWD)
        self.assertNotIn("agentPreset", item)

    def test_list_parent_session_fields(self):
        self._create(session_id="session-par")
        child = self.api.store.prepare("session-child", {
            "seed": [],
            "meta": {"cwd": CWD, "parentSession": "session-par", "origin": "subagent"}})
        self.api._attach(child)
        items = self._value(self.api.dispatch("session.list", "rid", {}))["items"]
        item = next(i for i in items if i["sessionId"] == "session-child")
        self.assertEqual(item["parentSessionId"], "session-par")
        self.assertEqual(item["origin"], "subagent")

    def test_list_sorted_desc(self):
        self._create(session_id="session-a")
        self._create(session_id="session-b")
        items = self._value(self.api.dispatch("session.list", "rid", {}))["items"]
        self.assertEqual(len(items), 2)
        self.assertGreaterEqual(items[0]["updatedAt"], items[1]["updatedAt"])


class TestSessionSearch(WebApiTest):
    def test_search_finds_prompt_text_case_insensitive(self):
        session_id = self._value(self._create())["sessionId"]
        self._prompt_and_settle(session_id, "Hello DeepSeek world")
        value = self._value(self.api.dispatch("session.search", "rid",
                                              {"query": "  deepseek  "}))
        self.assertFalse(value["hasMore"])
        hit = next(i for i in value["items"] if i["sessionId"] == session_id)
        self.assertIn("deepseek", hit["snippet"].lower())

    def test_search_empty_query(self):
        error = self._error(self.api.dispatch("session.search", "rid", {"query": "   "}))
        self.assertEqual(error["code"], "bad-request")
        self.assertIn("must not be empty", error["message"])

    def test_search_nul_query(self):
        error = self._error(self.api.dispatch("session.search", "rid", {"query": "a\0b"}))
        self.assertEqual(error["code"], "bad-request")

    def test_search_overlong_query(self):
        error = self._error(self.api.dispatch("session.search", "rid",
                                              {"query": "a" * 501}))
        self.assertEqual(error["code"], "bad-request")
        self.assertIn("500", error["message"])

    def test_search_snippet_truncated(self):
        session_id = self._value(self._create())["sessionId"]
        self._prompt_and_settle(session_id, "needle " + "长" * 400)
        value = self._value(self.api.dispatch("session.search", "rid", {"query": "needle"}))
        hit = next(i for i in value["items"] if i["sessionId"] == session_id)
        self.assertLessEqual(len(hit["snippet"]), SESSION_SEARCH_SNIPPET_LENGTH)
        self.assertIn("needle", hit["snippet"])


class TestSessionSelectModel(WebApiTest):
    def test_catalog_advertises_adapter_route(self):
        session_id = self._value(self._create())["sessionId"]
        value = self._value(self.api.dispatch("session.modelCatalog", "rid", {}))
        self.assertEqual(value["default"], {"provider": "fake", "model": "fake-model"})
        self.assertEqual(value["routableProviders"], ["fake"])
        self.assertEqual(value["groups"][0]["models"], [{"id": "fake-model", "name": "fake-model"}])
        self.assertEqual(value["failures"], [])

    def test_select_model_adapter_route(self):
        session_id = self._value(self._create())["sessionId"]
        value = self._value(self.api.dispatch("session.selectModel", "rid", {
            "sessionId": session_id, "provider": "fake", "model": "fake-model",
        }))
        self.assertEqual(value["selected"], {"provider": "fake", "model": "fake-model"})

    def test_select_model_unknown_route(self):
        session_id = self._value(self._create())["sessionId"]
        error = self._error(self.api.dispatch("session.selectModel", "rid", {
            "sessionId": session_id, "provider": "other", "model": "x",
        }))
        self.assertEqual(error["code"], "model-unavailable")

    def test_select_model_unknown_session(self):
        error = self._error(self.api.dispatch("session.selectModel", "rid", {
            "sessionId": "session-nope", "provider": "fake", "model": "fake-model",
        }))
        self.assertEqual(error["code"], "session-not-found")


class TestWorkspacePath(WebApiTest):
    def test_can_open_never(self):
        self.assertIs(self._value(self.api.dispatch(
            "session.canOpenWorkspacePath", "rid", {})), False)

    def test_open_requires_path(self):
        error = self._error(self.api.dispatch("session.openWorkspacePath", "rid", {}))
        self.assertEqual(error["code"], "bad-request")

    def test_open_always_internal(self):
        error = self._error(self.api.dispatch("session.openWorkspacePath", "rid",
                                              {"path": CWD}))
        self.assertEqual(error["code"], "internal")


class TestSessionRename(WebApiTest):
    def test_rename_unavailable(self):
        session_id = self._value(self._create())["sessionId"]
        error = self._error(self.api.dispatch("session.rename", "rid", {
            "sessionId": session_id, "title": "t"}))
        self.assertEqual(error["code"], "internal")
        self.assertIn("no session-title service", error["message"])

    def test_rename_unknown_session(self):
        error = self._error(self.api.dispatch("session.rename", "rid", {
            "sessionId": "session-nope", "title": "t"}))
        self.assertEqual(error["code"], "session-not-found")


class TestSessionFork(WebApiTest):
    def test_fork_after_turn(self):
        session_id = self._value(self._create())["sessionId"]
        self._prompt_and_settle(session_id, "first")
        value = self._value(self.api.dispatch("session.fork", "rid",
                                              {"sessionId": session_id}))
        child_id = value["sessionId"]
        child = self.api.store.get(child_id)
        self.assertIsNotNone(child)
        self.assertEqual(child.meta["parentSession"], session_id)
        self.assertEqual(child.meta["cwd"], CWD)
        self.assertGreater(len(child.surface_nodes()), 0)

    def test_fork_anchored_within_completed_turn(self):
        session_id = self._value(self._create())["sessionId"]
        self._prompt_and_settle(session_id, "first")
        user = next(ev for ev in self.api.store.get(session_id).events
                    if ev["type"] == "user/message")
        value = self._value(self.api.dispatch("session.fork", "rid", {
            "sessionId": session_id, "atSeq": user["seq"]}))
        child = self.api.store.get(value["sessionId"])
        self.assertIsNotNone(child)
        self.assertGreaterEqual(child.meta["seedLength"], user["seq"] + 1)

    def test_fork_no_completed_turn(self):
        session_id = self._value(self._create())["sessionId"]
        error = self._error(self.api.dispatch("session.fork", "rid",
                                              {"sessionId": session_id}))
        self.assertEqual(error["code"], "fork-unavailable")
        self.assertIn("no completed turn", error["message"])

    def test_fork_at_seq_beyond_log(self):
        session_id = self._value(self._create())["sessionId"]
        self._prompt_and_settle(session_id, "first")
        # atSeq 越界 → 锚定最后一个已完成 turn（上游 commands.ts fork 的
        # boundary ?? (atSeq > lastSeq ? findLast(turn/end)) 分支）
        value = self._value(self.api.dispatch("session.fork", "rid", {
            "sessionId": session_id, "atSeq": 999}))
        child = self.api.store.get(value["sessionId"])
        self.assertIsNotNone(child)
        self.assertEqual(child.meta["parentSession"], session_id)
        self.assertGreaterEqual(child.meta["seedLength"],
                               len(self.api.store.get(session_id).events))

    def test_fork_unknown_session(self):
        error = self._error(self.api.dispatch("session.fork", "rid",
                                              {"sessionId": "session-nope"}))
        self.assertEqual(error["code"], "session-not-found")

    def test_fork_bad_at_seq(self):
        session_id = self._value(self._create())["sessionId"]
        error = self._error(self.api.dispatch("session.fork", "rid", {
            "sessionId": session_id, "atSeq": -3}))
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
        self.assertIn("not attached", error["message"])

    def test_cancel_keeps_inbox(self):
        session_id = self._value(self._create())["sessionId"]
        self.api.dispatch("session.cancel", "rid", {"sessionId": session_id})
        self._prompt_and_settle(session_id, "after cancel")
        types = [ev["type"] for ev in self.api.store.get(session_id).events]
        self.assertIn("user/message", types)


class TestSessionUpdateQueue(WebApiTest):
    def _queue_then(self, session_id, action_fn, text="first", mode="queue",
                    request_id="req-q", rpc_id="rp1"):
        """在 asyncio 内先排队（不结算）再执行 action_fn((item_id) -> 值)。"""

        async def go():
            response = self.api.dispatch("session.prompt", rpc_id, {
                "sessionId": session_id, "mode": mode, "requestId": request_id,
                "content": [{"type": "text", "text": text}],
            })
            self.assertEqual(self._value(response), {"accepted": True})
            target = self.api._agents[session_id].inbox.next_turn \
                if mode == "queue" else self.api._agents[session_id].inbox.next_step
            item_id = target[0]["id"]
            value = action_fn(item_id)
            await self.api._agents[session_id].when_idle_async()
            return value

        return _run(go())

    def test_remove_queued(self):
        session_id = self._value(self._create())["sessionId"]

        def action(item_id):
            value = self._value(self.api.dispatch("session.updateQueue", "rid", {
                "sessionId": session_id, "itemId": item_id,
                "action": {"kind": "remove"}}))
            self.assertEqual(value, {"accepted": True})
            self.assertEqual(self.api._agents[session_id].inbox.next_turn, [])

        self._queue_then(session_id, action)

    def test_edit_queued(self):
        session_id = self._value(self._create())["sessionId"]

        def action(item_id):
            value = self._value(self.api.dispatch("session.updateQueue", "rid", {
                "sessionId": session_id, "itemId": item_id,
                "action": {"kind": "edit",
                           "content": [{"type": "text", "text": "edited"}]}}))
            self.assertEqual(value, {"accepted": True})

        self._queue_then(session_id, action)
        user = next(ev for ev in self.api.store.get(session_id).events
                    if ev["type"] == "user/message")
        from miniharness.core.session.json import thaw
        self.assertEqual(thaw(user["data"]["content"]), [{"type": "text", "text": "edited"}])

    def test_steer_unavailable_when_idle(self):
        session_id = self._value(self._create())["sessionId"]

        def action(item_id):
            return self._error(self.api.dispatch("session.updateQueue", "rid", {
                "sessionId": session_id, "itemId": item_id,
                "action": {"kind": "steer"}}))

        error = self._queue_then(session_id, action, mode="steer")
        # next-step 条目不允许再 steer（steer 只认 next-turn 且 turn 须 running）
        self.assertEqual(error["code"], "steer-unavailable")

    def test_edit_non_text_rejected(self):
        session_id = self._value(self._create())["sessionId"]

        def action(item_id):
            return self._error(self.api.dispatch("session.updateQueue", "rid", {
                "sessionId": session_id, "itemId": item_id,
                "action": {"kind": "edit",
                           "content": [{"type": "image", "mediaType": "image/png",
                                        "data": TINY_PNG}]}}))

        error = self._queue_then(session_id, action)
        self.assertEqual(error["code"], "attachment-error")
        self.assertEqual(error["details"], {"reason": "QUEUE_EDIT_NON_TEXT"})

    def test_unknown_session_queue_item_not_found(self):
        error = self._error(self.api.dispatch("session.updateQueue", "rid", {
            "sessionId": "session-nope", "itemId": "x",
            "action": {"kind": "remove"}}))
        self.assertEqual(error["code"], "queue-item-not-found")

    def test_stale_item_queue_item_not_found(self):
        session_id = self._value(self._create())["sessionId"]
        error = self._error(self.api.dispatch("session.updateQueue", "rid", {
            "sessionId": session_id, "itemId": "stale",
            "action": {"kind": "remove"}}))
        self.assertEqual(error["code"], "queue-item-not-found")
        self.assertEqual(error["details"], {"itemId": "stale"})


class TestSessionPage(WebApiTest):
    def _append(self, session_id, type_, surface_op="append", source_event_seqs=None,
                **data):
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

    def _address(self, session_id):
        return {"address": {"kind": "session", "sessionId": session_id}}

    def test_page_tail_page_boundaries(self):
        self._create(session_id="session-h")
        self._append("session-h", "user/message", message={"id": "m1"})
        self._append("session-h", "tool/call", tool_call={"id": "t1"})
        self._append("session-h", "tool/result", tool_result={"id": "r1"})
        self._append("session-h", "assistant/message", message={"id": "m2"})
        self._append("session-h", "user/message", message={"id": "m3"})
        last = self.api.store.get("session-h").seq - 1
        value = self._value(self.api.dispatch("session.page", "rid", {
            **self._address("session-h"), "throughSeq": last,
            "maxMessages": 1}))
        self.assertEqual(len(value["records"]), 1)
        self.assertEqual(value["records"][0]["type"], "event")
        self.assertEqual(value["records"][0]["event"]["type"], "user/message")
        self.assertTrue(value["hasMore"])

    def test_page_all_when_unbounded(self):
        self._create(session_id="session-h")
        self._append("session-h", "user/message", message={"id": "m1"})
        self._append("session-h", "assistant/message", message={"id": "m2"})
        last = self.api.store.get("session-h").seq - 1
        value = self._value(self.api.dispatch("session.page", "rid", {
            **self._address("session-h"), "throughSeq": last}))
        self.assertEqual(len(value["records"]), 2)
        self.assertFalse(value["hasMore"])

    def test_page_before_seq(self):
        self._create(session_id="session-h")
        self._append("session-h", "user/message", message={"id": "m1"})
        self._append("session-h", "assistant/message", message={"id": "m2"})
        self._append("session-h", "user/message", message={"id": "m3"})
        last = self.api.store.get("session-h").seq - 1
        value = self._value(self.api.dispatch("session.page", "rid", {
            **self._address("session-h"), "throughSeq": 2, "beforeSeq": 2,
            "maxMessages": 1}))
        self.assertEqual(len(value["records"]), 1)
        self.assertEqual(value["records"][0]["event"]["type"], "assistant/message")
        self.assertTrue(value["hasMore"])

    def test_page_replacement_consumes_no_quota(self):
        self._create(session_id="session-h")
        self._append("session-h", "user/message", message={"id": "m1"})
        self._append("session-h", "assistant/message", message={"id": "m2"})
        self._append("session-h", "assistant/message", message={"id": "m2r"},
                     surface_op={"op": "replace", "start": 0, "end": 1},
                     source_event_seqs=[0, 1])
        last = self.api.store.get("session-h").seq - 1
        value = self._value(self.api.dispatch("session.page", "rid", {
            **self._address("session-h"), "throughSeq": last, "maxMessages": 1}))
        self.assertEqual([r["event"]["type"] for r in value["records"]],
                         ["assistant/message", "assistant/message"])
        self.assertTrue(value["hasMore"])

    def test_page_chunk_rows(self):
        self._create(session_id="session-chunks")
        session = self.api.store.get("session-chunks")
        for _ in range(3):
            session.append("assistant/chunk", {
                "turn": 1, "step": 1,
                "chunk": {"type": "text-delta", "index": 0, "text": "ab"}})
        last = session.seq - 1
        value = self._value(self.api.dispatch("session.page", "rid", {
            **self._address("session-chunks"), "throughSeq": last}))
        record = value["records"][0]
        self.assertEqual(record["type"], "chunks")
        self.assertEqual(record["event"]["type"], "chunkrow/text-chunks")
        self.assertEqual(record["event"]["seq"], 0)
        self.assertEqual(record["event"]["data"]["texts"], ["ab", "ab", "ab"])

    def test_page_unknown_session(self):
        error = self._error(self.api.dispatch("session.page", "rid", {
            **self._address("session-nope"), "throughSeq": 0}))
        self.assertEqual(error["code"], "session-not-found")

    def test_page_through_past_cursor(self):
        self._create(session_id="session-h")
        error = self._error(self.api.dispatch("session.page", "rid", {
            **self._address("session-h"), "throughSeq": 5}))
        self.assertEqual(error["code"], "bad-request")
        self.assertIn("past cursor", error["message"])

    def test_page_validation(self):
        self._create(session_id="session-h")
        error = self._error(self.api.dispatch("session.page", "rid", {
            **self._address("session-h"), "throughSeq": -5}))
        self.assertEqual(error["code"], "bad-request")
        error = self._error(self.api.dispatch("session.page", "rid", {
            **self._address("session-h"), "throughSeq": 0, "maxMessages": 0}))
        self.assertEqual(error["code"], "bad-request")

    def test_page_subagent_address_rejected(self):
        self._create(session_id="session-p")
        error = self._error(self.api.dispatch("session.page", "rid", {
            "address": {"kind": "subagent", "parentSessionId": "session-p",
                        "childSessionId": "session-c"},
            "throughSeq": 0}))
        self.assertEqual(error["code"], "session-not-found")


class TestSessionFollow(WebApiTest):
    def test_snapshot_shape(self):
        session_id = self._value(self._create())["sessionId"]
        sub = self.api.follow({"address": {"kind": "session", "sessionId": session_id},
                               "maxMessages": 5})
        self.assertIsNone(sub.error)
        snapshot = sub.pull()
        self.assertEqual(snapshot["type"], "snapshot")
        self.assertEqual(snapshot["header"]["id"], session_id)
        self.assertEqual(snapshot["cursor"], -1)
        self.assertEqual(snapshot["records"], [])
        self.assertIs(snapshot["hasMore"], False)
        self.assertEqual(snapshot["projections"],
                         {"asOfSeq": -1, "values": {}})
        sub.close()

    def test_streams_live_events(self):
        session_id = self._value(self._create())["sessionId"]
        sub = self.api.follow({"address": {"kind": "session", "sessionId": session_id}})
        self.assertIsNotNone(sub.pull())  # snapshot
        self._prompt_and_settle(session_id, "hello")
        frames = self._drain(sub)
        self.assertGreaterEqual(len(frames), 2)
        last_seen = -1
        for frame in frames:
            self.assertEqual(frame["type"], "event")
            self.assertGreater(frame["event"]["seq"], last_seen)
            last_seen = frame["event"]["seq"]
        sub.close()

    def test_unknown_session_fails_subscription(self):
        sub = self.api.follow({"address": {"kind": "session",
                                           "sessionId": "session-nope"}})
        self.assertIsNotNone(sub.error)
        self.assertEqual(sub.error["code"], "session-not-found")
        self.assertIsNone(sub.pull())

    def test_bad_max_messages_fails_subscription(self):
        session_id = self._value(self._create())["sessionId"]
        sub = self.api.follow({"address": {"kind": "session", "sessionId": session_id},
                               "maxMessages": 0})
        self.assertEqual(sub.error["code"], "bad-request")

    def test_close_stops_stream(self):
        session_id = self._value(self._create())["sessionId"]
        sub = self.api.follow({"address": {"kind": "session", "sessionId": session_id}})
        sub.pull()
        sub.close()
        self.assertTrue(sub.done)


class TestSessionControl(WebApiTest):
    def test_baseline_empty(self):
        session_id = self._value(self._create())["sessionId"]
        sub = self.api.control()
        baseline = sub.pull()
        self.assertEqual(baseline["type"], "baseline")
        self.assertEqual(baseline["value"]["queues"], {session_id: []})
        self.assertEqual(baseline["value"]["jobs"], {session_id: []})
        self.assertEqual(baseline["value"]["projections"],
                         {session_id: {"asOfSeq": -1, "values": {}}})
        self.assertEqual(self._drain(sub), [])
        sub.close()

    def test_queue_frame_on_prompt(self):
        session_id = self._value(self._create())["sessionId"]
        sub = self.api.control()
        baseline = sub.pull()
        self.assertEqual(baseline["value"]["queues"][session_id], [])
        self.api.dispatch("session.prompt", "rp1", {
            "sessionId": session_id, "mode": "queue", "requestId": "req-q",
            "content": [{"type": "text", "text": "first"}]})
        frames = self._drain(sub)
        queue = next(f for f in frames if f["type"] == "queue")
        self.assertEqual(queue["sessionId"], session_id)
        self.assertEqual(len(queue["items"]), 1)
        self.assertEqual(queue["items"][0]["placement"], "queued")
        self.assertEqual(queue["items"][0]["rpcId"], "req-q")
        self.assertEqual(queue["items"][0]["message"]["content"],
                         [{"type": "text", "text": "first"}])
        sub.close()

    def test_steered_item_placement(self):
        session_id = self._value(self._create())["sessionId"]
        sub = self.api.control()
        sub.pull()
        self.api.dispatch("session.prompt", "rp1", {
            "sessionId": session_id, "mode": "steer", "requestId": "req-s",
            "content": [{"type": "text", "text": "go now"}]})
        frames = self._drain(sub)
        queue = next(f for f in frames if f["type"] == "queue")
        # steer 源是 user（带 rpcId），落 next-step → placement 'steering'
        self.assertEqual(queue["items"][0]["placement"], "steering")
        self.assertEqual(queue["items"][0]["rpcId"], "req-s")
        sub.close()


class TestSessionAttachment(WebApiTest):
    with_attachments = True

    def _prompt_image_and_settle(self, session_id, rpc_id="img1"):
        async def go():
            response = self.api.dispatch("session.prompt", rpc_id, {
                "sessionId": session_id, "mode": "queue", "requestId": "req-img",
                "content": [{"type": "image", "mediaType": "image/png", "data": TINY_PNG}],
            })
            self.assertEqual(self._value(response), {"accepted": True})
            await self.api._agents[session_id].when_idle_async()
        _run(go())

    def test_prompt_admits_image(self):
        session_id = self._value(self._create())["sessionId"]
        self._prompt_image_and_settle(session_id)
        user = next(ev for ev in self.api.store.get(session_id).events
                    if ev["type"] == "user/message")
        # user/message 事件的 data 即消息本体（surface.ts deriveEventMessage）
        block = user["data"]["content"][0]
        self.assertEqual(block["type"], "image")
        self.assertIn("attachment", block)
        self.api.attachments.read_image(self.api._ref_from_dict(block["attachment"]))

    def test_attachment_returns_bytes(self):
        session_id = self._value(self._create())["sessionId"]
        self._prompt_image_and_settle(session_id)
        user = next(ev for ev in self.api.store.get(session_id).events
                    if ev["type"] == "user/message")
        attachment_id = user["data"]["content"][0]["attachment"]["attachmentId"]
        value = self._value(self.api.dispatch("session.attachment", "rid", {
            "sessionId": session_id, "attachmentId": attachment_id}))
        self.assertEqual(value["attachment"]["attachmentId"], attachment_id)
        payload = base64.b64decode(value["data"])
        self.assertGreater(len(payload), 0)

    def test_attachment_not_referenced(self):
        session_id = self._value(self._create())["sessionId"]
        error = self._error(self.api.dispatch("session.attachment", "rid", {
            "sessionId": session_id, "attachmentId": "att-nope"}))
        self.assertEqual(error["code"], "attachment-error")
        self.assertEqual(error["details"], {"reason": "ATTACHMENT_NOT_REFERENCED"})

    def test_attachment_unknown_session(self):
        error = self._error(self.api.dispatch("session.attachment", "rid", {
            "sessionId": "session-nope", "attachmentId": "att-1"}))
        self.assertEqual(error["code"], "session-not-found")


class TestAttachmentUnavailable(WebApiTest):
    def test_referenced_without_service(self):
        session_id = self._value(self._create())["sessionId"]
        session = self.api.store.get(session_id)
        session.append("user/message", {
            "id": "mid", "role": "user",
            "content": [{"type": "image", "attachment": {
                "attachmentId": "att-1", "mediaType": "image/png",
                "bytes": 4, "width": 1, "height": 1}}],
            "source": {"kind": "user"},
        }, surfaceOp="append")
        error = self._error(self.api.dispatch("session.attachment", "rid", {
            "sessionId": session_id, "attachmentId": "att-1"}))
        self.assertEqual(error["code"], "attachment-error")
        self.assertEqual(error["details"], {"reason": "ATTACHMENT_UNAVAILABLE"})


class TestDispatch(WebApiTest):
    def test_unknown_method(self):
        self.assertIsNone(self.api.dispatch("session.nope", "rid", {}))

    def test_methods_set(self):
        self.assertEqual(self.api.methods(), frozenset({
            "session.list", "session.search", "session.create", "session.selectModel",
            "session.modelCatalog", "session.canOpenWorkspacePath",
            "session.openWorkspacePath", "session.rename", "session.fork",
            "session.prompt", "session.attachment", "session.updateQueue",
            "session.cancel", "session.page"}))

    def test_bad_payload_shape(self):
        error = self._error(self.api.dispatch("session.list", "rid", "nope"))
        self.assertEqual(error["code"], "bad-request")


class TestSessionSearchConstants(WebApiTest):
    def test_limits(self):
        self.assertEqual(SESSION_SEARCH_RESULT_LIMIT, 20)
        self.assertEqual(SESSION_SEARCH_SNIPPET_LENGTH, 240)
        self.assertEqual(_truncate_code_points("abcdef", 3), "abc")
        pairs = "a😀b"
        self.assertEqual(_truncate_code_points(pairs, 2), "a😀")


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

    def tearDown(self):
        self.ctx.dispose()

    def test_tool_call_turn(self):
        session_id = self._value(self._create())["sessionId"]
        async def go():
            response = self.api.dispatch("session.prompt", "rp1", {
                "sessionId": session_id, "mode": "queue", "requestId": "req-tool",
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