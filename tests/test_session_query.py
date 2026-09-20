"""session-query 验收（对齐 packages/session-query）。"""
import os
import unittest

from miniharness.core.scope import Context
from miniharness.core.session_store import SessionStore
from miniharness.core.tools import ToolExec, ToolRegistry
from miniharness.session_query import (
    SessionQuery,
    SessionQueryError,
    build_search_documents,
    extract_event_text,
    install_session_query_tools,
)

_CWD = os.path.abspath(os.sep)
_CWD_B = os.path.join(_CWD, "other")


class TestExtraction(unittest.TestCase):
    def test_first_party_text(self):
        self.assertEqual(extract_event_text({
            "type": "user/message", "seq": 0, "time": 1,
            "data": {"content": [{"type": "text", "text": " hello "}]}}), "hello")
        self.assertEqual(extract_event_text({
            "type": "tool/call", "seq": 0, "time": 1,
            "data": {"name": "bash", "arguments": "echo hi"}}), "bash\necho hi")
        self.assertEqual(extract_event_text({
            "type": "turn/end", "seq": 0, "time": 1,
            "data": {"reason": {"kind": "error", "error": {"message": "boom"}}}}),
            "error\nboom")
        self.assertEqual(extract_event_text({"type": "step/start", "seq": 0, "time": 1,
                                             "data": {}}), "")
        self.assertEqual(extract_event_text({
            "type": "assistant/message", "seq": 0, "time": 1,
            "data": {"message": {"content": [{"type": "reasoning", "text": "hidden"}]}}}), "")


class TestService(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        ToolRegistry(self.ctx)
        self.store = SessionStore(self.ctx)
        self.query = SessionQuery(self.ctx)
        self.s1 = self.store.create("s1", {"meta": {"cwd": _CWD}})
        self.s1.append("tool/call", {"callId": "c1", "name": "bash", "arguments": "echo alpha"})
        self.s1.append("user/message", {"content": [{"type": "text", "text": "alpha beta"}]},
                       surfaceOp="append")
        self.s2 = self.store.create("s2", {"meta": {"cwd": _CWD, "parentSession": "s1"}})
        self.s2.append("user/message", {"content": [{"type": "text", "text": "gamma alpha"}]},
                       surfaceOp="append")

    def tearDown(self):
        self.ctx.dispose()

    def test_search_cross_session(self):
        page = self.query.search({"query": "alpha"})
        ids = [hit["header"]["id"] for hit in page["items"]]
        self.assertEqual(sorted(ids), ["s1", "s2"])
        self.assertTrue(all(hit["bestMatch"]["snippet"] for hit in page["items"]))

    def test_search_session_filter(self):
        page = self.query.search({"query": "alpha",
                                  "sessionFilters": [{"kind": "cwd", "values": [_CWD_B]}]})
        self.assertEqual(page["items"], [])
        page2 = self.query.search({"query": "alpha", "limit": 1})
        self.assertEqual(len(page2["items"]), 1)
        self.assertIsNotNone(page2["nextCursor"])

    def test_search_events_filters(self):
        page = self.query.search_events({"sessionId": "s1", "query": "alpha",
                                         "filters": [{"kind": "type", "values": ["user/message"]}]})
        self.assertEqual([hit["type"] for hit in page["items"]], ["user/message"])

    def test_read_event_window_and_bounds(self):
        value = self.query.read_event({"sessionId": "s1", "seq": 0, "after": 1})
        self.assertEqual(value["startSeq"], 0)
        self.assertEqual(value["endSeq"], 1)
        with self.assertRaises(SessionQueryError) as not_found:
            self.query.read_event({"sessionId": "s1", "seq": 99})
        self.assertEqual(not_found.exception.code, "SESSION_QUERY_EVENT_NOT_FOUND")
        with self.assertRaises(SessionQueryError) as window:
            self.query.read_event({"sessionId": "s1", "seq": 0, "after": 999})
        self.assertEqual(window.exception.code, "SESSION_QUERY_INVALID_WINDOW")

    def test_trace_event(self):
        trace = self.query.trace_event({"sessionId": "s1", "seq": 0})
        self.assertEqual(trace["target"]["type"], "tool/call")

    def test_lineage(self):
        lineage = self.query.lineage({"sessionId": "s2"})
        self.assertEqual([a["header"]["id"] for a in lineage["ancestors"]], ["s1"])
        self.assertTrue(lineage["complete"])
        parent = self.query.lineage({"sessionId": "s1"})
        self.assertEqual([d["session"]["header"]["id"] for d in parent["descendants"]], ["s2"])

    def test_search_rejects_bad_query_and_unknown_session(self):
        with self.assertRaises(SessionQueryError):
            self.query.search({"query": ""})
        with self.assertRaises(SessionQueryError) as missing:
            self.query.search_events({"sessionId": "nope", "query": "x"})
        self.assertEqual(missing.exception.code, "SESSION_QUERY_SESSION_NOT_FOUND")


class TestTools(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        ToolRegistry(self.ctx)
        self.store = SessionStore(self.ctx)
        SessionQuery(self.ctx)
        self.tools = install_session_query_tools(self.ctx)
        session = self.store.create("s1", {"meta": {"cwd": _CWD}})
        session.append("user/message", {"content": [{"type": "text", "text": "needle in haystack"}]},
                       surfaceOp="append")
        self.exec = ToolExec(agent=None)

    def tearDown(self):
        self.ctx.dispose()

    def test_session_search_tool(self):
        value = self.tools["session_search"].execute({"query": "needle"}, self.exec)
        self.assertEqual(value["items"][0]["header"]["id"], "s1")
        text = self.tools["session_search"].render({"query": "needle"}, value)[0]["text"]
        self.assertIn("s1", text)

    def test_event_read_tool(self):
        value = self.tools["session_event_read"].execute(
            {"session_id": "s1", "seq": 0}, self.exec)
        self.assertEqual(value["target"]["seq"], 0)


class TestDocuments(unittest.TestCase):
    def test_structural_events_omitted(self):
        events = [
            {"type": "turn/start", "seq": 0, "time": 1, "data": {}},
            {"type": "user/message", "seq": 1, "time": 2,
             "data": {"content": [{"type": "text", "text": "hi"}]}},
        ]
        docs = build_search_documents("s", events)
        self.assertEqual([doc["seq"] for doc in docs], [1])


if __name__ == "__main__":
    unittest.main()
