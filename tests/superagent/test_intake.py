import unittest

from tests.superagent.support.harness import Harness
from tests.superagent.support.triggers import trigger


class IntakeTests(unittest.TestCase):
    def test_stores_a_trigger_once_and_records_source_filtering(self):
        h = Harness()
        self.assertEqual(h.engine.receive(trigger("1")), "stored")
        self.assertEqual(h.engine.receive(trigger("1")), "duplicate")
        self.assertEqual(h.engine.receive(trigger("2", meta={"blocked": True})), "filtered")
        self.assertEqual(h.repo.triggers.disposition("2")["filtered"], {"code": "chat not allowed"})
        self.assertEqual(h.repo.threads.get("chat:t1")["pending"], ["1"])

    def test_records_correlation_for_a_trigger_on_several_threads(self):
        h = Harness()
        h.engine.receive(
            trigger(
                "1",
                threads=[{"source": "gh", "id": "pr-7"}, {"source": "gh", "id": "run-9"}],
            )
        )
        self.assertEqual(h.repo.threads.get("gh:pr-7")["pending"], ["1"])
        self.assertEqual(h.repo.threads.get("gh:pr-7")["correlated"], ["gh:run-9"])
        self.assertEqual(h.repo.threads.get("gh:run-9")["pending"], [])
        self.assertEqual(h.repo.threads.get("gh:run-9")["correlated"], ["gh:pr-7"])
