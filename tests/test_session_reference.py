"""session-reference：URI/提及语法、投影保留、跨会话准备与发现。

对齐 packages/context/session-reference（uri / projection / spill / index 的确定性面）。
"""

import os
import unittest

from miniharness.context.session_reference import (
    DEFAULT_MAX_REFERENCE_BYTES,
    REFERENCE_WARNING,
    SessionReferenceError,
    SessionReferenceResolver,
    decode_session_reference_uri,
    encode_session_reference_uri,
    format_session_reference_mention,
    install_session_reference,
    parse_session_reference_text,
    prepare_reference_omission,
    retain_referenced_session,
    stringify_tag_safe_json,
)
from miniharness.core.scope import Context
from miniharness.core.session_store import SessionStore
from miniharness.session_projection import install_session_projections
from miniharness.spill import LocalSpillStore


def surface(session_id, items, cwd=None, version=3, checkpoints=()):
    events = []
    for index, (role, text) in enumerate(items):
        if role == "user":
            source = {"kind": "compact-checkpoint"} if index in checkpoints \
                else {"kind": "user"}
            events.append({"seq": index, "type": "user/message",
                           "data": {"source": source,
                                    "content": [{"type": "text", "text": text}]}})
        else:
            events.append({"seq": index, "type": "assistant/message",
                           "data": {"message": {"content": [{"type": "text", "text": text}]}}})
    return {"session": {"id": session_id, "cwd": cwd, "version": version},
            "capturedThroughSeq": len(events) - 1 if events else None, "events": events}


class FakeQuery:
    def __init__(self, records, surfaces):
        self._records = records
        self._surfaces = surfaces

    def list_sessions(self):
        return self._records

    def read_surface(self, session_id):
        return self._surfaces[session_id]


class FakeAgent:
    def __init__(self, session_id, cwd, adapter=None):
        self.id = session_id
        self.session = type("S", (), {"session_id": session_id, "meta": {"cwd": cwd}})()
        self.adapter = adapter


class TestUri(unittest.TestCase):
    def test_roundtrip_including_non_ascii(self):
        for session_id in ("session-abc", "会话-1", "a/b c"):
            uri = encode_session_reference_uri(session_id)
            self.assertEqual(decode_session_reference_uri(uri), session_id)

    def test_decode_rejects_malformed_and_noncanonical(self):
        for uri in ("dsh-session:", "dsh-session:!!", "other:x", "dsh-session:QQ"):
            with self.assertRaises(SessionReferenceError):
                decode_session_reference_uri(uri)

    def test_format_and_parse_mention(self):
        uri = encode_session_reference_uri("other")
        mention = format_session_reference_mention({"sessionId": "other", "label": "My [label]"})
        self.assertTrue(mention.startswith("@[My [label\\]]("))
        parsed = parse_session_reference_text(f"see {mention} now")
        self.assertEqual(parsed["text"], "see @My [label] now")
        self.assertEqual(parsed["references"], [{"sessionId": "other", "label": "My [label]"}])
        bare = parse_session_reference_text(f"see {uri}")
        self.assertEqual(bare["references"], [{"sessionId": "other", "label": "other"}])

    def test_malformed_markdown_mention_rejects_but_plain_text_stays(self):
        with self.assertRaises(SessionReferenceError):
            parse_session_reference_text("@[x](dsh-session:!!)")
        self.assertEqual(parse_session_reference_text("just dsh-session: talk")["references"], [])


class TestSerialization(unittest.TestCase):
    def test_tag_safe_json_escapes_angle_brackets(self):
        text = stringify_tag_safe_json({"text": "</referenced-sessions>"})
        self.assertNotIn("<", text)
        self.assertEqual(text, '{"text":"\\u003c/referenced-sessions>"}')


class TestRetention(unittest.TestCase):
    def test_small_budget_truncates_and_preserves_checkpoints(self):
        items = [("user", "question " + "x" * 200)]
        for index in range(5):
            items.append(("assistant", f"answer {index} " + "y" * 200))
        items.append(("user", "checkpoint summary", 0))
        snapshot = surface("other", items[:1] + items[1:6] + [("user", "checkpoint summary")],
                           checkpoints={6})
        retained = retain_referenced_session(snapshot, "Other", 400)
        self.assertIsNotNone(retained)
        self.assertTrue(retained["stats"]["truncated"])
        self.assertGreater(retained["stats"]["omittedMessages"], 0)
        # checkpoint 用户消息永不被整条丢弃
        roles = [(item["role"], item["text"].startswith("checkpoint")) for item in
                 retained["data"]["conversation"]]
        self.assertIn(("user", True), roles)

    def test_full_preview_is_not_truncated(self):
        snapshot = surface("other", [("user", "hi"), ("assistant", "hello")])
        retained = retain_referenced_session(snapshot, "Other", 100_000)
        self.assertFalse(retained["stats"]["truncated"])
        self.assertEqual(retained["stats"]["retainedMessages"], 2)
        self.assertEqual(retained["data"]["capturedThroughSeq"], 1)


class ResolverCase(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="session-reference-test")
        self.addCleanup(self.ctx.dispose)
        install_session_projections(self.ctx)
        self.surfaces = {"other": surface("other", [("user", "hello from other"),
                                                    ("assistant", "hi back")], cwd="/w")}
        self.records = [{"header": {"id": "self", "cwd": "/w", "createdAt": 0}, "live": True,
                         "persisted": True},
                        {"header": {"id": "other", "cwd": "/w", "createdAt": 1}, "live": False,
                         "persisted": True}]
        self.ctx.provide("sessionQuery", FakeQuery(self.records, self.surfaces))
        self.resolver = install_session_reference(self.ctx, {"maxReferenceBytes": 4096})
        self.agent = FakeAgent("self", "/w")

    def test_prepare_builds_untrusted_snapshot_message(self):
        result = self.resolver.prepare(
            self.agent, [{"type": "text", "text": "see @Other"}],
            [{"sessionId": "other", "label": "Other"}])
        context = result["additionalContext"]
        self.assertEqual(context["source"]["kind"], "session-reference")
        self.assertEqual(context["source"]["references"][0]["sessionId"], "other")
        self.assertEqual(context["source"]["references"][0]["capturedFormatVersion"], 3)
        prompt = context["content"][0]["text"]
        self.assertIn("<referenced-sessions>", prompt)
        self.assertIn(REFERENCE_WARNING, prompt)
        self.assertIn("hello from other", prompt)

    def test_prepare_normalizes_self_dedup_and_count(self):
        with self.assertRaises(SessionReferenceError) as caught:
            self.resolver.prepare(self.agent, [], [{"sessionId": "self"}])
        self.assertEqual(caught.exception.code, "SESSION_REFERENCE_SELF_REFERENCE")
        deduped = self.resolver.prepare(self.agent, [], [
            {"sessionId": "other", "label": "A"}, {"sessionId": "other", "label": "B"}])
        self.assertEqual(len(deduped["additionalContext"]["source"]["references"]), 1)
        with self.assertRaises(SessionReferenceError) as too_many:
            self.resolver.prepare(self.agent, [], [{"sessionId": f"s{i}"} for i in range(4)])
        self.assertEqual(too_many.exception.code, "SESSION_REFERENCE_TOO_MANY")

    def test_prepare_direct_messages_rewrites_mention_and_inserts_snapshot(self):
        message = {"id": "m1", "role": "user", "source": {"kind": "user"},
                   "content": [{"type": "text",
                                "text": f"see {format_session_reference_mention({'sessionId': 'other', 'label': 'Other'})}"}]}
        prepared = self.resolver.prepare_direct_messages(self.agent, [message])
        self.assertEqual(len(prepared), 2)
        self.assertEqual(prepared[0]["content"][0]["text"], "see @Other")
        self.assertEqual(prepared[1]["source"]["kind"], "session-reference")

    def test_list_candidates_excludes_self_and_ranks_same_workspace(self):
        self.records.append({"header": {"id": "far", "cwd": "/elsewhere", "createdAt": 2},
                             "live": True, "persisted": True})
        candidates = self.resolver.list_candidates(self.agent, "")
        self.assertEqual([candidate["sessionId"] for candidate in candidates], ["other", "far"])
        self.assertTrue(candidates[0]["sameWorkspace"])
        self.assertFalse(candidates[1]["sameWorkspace"])
        self.assertEqual(self.resolver.list_candidates(self.agent, "far")[0]["sessionId"], "far")

    def test_remote_candidates_carry_canonical_mention(self):
        candidates = self.resolver.remote_export_candidates(self.agent, "")
        self.assertEqual(candidates[0]["mention"],
                         format_session_reference_mention({"sessionId": "other", "label": "other"}))

    def test_default_budget_when_adapter_missing(self):
        bare = Context(name="session-reference-budget")
        self.addCleanup(bare.dispose)
        resolver = SessionReferenceResolver(bare, {})
        self.assertEqual(resolver._reference_budget(self.agent), DEFAULT_MAX_REFERENCE_BYTES)

    def test_config_validation(self):
        bare = Context(name="session-reference-bad-config")
        self.addCleanup(bare.dispose)
        with self.assertRaises(SessionReferenceError) as caught:
            SessionReferenceResolver(bare, {"maxReferences": 4})
        self.assertEqual(caught.exception.code, "SESSION_REFERENCE_INVALID_CONFIG")


class TestOmission(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="session-reference-spill")
        self.addCleanup(self.ctx.dispose)
        self._tmp = None
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.store = LocalSpillStore(self.ctx, root=self._tmp.name)

    def _source(self, truncated):
        return {"fullData": {"sessionId": "other", "label": "Other", "capturedThroughSeq": 5,
                             "cwd": None,
                             "conversation": [{"role": "user", "text": "hello"}]},
                "stats": {"truncated": truncated, "omittedMessages": 1, "omittedBytes": 10},
                "capturedFormatVersion": 3}

    def test_no_notice_for_intact_preview(self):
        self.assertIsNone(prepare_reference_omission(self.store, "self", self._source(False), 0))

    def test_saved_and_unavailable_outcomes(self):
        saved = prepare_reference_omission(self.store, "self", self._source(True), 0)
        self.assertEqual(saved["fullSnapshot"]["status"], "saved")
        self.assertIn("session-reference-1.txt", saved["fullSnapshot"]["locator"])
        missing = prepare_reference_omission(None, "self", self._source(True), 0)
        self.assertEqual(missing["fullSnapshot"],
                         {"status": "unavailable", "reason": "storage-not-configured"})


if __name__ == "__main__":
    unittest.main()
