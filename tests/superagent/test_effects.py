import os
import tempfile
import unittest

from superagent.app.effects import Effects, Later
from superagent.app.store import Store


class EffectsTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.path = os.path.join(self.dir, "db.sqlite")
        self.store = Store(self.path)
        self.clock = 1_000_000.0
        self.effects = Effects(self.store, now=lambda: self.clock)
        self.effects.start()
        self.runs: list[tuple] = []

    def tearDown(self):
        self.store.close()

    def test_runs_after_commit_not_after_rollback(self):
        self.effects.handle("relay", lambda payload, key: self.runs.append((payload, key)))

        def ok():
            self.effects.enqueue("relay", "kept", {"text": "a"})

        self.store.atomic(ok)
        with self.assertRaises(RuntimeError):
            self.store.atomic(
                lambda: (
                    self.effects.enqueue("relay", "dropped", {"text": "b"}),
                    (_ for _ in ()).throw(RuntimeError("rolled back")),
                )[-1]
            )
        self.effects.idle("relay")
        self.assertEqual(self.runs, [({"text": "a"}, "kept")])
        self.assertFalse(self.effects.pending("relay"))

    def test_later_reschedules(self):
        self.effects.handle("relay", lambda _p, _k: (_ for _ in ()).throw(Later()))
        self.store.atomic(lambda: self.effects.enqueue("relay", "k", {"text": "x"}))
        self.effects.idle("relay")
        self.assertTrue(self.effects.pending("relay", "k"))
        self.clock += 3
        self.effects.idle("relay")
        self.assertTrue(self.effects.pending("relay", "k"))

    def test_cancel_removes_row(self):
        self.effects.handle("relay", lambda _p, _k: None)
        self.store.atomic(lambda: self.effects.enqueue("relay", "k", {"text": "x"}))
        self.effects.cancel("relay", "k")
        self.store.atomic(lambda: None)
        self.effects.idle("relay")
        self.assertFalse(self.effects.pending("relay", "k"))
