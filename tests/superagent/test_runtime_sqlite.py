import os
import tempfile
import unittest

from superagent.app.runtime import Runtime
from superagent.infra.policy import SimplePolicy


class RuntimeSqliteTests(unittest.TestCase):
    def test_accept_persists_ticket(self):
        path = os.path.join(tempfile.mkdtemp(), "rt.sqlite")
        rt = Runtime(db_path=path, policy=SimplePolicy())
        trig = {
            "id": "1",
            "source": "chat",
            "threads": [{"source": "chat", "id": "t1"}],
            "actor": {"source": "chat", "id": "alice"},
            "human": True,
            "at": "2026-09-23T00:00:01Z",
            "text": "fix",
            "channel": {"source": "chat", "thread": "t1", "address": {"message": "1"}},
            "meta": {},
        }
        rt.passes.receive(trig)
        rt.passes.pump()
        c = rt.assessor.requests[-1]
        rt.engine.decide(
            "1",
            {
                "kind": "accept",
                "title": "Fix",
                "scope": "small",
                "reason": "r",
                "acknowledgement": "ok",
                "evalId": c["evalId"],
            },
        )
        rt.effects.idle("launch")
        rt.effects.idle("reply")
        self.assertEqual(len(rt.repo.tickets.list()), 1)
        rt.store.close()
        rt2 = Runtime(db_path=path, policy=SimplePolicy())
        self.assertEqual(len(rt2.repo.tickets.list()), 1)
        rt2.store.close()
