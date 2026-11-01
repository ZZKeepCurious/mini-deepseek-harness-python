import unittest

from superagent.core.stage import can_retry, ticket_stage

ATTENTION = {"id": "q", "question": "?"}
ANSWERED = {"inputs": [{"id": "i", "kind": "trigger", "triggerId": "e", "at": "later", "human": True}]}
ENDED = {"at": "", "by": "executor", "reason": "closed", "outcome": "closed"}


def assignment(**overrides):
    base = {"id": "a", "state": "running", "createdAt": ""}
    base.update(overrides)
    return base


def reported(report_state, **overrides):
    rep = {"state": report_state, "summary": "", "at": ""}
    return assignment(report=rep, **overrides)


def reported_at(state):
    return assignment(report={"state": state, "summary": "", "at": "earlier"})


def ticket(**overrides):
    base = {
        "id": "t",
        "title": "x",
        "decisionId": "d",
        "mandate": {
            "strength": "explicit",
            "policyVersion": "v",
            "basis": {"via": "listed"},
            "actor": {"source": "feishu", "id": "ou"},
            "scope": "",
        },
        "channels": [],
        "assignments": [assignment()],
        "inputs": [],
        "undelivered": [],
        "createdAt": "",
        "updatedAt": "",
        "origin": {"triggerId": "x", "triggerIds": ["x"]},
    }
    base.update(overrides)
    return base


class TicketStageTests(unittest.TestCase):
    CASES = [
        ("an undelivered Input", ticket(delivery={"error": "offline", "at": ""}), {"stage": "person", "action": "delivery"}),
        (
            "delivery before answer and review",
            ticket(delivery={"error": "offline", "at": ""}, attention=ATTENTION, assignments=[reported("ready")]),
            {"stage": "person", "action": "delivery"},
        ),
        ("an Attention", ticket(attention=ATTENTION), {"stage": "person", "action": "answer"}),
        (
            "answer before review",
            ticket(attention=ATTENTION, assignments=[reported("ready")]),
            {"stage": "person", "action": "answer"},
        ),
        (
            "answer before a failure",
            ticket(attention=ATTENTION, assignments=[reported("failed")]),
            {"stage": "person", "action": "answer"},
        ),
        ("a ready Report", ticket(assignments=[reported("ready")]), {"stage": "person", "action": "review"}),
        (
            "a ready Report of an ended Assignment",
            ticket(assignments=[reported("ready", state="ended")]),
            {"stage": "person", "action": "review"},
        ),
        ("a blocked Report", ticket(assignments=[reported("blocked")]), {"stage": "person", "action": "blocked"}),
        ("a failed Report", ticket(assignments=[reported("failed")]), {"stage": "person", "action": "failed"}),
        (
            "a blocked Report a person answered since",
            ticket(**ANSWERED, assignments=[reported_at("blocked")]),
            {"stage": "working"},
        ),
        ("no Assignment", ticket(assignments=[]), {"stage": "working", "action": "queued"}),
        (
            "a pending Assignment",
            ticket(assignments=[reported("failed", state="ended"), assignment(state="pending")]),
            {"stage": "working", "action": "queued"},
        ),
        ("a waiting Report", ticket(assignments=[reported("waiting")]), {"stage": "idle"}),
        ("an ended Assignment", ticket(assignments=[assignment(state="ended")]), {"stage": "idle"}),
        ("a running Assignment", ticket(), {"stage": "working"}),
        ("a progress Report", ticket(assignments=[reported("progress")]), {"stage": "working"}),
        (
            "an ended Ticket",
            ticket(ended=ENDED, attention=ATTENTION, assignments=[reported("failed")]),
            {"stage": "ended"},
        ),
    ]

    def test_ticket_stage_table(self):
        for name, value, stage in self.CASES:
            with self.subTest(name):
                self.assertEqual(ticket_stage(value), stage)


class CanRetryTests(unittest.TestCase):
    CASES = [
        ("a failed open Ticket", True, ticket(assignments=[reported("failed")])),
        ("a blocked open Ticket", True, ticket(assignments=[reported("blocked")])),
        (
            "a failed Ticket that also awaits an answer",
            True,
            ticket(attention=ATTENTION, assignments=[reported("failed")]),
        ),
        (
            "a blocked Ticket whose Input is undelivered",
            True,
            ticket(delivery={"error": "offline", "at": ""}, assignments=[reported("blocked")]),
        ),
        ("a failed Ticket that ended", False, ticket(ended=ENDED, assignments=[reported("failed")])),
        (
            "a Ticket retried already, whose new Assignment is pending",
            False,
            ticket(assignments=[reported("failed", state="ended"), assignment(state="pending")]),
        ),
        ("a ready Ticket", False, ticket(assignments=[reported("ready")])),
        ("a running Ticket", False, ticket()),
        ("a Ticket without an Assignment", False, ticket(assignments=[])),
        (
            "a failed Ticket a person answered since",
            False,
            ticket(**ANSWERED, assignments=[reported_at("failed")]),
        ),
        (
            "a failed Ticket whose delivery of a person's answer failed",
            True,
            ticket(
                **ANSWERED,
                delivery={"error": "offline", "at": ""},
                assignments=[reported_at("failed")],
            ),
        ),
        (
            "a working Ticket whose Input could not be delivered",
            True,
            ticket(delivery={"error": "offline", "at": ""}),
        ),
        (
            "an ended Ticket whose Input could not be delivered",
            False,
            ticket(ended=ENDED, delivery={"error": "offline", "at": ""}),
        ),
    ]

    def test_can_retry_table(self):
        for name, expected, value in self.CASES:
            with self.subTest(name):
                self.assertEqual(can_retry(value), expected)

    def test_retry_in_person_delivery_stage(self):
        delivery = ticket(
            **ANSWERED,
            delivery={"error": "offline", "at": ""},
            assignments=[reported_at("failed")],
        )
        self.assertEqual(ticket_stage(delivery), {"stage": "person", "action": "delivery"})
        self.assertTrue(can_retry(delivery))
