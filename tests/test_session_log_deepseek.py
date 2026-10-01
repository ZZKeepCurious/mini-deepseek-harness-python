"""session-log-deepseek 验收（对齐 packages/session/session-log-deepseek）。

字节前缀截断 + 水位折叠 + wire 翻译。用真实 Session（无 mock）。
"""
import unittest

from miniharness.core.scope import Context
from miniharness.core.session.session import Session
from miniharness.core.session.types import SESSION_FORMAT_VERSION
from miniharness.session_log_deepseek import (
    DEFAULT_MAX_BYTES,
    accepted_through,
    fold_accepted_through,
    is_enabled,
    json_bytes,
    prepare,
    prepare_field,
    resolve_config,
    wire_event,
    wire_header,
)


def _user_message(text, seq, time):
    return {
        "type": "user/message", "seq": seq, "time": time, "surfaceOp": "append",
        "data": {"content": [{"type": "text", "text": text}], "source": {"kind": "user"}},
    }


def _prefix(value, count):
    return {
        **value,
        "throughSeq": value["events"][count - 1]["seq"],
        "events": value["events"][:count],
    }


class _Volatile:
    def __init__(self, value):
        self._value = value

    def get(self):
        return self._value


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        resolved = resolve_config(None)
        self.assertTrue(resolved["enabled"])
        self.assertEqual(resolved["maxBytes"], DEFAULT_MAX_BYTES)

    def test_max_bytes_must_be_positive_integer(self):
        for value in (0, -1, 1.5, True):
            with self.assertRaisesRegex(ValueError, "maxBytes"):
                resolve_config({"maxBytes": value})

    def test_is_enabled_reads_live(self):
        self.assertTrue(is_enabled({"enabled": True}))
        self.assertFalse(is_enabled({"enabled": _Volatile(False)}))
        self.assertFalse(is_enabled({"enabled": lambda: False}))
        self.assertTrue(is_enabled({}))


class TestWire(unittest.TestCase):
    def test_header_translates_meta(self):
        seed = [{"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}}]
        session = Session(
            "child", seed=seed, meta={
                "cwd": "/wire-workspace", "parentSession": "wire-parent", "isSeeded": True,
                "origin": "subagent", "delegationDepth": 1, "agentPreset": "minimal"},
            inherited_event_count=len(seed), mode="restore")
        header = wire_header(session)
        self.assertEqual(header, {
            "version": SESSION_FORMAT_VERSION, "id": "child", "createdAt": session.created_at,
            "cwd": "/wire-workspace", "parentSession": "wire-parent", "seedLength": 1,
            "origin": "subagent", "delegationDepth": 1, "agentPreset": "minimal"})
        self.assertNotIn("isSeeded", header)

    def test_event_surface_and_sources(self):
        event = {
            "type": "user/message", "seq": 1, "time": 2, "ignorable": True,
            "surfaceOp": {"op": "replace", "startSeq": 0, "endSeq": 0},
            "sourceEventSeqs": [0],
            "data": {"content": [{"type": "text", "text": "first"}]},
        }
        wire = wire_event(event)
        self.assertEqual(wire["type"], "user/message")
        self.assertEqual(wire["ignorable"], True)
        self.assertEqual(wire["surfaceOp"], {"op": "replace", "startSeq": 0, "endSeq": 0})
        self.assertEqual(wire["sourceEventSeqs"], [0])

    def test_assistant_message_omits_sources(self):
        wire = wire_event({
            "type": "assistant/message", "seq": 0, "time": 1, "surfaceOp": "append",
            "data": {"message": {}}})
        self.assertEqual(wire, {
            "seq": 0, "time": 1, "data": {"message": {}}, "type": "assistant/message",
            "surfaceOp": "append"})

    def test_log_only_event_has_no_placement(self):
        wire = wire_event({"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}})
        self.assertEqual(wire, {"seq": 0, "time": 1, "data": {"turn": 1}, "type": "turn/start"})


class TestFoldAcceptedThrough(unittest.TestCase):
    def _accept(self, seq, through, session_id="s", fmt=SESSION_FORMAT_VERSION):
        return {"type": "session-log-deepseek/delivery-accepted", "seq": seq, "time": 1,
                "data": {"sessionId": session_id, "throughSeq": through,
                         "sessionFormatVersion": fmt}}

    def test_takes_maximum_and_ignores_other_identity(self):
        events = [self._accept(2, 1), self._accept(3, 0, session_id="other"),
                  self._accept(4, 3), self._accept(5, 9, fmt=SESSION_FORMAT_VERSION - 1)]
        self.assertEqual(fold_accepted_through(events, "s", SESSION_FORMAT_VERSION), 3)

    def test_rejects_malformed(self):
        with self.assertRaisesRegex(RuntimeError, "malformed acceptance watermark"):
            fold_accepted_through([self._accept(2, -1)], "s", SESSION_FORMAT_VERSION)
        with self.assertRaisesRegex(RuntimeError, "malformed acceptance watermark"):
            fold_accepted_through([self._accept(2, 5)], "s", SESSION_FORMAT_VERSION)
        with self.assertRaisesRegex(RuntimeError, "malformed acceptance format version"):
            fold_accepted_through(
                [self._accept(2, 1, fmt=-1)], "s", SESSION_FORMAT_VERSION)

    def test_accepted_through_fresh_session_is_minus_one(self):
        session = Session("fresh", meta={})
        session.append("turn/start", {"turn": 1})
        self.assertEqual(accepted_through(session), -1)


class TestPreparePrefix(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        self.warnings = []
        self.ctx.logger.exporter({
            "levels": {"default": 3},
            "export": lambda message: self.warnings.append(message),
        })

    def tearDown(self):
        self.ctx.dispose()

    def _session(self, id_, events):
        session = Session(id_, meta={})
        for event in events:
            event_type = event["type"]
            data = event["data"]
            session.append(event_type, data, event.get("surfaceOp"))
        return session

    def test_fills_longest_prefix_under_limit(self):
        session = Session("bounded-exact", meta={})
        for index in range(3):
            session.append("turn/start", {"turn": 1, "text": "a" * 200})
        full = prepare(session, DEFAULT_MAX_BYTES)["value"]
        limit = json_bytes(_prefix(full, 2))
        at_limit = prepare(session, limit)["value"]
        self.assertEqual(at_limit, _prefix(full, 2))
        self.assertEqual(json_bytes(at_limit), limit)
        below = prepare(session, limit - 1)["value"]
        self.assertEqual(below, _prefix(full, 1))

    def test_measures_utf8_bytes(self):
        session = Session("bounded-utf8", meta={})
        session.append("turn/start", {"turn": 1, "text": "界" * 100})
        session.append("turn/start", {"turn": 2, "text": "界" * 100})
        full = prepare(session, DEFAULT_MAX_BYTES)["value"]
        limit = json_bytes(_prefix(full, 2)) - 1
        self.assertEqual(prepare(session, limit)["value"], _prefix(full, 1))

    def test_omits_field_when_first_event_exceeds_and_warns(self):
        session = Session("bounded-oversized", meta={})
        session.append("user/message",
                       {"content": [{"type": "text", "text": "x" * 1000}],
                        "source": {"kind": "user"}}, "append")
        full = prepare(session, DEFAULT_MAX_BYTES)["value"]
        limit = json_bytes(_prefix(full, 1)) - 1
        result = prepare(session, limit, logger=self.ctx.logger)
        self.assertIsNone(result)
        warning = (
            f'session-log-deepseek: event 0 of session "bounded-oversized" needs a '
            f'{limit + 1}-byte dsh_session_log field, above maxBytes {limit}; '
            f"this session's upload stays at event 0 until maxBytes admits it")
        args = [message["args"] for message in self.warnings if message["type"] == "warn"]
        self.assertEqual(args, [(warning,)])

    def test_omits_field_without_pending_events(self):
        session = Session("empty", meta={})
        self.assertIsNone(prepare(session, DEFAULT_MAX_BYTES))

    def test_disabled_config_omits_field_live(self):
        session = Session("disabled", meta={})
        session.append("turn/start", {"turn": 1})
        enabled = {"enabled": True, "maxBytes": DEFAULT_MAX_BYTES}
        self.assertIsNotNone(prepare_field(session, enabled))
        enabled["enabled"] = False
        self.assertIsNone(prepare_field(session, enabled))

    def test_accept_appends_delivery_accepted_and_advances_watermark(self):
        # 对齐上游 accept()：append `session-log-deepseek/delivery-accepted`
        # （该类型已入 KNOWN_TYPES，故此处必须成功且推进已接受水位）。
        session = Session("accepts", meta={})
        session.append("turn/start", {"turn": 1})
        session.append("turn/start", {"turn": 2})
        self.assertEqual(accepted_through(session), -1)
        prepared = prepare(session, DEFAULT_MAX_BYTES)
        prepared["accept"]()
        self.assertEqual(
            session.events[-1]["type"], "session-log-deepseek/delivery-accepted")
        self.assertEqual(accepted_through(session), prepared["value"]["throughSeq"])


if __name__ == "__main__":
    unittest.main()
