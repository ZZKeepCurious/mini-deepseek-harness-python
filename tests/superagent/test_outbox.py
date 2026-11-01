import os
import tempfile
import unittest

from superagent.app.runtime import Runtime
from superagent.core.ids import key
from superagent.infra.policy import SimplePolicy


class OutboxTests(unittest.TestCase):
    def test_reply_effect_sends_via_source(self):
        path = os.path.join(tempfile.mkdtemp(), "out.sqlite")
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
        candidate = rt.assessor.requests[-1]
        rt.engine.decide(
            "1",
            {
                "kind": "accept",
                "title": "Fix",
                "scope": "small",
                "reason": "r",
                "acknowledgement": "ok",
                "evalId": candidate["evalId"],
            },
        )
        rt.effects.idle("reply")
        self.assertEqual(len(rt.sent_messages), 1)
        self.assertEqual(rt.sent_messages[0]["id"], key("ack", "1"))
        rt.store.close()
