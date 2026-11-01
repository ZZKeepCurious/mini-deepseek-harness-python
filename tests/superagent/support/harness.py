from datetime import datetime, timedelta, timezone

from superagent.core.engine import Engine
from superagent.core.memory import MemoryRepositories
from superagent.core.model import running_assignment
from superagent.core.part import EngineOptions
from superagent.executor.fake import FakeExecutor

from tests.superagent.support.triggers import trigger

_BASE = datetime(2026, 9, 23, 0, 0, 0, tzinfo=timezone.utc)


class FakeAssessor:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.released: list[str] = []

    def request(self, candidate: dict) -> None:
        self.requests.append(candidate)

    def release(self, eval_id: str) -> None:
        self.released.append(eval_id)


class Harness:
    def __init__(self, policy_overrides=None, *, emit=None, prepare_ticket=None, paused=False):
        policy_overrides = dict(policy_overrides or {})
        if policy_overrides.pop("paused", None):
            paused = True
        self.repo = MemoryRepositories()
        self.outbox: list[dict] = []
        self.withdrawn: list[str] = []
        self.assessor = FakeAssessor()
        self.executor = FakeExecutor()
        self.delivered: list[tuple[str, str]] = []
        self._version = "v1"
        self._paused = paused
        self._clock = 0
        self._emit = emit

        def gate(t, thread, tickets):
            if not t.get("human"):
                return {"admissible": False, "reason": "not human"}
            if t["actor"]["id"] == "alice":
                return {"admissible": True, "strength": "explicit", "basis": {"via": "listed"}}
            if thread.get("subscriptions"):
                return {"admissible": True, "strength": "inferred", "basis": {"via": "followup"}}
            return {"admissible": False, "reason": "not a member"}

        class Policy:
            evalTimeoutMs = 60_000

            @property
            def version(self):
                return self_outer._version

            @property
            def paused(self):
                return self_outer._paused

            def gate(self, t, thread, tickets):
                custom = policy_overrides.get("gate")
                return custom(t, thread, tickets) if custom else gate(t, thread, tickets)

            def revalidate(self, mandate):
                fn = policy_overrides.get("revalidate")
                return fn(mandate) if fn else self_outer._version != "revoked"

            def admitsInferred(self, rules):
                fn = policy_overrides.get("admitsInferred")
                return fn(rules) if fn else len(rules) > 0

        self_outer = self
        policy = Policy()
        for key, val in policy_overrides.items():
            if key in ("gate", "revalidate", "admitsInferred"):
                continue
            setattr(policy, key, val)

        class Scope:
            def scope(self, t):
                if t.get("meta", {}).get("blocked"):
                    return {"listened": False, "reason": "chat not allowed"}
                return {"listened": True, "observed": False}

        class QueuedOutbox:
            def send(self, message):
                if not any(item["id"] == message["id"] for item in self_outer.outbox):
                    self_outer.outbox.append(message)

            def status(self, id):
                return "pending" if any(item["id"] == id for item in self_outer.outbox) else None

            def withdraw(self, id):
                self_outer.withdrawn.append(id)

        def now():
            return (_BASE + timedelta(seconds=self_outer._clock)).isoformat().replace("+00:00", "Z")

        opts = EngineOptions(
            repo=self.repo,
            scope=Scope(),
            policy=lambda: policy,
            assessor=self.assessor,
            executor=self.executor,
            outbox=QueuedOutbox(),
            now=now,
            emit=emit,
        )
        if prepare_ticket:
            opts.prepareTicket = prepare_ticket
        self.engine = Engine(opts)

    @property
    def requests(self):
        return self.assessor.requests

    def settle(self, launch=True):
        while True:
            if launch and self.executor.launches:
                ticket, assignment = self.executor.launches.pop(0)
                self.engine.assignment_started(ticket["id"], assignment["id"])
                continue
            if not self.executor.deliveries:
                return
            ticket_id = self.executor.deliveries.pop(0)
            ticket = self.repo.tickets.get(ticket_id)
            assignment = running_assignment(ticket)
            if ticket.get("ended") or not assignment:
                continue
            for input_id in list(ticket.get("undelivered", [])):
                self.delivered.append((ticket_id, input_id))
                self.engine.inputs_delivered(ticket_id, assignment["id"], [input_id])

    def pump(self):
        self.engine.evaluate_due()
        self.settle()

    def read(self, ticket_id: str):
        ticket = self.repo.tickets.get(ticket_id)
        unread = [
            inp["id"]
            for inp in ticket.get("inputs", [])
            if inp.get("kind") == "trigger" and inp.get("readAt") is None
        ]
        if unread:
            self.engine.inputs_read(ticket_id, unread)

    def decide(self, candidate, outcome):
        tid = candidate["triggers"][0]["id"]
        return self.engine.decide(tid, {**outcome, "evalId": candidate["evalId"]})

    def set_version(self, value: str):
        self._version = value

    def set_paused(self, value: bool):
        self._paused = value

    def tick(self, seconds: int):
        self._clock += seconds


def ended(h: Harness, by: str):
    decision = accepted(h)
    ticket_id = decision["ticketId"]
    if by == "executor":
        h.engine.report(ticket_id, {"state": "ready", "summary": "done"})
    else:
        h.engine.stop_ticket(ticket_id)
    return ticket_id


def accepted(h: Harness, id: str = "1", thread: str = "t1"):
    h.engine.receive(trigger(id, threads=[{"source": "chat", "id": thread}]))
    h.pump()
    decision = h.decide(
        h.requests[-1],
        {
            "kind": "accept",
            "title": "Fix",
            "scope": "small",
            "reason": "requested",
            "acknowledgement": "ok",
        },
    )
    h.settle()
    return decision
