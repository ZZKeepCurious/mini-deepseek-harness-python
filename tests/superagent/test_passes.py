import unittest

from superagent.app.runtime import Runtime


class PassesTests(unittest.TestCase):
    def test_closing_rejects_receive(self):
        rt = Runtime()
        rt.closing_flag = True
        with self.assertRaises(RuntimeError):
            rt.passes.receive({"id": "1", "source": "chat", "threads": [], "human": True, "at": "", "text": "", "meta": {}})

    def test_receive_stored_triggers_pump(self):
        rt = Runtime()
        trig = {
            "id": "1",
            "source": "chat",
            "threads": [{"source": "chat", "id": "t1"}],
            "actor": {"source": "chat", "id": "alice"},
            "human": True,
            "at": "2026-09-23T00:00:01Z",
            "text": "x",
            "meta": {},
        }
        self.assertEqual(rt.passes.receive(trig), "stored")
        self.assertTrue(len(rt.assessor.requests) >= 1)
