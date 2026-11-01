import json
import os
import tempfile
import unittest

from superagent.app.store import Store


class StoreTests(unittest.TestCase):
    def test_atomic_rollback_restores_memory(self):
        path = os.path.join(tempfile.mkdtemp(), "t.sqlite")
        store = Store(path)
        store.atomic(lambda: store.triggers.save({"id": "1", "source": "chat", "threads": [], "text": "x"}))
        self.assertEqual(store.triggers.get("1")["id"], "1")
        def fail():
            store.triggers.save({"id": "2", "source": "chat", "threads": [], "text": "y"})
            raise RuntimeError("fail")

        with self.assertRaises(RuntimeError):
            store.atomic(fail)
        self.assertIsNone(store.triggers.get("2"))
        store.close()

    def test_reload_from_sqlite(self):
        path = os.path.join(tempfile.mkdtemp(), "t.sqlite")
        store = Store(path)
        store.atomic(lambda: store.triggers.save({"id": "1", "source": "chat", "threads": [], "text": "x"}))
        store.close()
        store2 = Store(path)
        self.assertEqual(store2.triggers.get("1")["text"], "x")
        store2.close()

    def test_receipts_insert_or_ignore(self):
        path = os.path.join(tempfile.mkdtemp(), "t.sqlite")
        store = Store(path)
        msg = {"id": "m1", "text": "hi", "kind": "acknowledgement"}
        self.assertTrue(store.receipts.add(msg))
        self.assertFalse(store.receipts.add(msg))
        store.close()
