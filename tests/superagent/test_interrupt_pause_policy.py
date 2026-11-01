import unittest

from superagent.core.model import running_assignment

from tests.superagent.support.harness import Harness, accepted
from tests.superagent.support.triggers import trigger


class InterruptedAssignmentsTests(unittest.TestCase):
    def test_interrupted_assignment_restarts_with_note_hands_waiting(self):
        """Starts an Assignment interrupted mid-work again with a note, and hands one that waits what waits for it."""
        h = Harness()
        interrupted = accepted(h)["ticketId"]
        first = h.repo.tickets.get(interrupted)["assignments"][0]
        waiting = accepted(h, "2", "t2")["ticketId"]
        h.engine.report(waiting, {"state": "waiting", "summary": "done for now"})
        h.engine.receive(trigger("3", threads=[{"source": "chat", "id": "t2"}]))
        h.executor.start_count = 0
        h.executor.deliver_count = 0
        h.executor.deliveries = []
        for tid in (interrupted, waiting):
            h.engine.assignment_interrupted(tid, running_assignment(h.repo.tickets.get(tid))["id"])
        h.engine.assignment_interrupted(interrupted, first["id"])
        ticket = h.repo.tickets.get(interrupted)
        self.assertEqual(ticket["assignments"][0]["state"], "ended")
        self.assertEqual(ticket["assignments"][0]["endedReason"], "hostRestarted")
        self.assertEqual(ticket["assignments"][1]["state"], "pending")
        text_inputs = [i for i in ticket["inputs"] if i.get("kind") == "text"]
        self.assertTrue(any("The host restarted" in i.get("text", "") for i in text_inputs))
        self.assertEqual(ticket["undelivered"], [ticket["inputs"][0]["id"]])
        self.assertEqual(h.executor.start_count, 1)
        self.assertEqual(h.executor.launches[0][0]["id"], interrupted)
        waiting_ticket = h.repo.tickets.get(waiting)
        self.assertEqual(len(waiting_ticket["assignments"]), 1)
        self.assertEqual(h.executor.deliver_count, 1)

    def test_interrupted_after_report_and_person_message(self):
        """Starts again a worker that reported but then got a person's message, which it was working on."""
        h = Harness()
        other = accepted(h, "2", "t2")["ticketId"]
        h.engine.report(other, {"state": "waiting", "summary": "done for now"})
        h.tick(1)
        h.engine.receive(trigger("3", actor={"source": "chat", "id": "guest"}, threads=[{"source": "chat", "id": "t2"}]))
        h.settle()
        running = running_assignment(h.repo.tickets.get(other))
        self.assertGreater(running["deliveredAt"], running["report"]["at"])
        h.engine.assignment_interrupted(other, running["id"])
        states = [a["state"] for a in h.repo.tickets.get(other)["assignments"]]
        self.assertEqual(states, ["ended", "pending"])


class PauseTests(unittest.TestCase):
    def test_pause_extends_eval_deadline_no_expire_while_paused(self):
        """Lets no Eval expire while processing is paused, and moves its deadline on by the pause's length."""
        h = Harness()
        h.engine.receive(trigger("1"))
        h.pump()
        h.engine.candidate_delivered(h.requests[0]["evalId"])
        deadline = h.repo.threads.get("chat:t1")["evaluating"]["deadline"]
        from datetime import datetime

        deadline_ms = int(datetime.fromisoformat(deadline.replace("Z", "+00:00")).timestamp() * 1000)
        h.tick(30)
        h.set_paused(True)
        h.pump()
        h.tick(300)
        h.pump()
        self.assertIsNotNone(h.engine.candidate("1"))
        h.set_paused(False)
        h.pump()
        new_deadline = h.repo.threads.get("chat:t1")["evaluating"]["deadline"]
        new_ms = int(datetime.fromisoformat(new_deadline.replace("Z", "+00:00")).timestamp() * 1000)
        self.assertEqual(new_ms, deadline_ms + 300_000)
        h.tick(29)
        h.pump()
        self.assertIsNotNone(h.engine.candidate("1"))
        h.tick(1)
        h.pump()
        self.assertNotEqual(h.engine.candidate("1")["evalId"], h.requests[0]["evalId"])
        self.assertEqual(h.repo.decisions.byThread("chat:t1"), [])


class PolicyChangeTests(unittest.TestCase):
    def test_policy_change_notifies_tickets_restarts_evals(self):
        """Tells only the live tickets a change bears on how it does, and restarts in-flight evals under the new version."""
        h = Harness()
        decision = accepted(h)
        other = accepted(h, "3", "t3")["ticketId"]
        h.engine.receive(trigger("2", threads=[{"source": "chat", "id": "t2"}]))
        h.pump()
        effect = {
            "rules": [
                {"id": "copy", "name": "Copy", "change": "changed", "instruction": "Fix typos only."}
            ]
        }
        h.set_version("v2")
        h.engine.policy_changed("v1", "v2", {decision["ticketId"]: effect})
        ticket_inputs = h.repo.tickets.get(decision["ticketId"])["inputs"]
        pc = [i for i in ticket_inputs if i.get("kind") == "policy-changed"]
        self.assertEqual(len(pc), 1)
        self.assertEqual(pc[0]["from"], "v1")
        self.assertEqual(pc[0]["to"], "v2")
        self.assertEqual(pc[0]["effect"], effect)
        other_pc = [
            i for i in h.repo.tickets.get(other)["inputs"] if i.get("kind") == "policy-changed"
        ]
        self.assertEqual(other_pc, [])
        t2_decisions = h.repo.decisions.byThread("chat:t2")
        self.assertEqual(t2_decisions[-1]["kind"], "unavailable")
        self.assertEqual(t2_decisions[-1]["reason"], [{"code": "policyChanged"}])
        self.assertEqual(h.repo.threads.get("chat:t2")["pending"], ["2"])
        h.pump()
        self.assertEqual(h.requests[-1]["policyVersion"], "v2")
        self.assertEqual(h.requests[-1]["triggers"][0]["id"], "2")
        told = pc[0]
        self.assertEqual(h.delivered, [(decision["ticketId"], told["id"])])

    def test_does_not_start_evals_while_paused(self):
        """Does not start evals while paused."""
        h = Harness(paused=True)
        h.engine.receive(trigger("1"))
        h.pump()
        self.assertEqual(len(h.assessor.requests), 0)

