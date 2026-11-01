from __future__ import annotations

from superagent.core.ids import key
from superagent.core.model import iso_time, live_ticket, thread_key
from superagent.core.part import EnginePart, EngineOptions

ReceiveResult = str  # stored | duplicate | filtered


class Intake(EnginePart):
    def __init__(self, options: EngineOptions, tickets) -> None:
        super().__init__(options)
        self.tickets = tickets

    def receive(self, received: dict) -> ReceiveResult:
        trigger = {**received, "at": iso_time(received["at"])}
        if not trigger.get("threads"):
            raise RuntimeError("A trigger must belong to at least one thread")
        if self.repo.triggers.get(trigger["id"]):
            return "duplicate"
        at = self.now()
        filter_result = self.options.scope.scope(trigger)
        if not filter_result.get("listened"):
            self.repo.triggers.save(trigger)
            self.repo.triggers.saveDisposition(
                trigger["id"],
                {"filtered": {"code": filter_result["reason"]}, "fanout": [], "at": at},
            )
            return "filtered"
        self.repo.triggers.save(trigger)
        keys = [thread_key(ref) for ref in trigger["threads"]]
        reached: dict[str, dict] = {}
        for ref in trigger["threads"]:
            thread = self.thread(ref)
            if not thread.get("lastAt") or trigger["at"] > thread["lastAt"]:
                thread["lastAt"] = trigger["at"]
            correlated = set(thread.get("correlated", []))
            correlated.update(k for k in keys if k != thread["key"])
            thread["correlated"] = list(correlated)
            for ticket_id in thread.get("subscriptions", []):
                ticket = self.repo.tickets.get(ticket_id)
                followed = ticket_id in (thread.get("context") or [])
                resumes = trigger.get("human") and (ticket or {}).get("ended", {}).get("by") != "human" and not followed
                if not ticket or (not live_ticket(ticket) and not resumes):
                    continue
                prev = reached.get(ticket_id)
                context_only = followed and (prev.get("context", True) if prev else True)
                reached[ticket_id] = {"ticket": ticket, "context": context_only}
            self.repo.threads.save(thread)
        for ticket_id, info in reached.items():
            ticket = info["ticket"]
            followed = info["context"]
            inp = {
                "id": key("input", ticket_id, "trigger", trigger["id"]),
                "kind": "trigger",
                "triggerId": trigger["id"],
                "at": at,
            }
            if trigger.get("human"):
                inp["human"] = True
            if followed:
                inp["context"] = True
            self.tickets.add_input(ticket, inp)
        fanout = list(reached.keys())
        context = [tid for tid in fanout if reached[tid]["context"]]
        disposition: dict = {"fanout": fanout, "at": at}
        if context:
            disposition["context"] = context
        self.repo.triggers.saveDisposition(trigger["id"], disposition)
        if not self.owners(trigger):
            self.queue_for_eval(trigger)
        for ticket_id in fanout:
            self.tickets.wake(ticket_id)
        self.emit("trigger.received", {"triggerId": trigger["id"], "threads": [thread_key(ref) for ref in trigger["threads"]]})
        return "stored"
