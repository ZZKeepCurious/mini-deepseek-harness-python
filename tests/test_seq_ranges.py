# -*- coding: utf-8 -*-
"""sourceEventSeqs 存储态区间编码焊线验收（CHANGE 1）。

对齐上游：
- packages/core/session/src/seq-ranges.ts（encodeSeqRanges / decodeSeqRanges）
- packages/session/session-persistence-jsonl/src/format.ts
  encodeProvenanceForStorage（写边界）/ expandProvenanceFromStorage（读边界）
- invariant.ts validateEvent 对 sourceEventSeqs 先 decode 再存储

运行：python -m unittest tests.test_seq_ranges
"""
import tempfile
import unittest
from pathlib import Path

from miniharness.core.session.persistence import JsonlPersistence, SqlitePersistence
from miniharness.core.session.seq_ranges import decode_seq_ranges, encode_seq_ranges


def _chunk(i):
    return {
        "type": "assistant/chunk",
        "seq": i,
        "time": 1000 + i,
        "data": {"turn": 1, "step": 1,
                 "chunk": {"type": "text-delta", "index": 0, "text": f"t{i}"}},
    }


def _surface(seq, source_seqs):
    return {
        "type": "user/message",
        "seq": seq,
        "time": 1000 + seq,
        "data": {"role": "user", "content": [{"type": "text", "text": "t"}],
                 "source": "test"},
        "surfaceOp": "append",
        "sourceEventSeqs": source_seqs,
    }


class TestSeqRangesRoundTrip(unittest.TestCase):
    def test_encode_folds_contiguous_runs(self):
        self.assertEqual(encode_seq_ranges([3, 4, 5, 7, 8, 9]), [[3, 5], [7, 9]])

    def test_encode_keeps_non_contiguous_verbatim(self):
        self.assertEqual(encode_seq_ranges([1, 3, 5]), [1, 3, 5])

    def test_decode_expands_and_roundtrips(self):
        seqs = [3, 4, 5, 7, 8, 9]
        self.assertEqual(decode_seq_ranges(encode_seq_ranges(seqs), 10), seqs)


class TestSourceEventSeqsPersistenceWiring(unittest.TestCase):
    def _seed(self, source_seqs):
        return [{"type": "turn/start", "seq": 0, "time": 1, "data": {"turn": 1}},
                _chunk(1), _chunk(2), _chunk(3), _surface(4, source_seqs)]

    def test_jsonsl_write_folds_read_expands(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.declare("s1", {"cwd": str(tmp)}, created_at=1)
            for ev in self._seed([1, 2, 3]):
                p.append("s1", ev)
            p.flush()
            raw = p.read_raw("s1")
            self.assertIn("[[1, 3]]", raw)
            loaded = p.load("s1")
            surface = next(ev for ev in loaded if ev["type"] == "user/message")
            self.assertEqual(surface["sourceEventSeqs"], [1, 2, 3])

    def test_jsonsl_non_contiguous_stored_verbatim(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = JsonlPersistence(Path(tmp))
            p.declare("s1", {"cwd": str(tmp)}, created_at=1)
            for ev in self._seed([1, 3]):
                p.append("s1", ev)
            p.flush()
            raw = p.read_raw("s1")
            self.assertIn("[1, 3]", raw)
            loaded = p.load("s1")
            surface = next(ev for ev in loaded if ev["type"] == "user/message")
            self.assertEqual(surface["sourceEventSeqs"], [1, 3])

    def test_sqlite_write_folds_read_expands(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = SqlitePersistence(Path(tmp))
            try:
                for ev in self._seed([1, 2, 3]):
                    p.append("s1", ev)
                p.flush()
                row = p._conn.execute(
                    "SELECT data FROM events WHERE type='user/message'"
                ).fetchone()
                self.assertIn("[[1, 3]]", row[0])
                loaded = p.load("s1")
                surface = next(ev for ev in loaded if ev["type"] == "user/message")
                self.assertEqual(surface["sourceEventSeqs"], [1, 2, 3])
            finally:
                p.close()


if __name__ == "__main__":
    unittest.main()
