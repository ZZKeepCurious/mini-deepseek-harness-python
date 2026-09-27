"""command-feedback + message-feedback（M12）验收。

对齐上游 `packages/feedback/command-feedback/tests/command-feedback.spec.ts` +
`packages/feedback/message-feedback/tests/message-feedback.spec.ts`：

- command-feedback：/feedback 命令（recordInput:false、空输入拒绝、成功两行
  ack 含匿名用户 id）+ record_feedback（trim、省略空 text/无 category、
  `{}` 也记）+ sessionFeedback Remote（session-not-found）
- message-feedback：note 校验先于一切、目标校验（append-origin 非空
  assistant/message）、版本 CAS（ifVersion null=create-only / UUID=精确匹配）、
  无操作不追加事件、note/category 省略丢键、delete 幂等恒 {absent:true}
"""
import os
import tempfile
import unittest

from miniharness.commands import CommandRegistry, install_commands
from miniharness.core.scope import Context
from miniharness.core.session import create_message, text_block
from miniharness.core.session.persistence import JsonlPersistence
from miniharness.core.session_store import install_sessions
from miniharness.feedback import (
    FEEDBACK_CATEGORIES,
    install_command_feedback,
    install_message_feedback,
    record_feedback,
)
from miniharness.identity import (
    ANONYMOUS_USER_ID_FILE_NAME,
    get_or_create_anonymous_user_id,
)


def _assistant_message(session, text="assistant answer"):
    """往日志里落一条 append-origin 非空 assistant/message（feedback 目标）。

    返回落盘消息的 id（feedback 目标校验按真实消息 id 匹配）。
    """
    message = create_message("assistant", [text_block(text)])
    session.append("assistant/message", {
        "turn": 1, "step": 1, "message": message, "stream": [],
    }, surfaceOp="append")
    return message["id"]


class CommandFeedbackTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="feedback-cmd")
        self.store = install_sessions(self.ctx)
        self.commands = install_commands(self.ctx)
        self.session = self.store.create("s1", {"meta": {"cwd": os.getcwd()}})

    def tearDown(self):
        self.ctx.dispose()

    def test_categories_shared(self):
        self.assertEqual(FEEDBACK_CATEGORIES, (
            "task-result", "instruction-following", "product-interaction",
            "service-stability", "resource-cost", "security-privacy-permission", "other"))

    def test_record_feedback_trims_text(self):
        record_feedback(self.session, {"text": "  the diff view is unreadable  "})
        event = next(e for e in self.session.events if e["type"] == "feedback/record")
        self.assertEqual(event["data"], {"text": "the diff view is unreadable"})

    def test_record_feedback_omits_empty_text_and_missing_category(self):
        record_feedback(self.session, {"sessionId": self.session.session_id})
        event = next(e for e in self.session.events if e["type"] == "feedback/record")
        self.assertEqual(event["data"], {})
        # log-only 非 surface
        self.assertNotIn("surfaceOp", event)

    def test_feedback_record_is_log_only(self):
        record_feedback(self.session, {"text": "t"})
        event = next(e for e in self.session.events if e["type"] == "feedback/record")
        self.assertNotIn("surfaceOp", event)

    def test_command_registered_with_record_input_false(self):
        install_command_feedback(self.ctx)
        self.assertIn("feedback", self.commands.names())
        # command/run 不带 args
        result = self.commands.dispatch(self._fake_agent(), "/feedback  hello world ")
        self.assertEqual(result["kind"], "success")
        run = next(e for e in self.session.events if e["type"] == "command/run")
        self.assertNotIn("args", run["data"])
        self.assertEqual(run["data"]["name"], "feedback")
        done = next(e for e in self.session.events if e["type"] == "command/done")
        self.assertEqual(done["data"]["kind"], "success")
        record = next(e for e in self.session.events if e["type"] == "feedback/record")
        self.assertEqual(record["data"]["text"], "hello world")

    def test_command_empty_input_rejected(self):
        install_command_feedback(self.ctx)
        result = self.commands.dispatch(self._fake_agent(), "/feedback    \n\t ")
        self.assertEqual(result["kind"], "error")
        self.assertEqual(result["text"],
                         "Feedback text is required. Usage: /feedback <text>")
        self.assertFalse(any(e["type"] == "feedback/record" for e in self.session.events))
        done = next(e for e in self.session.events if e["type"] == "command/done")
        self.assertEqual(done["data"]["kind"], "error")

    def test_command_ack_contains_anonymous_user(self):
        install_command_feedback(self.ctx)
        result = self.commands.dispatch(self._fake_agent(), "/feedback nice work")
        user_id = get_or_create_anonymous_user_id()
        self.assertEqual(result["text"],
                         f"Feedback recorded for session s1\nAnonymous user: {user_id}.")

    def _fake_agent(self):
        class _Agent:
            session = self.session
        return _Agent()


class SessionFeedbackRemoteTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="feedback-remote")
        self.store = install_sessions(self.ctx)
        install_commands(self.ctx)
        install_command_feedback(self.ctx)
        self.service = self.ctx.get("sessionFeedback")

    def tearDown(self):
        self.ctx.dispose()

    def test_record_returns_confirmation(self):
        session = self.store.create("s2", {"meta": {"cwd": os.getcwd()}})
        result = self.service.record({"sessionId": "s2", "text": "  ok  ",
                                      "category": "task-result"})
        self.assertEqual(result, {"recorded": True})
        event = next(e for e in session.events if e["type"] == "feedback/record")
        self.assertEqual(event["data"], {"text": "ok", "category": "task-result"})

    def test_record_missing_session(self):
        with self.assertRaises(Exception) as raised:
            self.service.record({"sessionId": "nope"})
        self.assertEqual(getattr(raised.exception, "code", None), "session-not-found")


class MessageFeedbackServiceTest(unittest.TestCase):
    max_note_bytes = 4

    def setUp(self):
        self.ctx = Context(name="feedback-msg")
        self.store = install_sessions(self.ctx)
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.persistence = JsonlPersistence(os.path.join(self.tmp.name, "sessions"))
        self.ctx.provide("sessionPersistence", self.persistence)
        # 把持久化挂到 session/event + session/flush（对齐 ACP _install_persistence_hook
        # 的装配面）：活会话 append 经事件流进持久化、flush 时落盘。
        def on_event(payload):
            event = payload.get("event") if isinstance(payload, dict) else payload
            if event is not None and getattr(event, "get", None) and event.get("type"):
                self.persistence.append("m1", event)
        def on_flush(payload=None):
            self.persistence.flush()
        self.ctx.on("session/event", on_event)
        self.ctx.on("session/flush", on_flush)
        install_message_feedback(self.ctx, {"maxNoteBytes": self.max_note_bytes})
        self.service = self.ctx.get("messageFeedback")
        self.session = self.store.create("m1", {"meta": {"cwd": os.getcwd()}})

    def tearDown(self):
        self.ctx.dispose()

    def _put(self, message_id, rating="positive", note=None, category=None,
             if_version=None):
        item = {"messageId": message_id, "rating": rating}
        if note is not None:
            item["note"] = note
        if category is not None:
            item["category"] = category
        return self.service.put({"sessionId": "m1", "item": item,
                                 "ifVersion": if_version})

    def _put_target(self, text="assistant answer", **kwargs):
        """落一条真实 assistant/message 并用其 id 作 put 目标。"""
        message_id = _assistant_message(self.session, text)
        return message_id, self._put(message_id, **kwargs)

    def test_put_creates_item(self):
        message_id, created = self._put_target(note="good")
        self.assertEqual(created["messageId"], message_id)
        self.assertEqual(created["rating"], "positive")
        self.assertEqual(created["note"], "good")
        self.assertRegex(created["version"], r"^[0-9a-f-]{36}$")
        self.assertIsInstance(created["createdAt"], int)
        self.assertIsInstance(created["updatedAt"], int)
        stored = next(e for e in self.session.events
                     if e["type"] == "feedback/message-put")
        self.assertEqual(stored["data"]["sessionId"], "m1")
        self.assertEqual(stored["data"]["item"]["messageId"], message_id)

    def test_put_note_blank_rejected_before_persistence(self):
        message_id = _assistant_message(self.session, "hello")
        with self.assertRaises(Exception) as raised:
            self._put(message_id, note="   \n\t ")
        self.assertEqual(getattr(raised.exception, "code", None), "note-blank")
        self.assertFalse(any(e["type"] == "feedback/message-put"
                             for e in self.session.events))

    def test_put_note_too_large(self):
        message_id = _assistant_message(self.session, "hello")
        with self.assertRaises(Exception) as raised:
            self._put(message_id, note="ééé")  # 6 字节 > 4
        self.assertEqual(getattr(raised.exception, "code", None), "note-too-large")
        self.assertEqual(getattr(raised.exception, "details", {}).get("maxBytes"), 4)
        self.assertEqual(getattr(raised.exception, "details", {}).get("actualBytes"), 6)

    def test_put_accepts_exact_limit(self):
        message_id = _assistant_message(self.session, "hello")
        created = self._put(message_id, note="😀")  # 4 字节 = limit
        self.assertEqual(created["note"], "😀")

    def test_target_must_be_assistant_message(self):
        # 用户消息不是目标
        self.session.append("user/message",
                            create_message("user", [text_block("hi")], {"kind": "user"}),
                            surfaceOp="append")
        with self.assertRaises(Exception) as raised:
            self._put("missing")
        self.assertEqual(getattr(raised.exception, "code", None), "target-not-found")

    def test_version_conflict_on_absent(self):
        message_id = _assistant_message(self.session, "hello")
        with self.assertRaises(Exception) as raised:
            self._put(message_id, if_version="some-token")
        self.assertEqual(getattr(raised.exception, "code", None), "version-conflict")
        self.assertEqual(getattr(raised.exception, "details", {}).get("current"), None)

    def test_version_conflict_on_absent_null_token(self):
        # ifVersion: null = create-only：无既有条目 → 冲突 current null
        message_id = _assistant_message(self.session, "hello")
        with self.assertRaises(Exception) as raised:
            self._put(message_id, if_version="")
        self.assertEqual(getattr(raised.exception, "code", None), "version-conflict")

    def test_version_conflict_on_stale(self):
        message_id = _assistant_message(self.session, "hello")
        created = self._put(message_id, note="v1")
        with self.assertRaises(Exception) as raised:
            self._put(message_id, note="v2", if_version="stale-token")
        self.assertEqual(getattr(raised.exception, "code", None), "version-conflict")
        self.assertEqual(getattr(raised.exception, "details", {}).get("current")
                         ["version"], created["version"])

    def test_put_update_mints_new_version_and_keeps_created_at(self):
        message_id, created = self._put_target(note="v1")
        updated = self._put(message_id, note="v2", if_version=created["version"])
        self.assertNotEqual(updated["version"], created["version"])
        self.assertEqual(updated["createdAt"], created["createdAt"])
        self.assertGreaterEqual(updated["updatedAt"], created["updatedAt"])

    def test_put_noop_returns_same_item(self):
        message_id, created = self._put_target(note="same")
        again = self._put(message_id, note="same", if_version=created["version"])
        self.assertEqual(again, created)
        puts = [e for e in self.session.events if e["type"] == "feedback/message-put"]
        self.assertEqual(len(puts), 1)

    def test_put_omits_category_when_absent(self):
        message_id, created = self._put_target(note="nc")
        self.assertNotIn("category", created)
        stored = next(e for e in self.session.events
                     if e["type"] == "feedback/message-put")
        self.assertNotIn("category", stored["data"]["item"])

    def test_put_category_change_drops_note(self):
        message_id, created = self._put_target(note="n", category="other")
        updated = self._put(message_id, category="task-result",
                            if_version=created["version"])
        self.assertNotIn("note", updated)
        self.assertEqual(updated["category"], "task-result")

    def test_list_returns_current_items(self):
        a_id, a = self._put_target(text="first", note="a")
        b_id, b = self._put_target(text="second", note="b", category="other")
        result = self.service.list({"sessionId": "m1"})
        self.assertEqual([i["messageId"] for i in result["items"]], [a_id, b_id])
        self.assertEqual(result["items"][0], a)

    def test_delete_absent_succeeds(self):
        result = self.service.delete({"sessionId": "m1", "messageId": "ghost"})
        self.assertEqual(result, {"absent": True})
        self.assertFalse(any(e["type"] == "feedback/message-delete"
                             for e in self.session.events))

    def test_delete_requires_version(self):
        message_id, created = self._put_target()
        with self.assertRaises(Exception) as raised:
            self.service.delete({"sessionId": "m1", "messageId": message_id,
                                 "ifVersion": "stale"})
        self.assertEqual(getattr(raised.exception, "code", None), "version-conflict")
        with self.assertRaises(Exception) as raised:
            self.service.delete({"sessionId": "m1", "messageId": message_id})
        self.assertEqual(getattr(raised.exception, "code", None), "version-conflict")
        # 正确版本删除成功
        result = self.service.delete({"sessionId": "m1", "messageId": message_id,
                                      "ifVersion": created["version"]})
        self.assertEqual(result, {"absent": True})
        delete = next(e for e in self.session.events
                      if e["type"] == "feedback/message-delete")
        self.assertEqual(delete["data"], {"sessionId": "m1", "messageId": message_id})
        # 删除后 list 为空
        self.assertEqual(self.service.list({"sessionId": "m1"})["items"], [])

    def test_cold_session_list(self):
        # 冷会话（不在 live store）：持久化路径不存在 → session-not-found
        _assistant_message(self.session, "hello")
        with self.assertRaises(Exception) as raised:
            self.service.list({"sessionId": "cold-session"})
        self.assertEqual(getattr(raised.exception, "code", None), "session-not-found")

    def test_invalid_config(self):
        from miniharness.feedback.message_feedback import MessageFeedbackService
        with self.assertRaises(TypeError):
            MessageFeedbackService(self.ctx, {"maxNoteBytes": 0})

    def test_missing_session_put(self):
        with self.assertRaises(Exception) as raised:
            self.service.put({"sessionId": "not-persisted", "item": {
                "messageId": "x", "rating": "positive"}})
        self.assertEqual(getattr(raised.exception, "code", None), "session-not-found")


class FeedbackWebWireTest(unittest.TestCase):
    """feedback Remote 经 WebApi dispatch 的 wire 面（对齐 web-controller 模式）。"""

    def setUp(self):
        from miniharness.llm import FakeLlmAdapter
        from miniharness.web.api import WebApi

        self.ctx = Context(name="feedback-web")
        self.store = install_sessions(self.ctx)
        install_command_feedback(self.ctx)
        install_message_feedback(self.ctx, {"maxNoteBytes": 32})
        self.api = WebApi(self.ctx, FakeLlmAdapter())

    def tearDown(self):
        self.ctx.dispose()

    def _create(self, session_id="w1"):
        response = self.api.dispatch("session.create", "r0",
                                     {"sessionId": session_id, "cwd": os.getcwd()})
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]["sessionId"]

    def test_session_feedback_route(self):
        self._create()
        response = self.api.dispatch("sessionFeedback.record", "r1", {
            "sessionId": "w1", "text": "  good session  ",
            "category": "task-result"})
        self.assertTrue(response["result"]["ok"])
        self.assertEqual(response["result"]["value"], {"recorded": True})
        session = self.store.get("w1")
        event = next(e for e in session.events if e["type"] == "feedback/record")
        self.assertEqual(event["data"], {"text": "good session", "category": "task-result"})

    def test_session_feedback_missing_session(self):
        response = self.api.dispatch("sessionFeedback.record", "r1", {
            "sessionId": "nope", "text": "x"})
        self.assertFalse(response["result"]["ok"])
        self.assertEqual(response["result"]["error"]["code"], "session-not-found")
        self.assertEqual(response["result"]["error"]["details"], {"sessionId": "nope"})

    def test_message_feedback_put_route(self):
        sid = self._create()
        session = self.store.get(sid)
        message_id = _assistant_message(session, "answer")
        response = self.api.dispatch("messageFeedback.put", "r1", {
            "sessionId": sid,
            "item": {"messageId": message_id, "rating": "positive", "note": "nice"},
        })
        self.assertTrue(response["result"]["ok"])
        self.assertEqual(response["result"]["value"]["messageId"], message_id)
        list_response = self.api.dispatch("messageFeedback.list", "r2", {"sessionId": sid})
        self.assertEqual([i["messageId"] for i in list_response["result"]["value"]["items"]],
                         [message_id])

    def test_message_feedback_not_mounted(self):
        # 未装 messageFeedback 服务的独立 api（新 ctx）→ gateway/invocation-unavailable
        from miniharness.core.scope import Context as Ctx
        from miniharness.llm import FakeLlmAdapter
        from miniharness.web.api import WebApi
        ctx2 = Ctx(name="bare-feedback")
        try:
            api = WebApi(ctx2, FakeLlmAdapter())
            response = api.dispatch("messageFeedback.list", "r3", {"sessionId": "x"})
            self.assertFalse(response["result"]["ok"])
            self.assertEqual(response["result"]["error"]["code"],
                             "gateway/invocation-unavailable")
        finally:
            ctx2.dispose()


if __name__ == "__main__":
    unittest.main()