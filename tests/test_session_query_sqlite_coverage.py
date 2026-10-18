"""session_query.sqlite 补充覆盖：FTS5 文档索引、词项安全匹配、过滤/分页/多会话语义。

对齐 packages/session-query/session-query-sqlite。真 sqlite3 内存库 + 真实
文档 dict，断言命中摘要、bm25 排序与过滤子句。
"""
import sqlite3
import unittest

from miniharness.session_query.config import SessionQueryError
from miniharness.session_query.sqlite import SqliteSearchIndex, match_query


def _doc(seq, type_, time, surface, text):
    return {"seq": seq, "type": type_, "time": time, "surface": surface, "text": text}


class MatchQueryTest(unittest.TestCase):
    def test_empty_and_blank(self):
        self.assertEqual(match_query(""), "")
        self.assertEqual(match_query(None), "")
        self.assertEqual(match_query("   \t "), "")

    def test_terms_quoted_and_anded(self):
        self.assertEqual(match_query("alpha beta"), '"alpha" AND "beta"')

    def test_punctuation_is_data_not_syntax(self):
        self.assertEqual(match_query('alpha" OR "beta'), '"alpha" AND "OR" AND "beta"')


class SqliteSearchIndexTest(unittest.TestCase):
    def setUp(self):
        self.index = SqliteSearchIndex()
        self.addCleanup(self.index.close)
        self.index.index_session("s1", [
            _doc(0, "user/message", 10, "append", "alpha beta content"),
            _doc(1, "tool/call", 20, "append", "bash echo alpha"),
            _doc(2, "assistant/message", 30, "append", "gamma only"),
        ])
        self.index.index_session("s2", [
            _doc(0, "user/message", 15, "append", "alpha cross session"),
        ])

    def test_empty_query_returns_empty(self):
        self.assertEqual(self.index.search(""), [])
        self.assertEqual(self.index.search_session("s1", ""), [])

    def test_search_all_sessions_sorted_by_rank(self):
        hits = self.index.search("alpha", limit=10)
        self.assertEqual({hit["sessionId"] for hit in hits}, {"s1", "s2"})
        ranks = [hit["rank"] for hit in hits]
        self.assertEqual(ranks, sorted(ranks))
        self.assertTrue(all(hit["snippet"] for hit in hits))

    def test_search_session_scopes(self):
        hits = self.index.search_session("s1", "alpha", limit=10)
        self.assertEqual({hit["sessionId"] for hit in hits}, {"s1"})

    def test_type_filter(self):
        hits = self.index.search_session(
            "s1", "alpha", filters=[{"kind": "type", "values": ["tool/call"]}])
        self.assertEqual([hit["type"] for hit in hits], ["tool/call"])

    def test_surface_filter(self):
        hits = self.index.search_session(
            "s1", "alpha", filters=[{"kind": "surface", "values": ["append"]}])
        self.assertEqual(len(hits), 2)
        hits_none = self.index.search_session(
            "s1", "alpha", filters=[{"kind": "surface", "values": ["other"]}])
        self.assertEqual(hits_none, [])

    def test_seq_range_filter(self):
        hits = self.index.search_session(
            "s1", "alpha", filters=[{"kind": "seq", "from": 1, "to": 1}])
        self.assertEqual([hit["seq"] for hit in hits], [1])

    def test_time_range_filter(self):
        hits = self.index.search_session(
            "s1", "alpha", filters=[{"kind": "time", "from": 15, "to": 25}])
        self.assertEqual([hit["time"] for hit in hits], [20])

    def test_text_substring_filter(self):
        hits = self.index.search_session(
            "s1", "alpha", filters=[{"kind": "text", "text": "echo"}])
        self.assertEqual([hit["seq"] for hit in hits], [1])

    def test_limit_and_offset(self):
        all_hits = self.index.search_session("s1", "alpha", limit=10)
        self.assertEqual(len(all_hits), 2)
        first = self.index.search_session("s1", "alpha", limit=1, offset=0)
        second = self.index.search_session("s1", "alpha", limit=1, offset=1)
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertNotEqual(first[0]["seq"], second[0]["seq"])

    def test_search_with_session_ids_sort_and_cap(self):
        hits = self.index.search("alpha", session_ids=["s1", "s2"], limit=1)
        self.assertEqual(len(hits), 1)

    def test_remove_session_drops_documents(self):
        self.index.remove_session("s1")
        self.assertEqual(self.index.search_session("s1", "alpha"), [])
        self.assertEqual(len(self.index.search("alpha")), 1)

    def test_reindex_replaces_previous_documents(self):
        self.index.index_session("s1", [_doc(0, "user/message", 40, "append", "delta")])
        self.assertEqual(self.index.search_session("s1", "alpha"), [])
        self.assertEqual([hit["seq"] for hit in self.index.search_session("s1", "delta")],
                         [0])

    def test_index_failure_classified(self):
        with self.assertRaises(SessionQueryError) as raised:
            self.index.index_session("bad", [
                _doc(0, "user/message", 1, "append", {"not": "a-scalar"}),
            ])
        self.assertEqual(raised.exception.code, "SESSION_QUERY_INDEX_FAILED")

    def test_close_releases_connection(self):
        index = SqliteSearchIndex()
        index.close()
        with self.assertRaises(sqlite3.ProgrammingError):
            index.remove_session("s")


if __name__ == "__main__":
    unittest.main()
