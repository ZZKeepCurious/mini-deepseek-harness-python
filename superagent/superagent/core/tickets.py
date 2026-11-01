from __future__ import annotations

import json

from superagent.core.errors import UserError
from superagent.core.ids import key
from superagent.core.model import (
    latest_assignment,
    latest_report,
    live_ticket,
    read_until,
    running_assignment,
    settled,
    thread_key,
    time_ms,
    waits_for_person,
)
from superagent.core.part import EnginePart, EngineOptions
from superagent.core.stage import can_retry

STATUS_REQUEST = (
    "Report the current ticket status with orchestrator_report. This is a status-only request: "
    "do not resume implementation or perform external actions."
)


def automatic(inp: dict | None) -> bool:
    if not inp:
        return False
    return inp.get("kind") == "note" or (inp.get("kind") == "trigger" and not inp.get("human"))


def _same_channel(a: dict, b: dict) -> bool:
    return json.dumps(a, sort_keys=True) == json.dumps(b, sort_keys=True)


def _read_before(inp: dict, until: str) -> bool:
    if "readAt" not in inp:
        delivered = inp.get("deliveredAt")
        baseline = delivered if delivered and delivered > inp.get("at", "") else inp.get("at", "")
        return baseline < until
    read_at = inp.get("readAt")
    return bool(read_at) and read_at < until


class Tickets(EnginePart):
    def __init__(self, options: EngineOptions, replies) -> None:
        super().__init__(options)
        self.replies = replies

    def create(self, id: str, decision_id: str, mandate: dict, evaluation: dict, outcome: dict, at: str) -> None:
        trigger_ids = evaluation["triggerIds"]
        primary_id = evaluation.get("primary") or trigger_ids[-1]
        primary = self.repo.triggers.get(primary_id)
        origin = {"triggerId": primary["id"] if primary else trigger_ids[0], "triggerIds": trigger_ids}
        ticket = {
            "id": id,
            "title": outcome["title"],
            "decisionId": decision_id,
            "mandate": mandate,
            "channels": [],
            "assignments": [],
            "inputs": [],
            "undelivered": [],
            "createdAt": at,
            "updatedAt": at,
            "origin": origin,
        }
        for trigger_id in trigger_ids:
            channel = (self.repo.triggers.get(trigger_id) or {}).get("channel")
            if channel and not any(_same_channel(c, channel) for c in ticket["channels"]):
                ticket["channels"].append(channel)
        if self.options.prepareTicket:
            ticket = self.options.prepareTicket(ticket, outcome)
        assignment = self._new_assignment(ticket)
        self.repo.tickets.save(ticket)
        for trigger_id in trigger_ids:
            for ref in (self.repo.triggers.get(trigger_id) or {}).get("threads", []):
                self._subscribe(id, ref)
        if outcome.get("acknowledgement") and primary:
            self.replies.acknowledge_accepted(id, primary, outcome["acknowledgement"])
        self.options.executor.start(self.require(id), assignment)
        self.emit("ticket.created", {"ticketId": id, "decisionId": decision_id, "strength": mandate["strength"]})

    def bind(self, ticket_id: str, thread: dict, own: bool) -> dict:
        if not live_ticket(self.require(ticket_id)):
            raise UserError("ticketEnded")
        self._subscribe(ticket_id, thread)
        ticket = self.require(ticket_id)
        followed = thread_key(thread)
        follows = list(ticket.get("follows") or [])
        follow = next((f for f in follows if f["thread"] == followed), None)
        if follow:
            follow["own"] = follow.get("own") or own
            follow.pop("closed", None)
        else:
            follows.append({"thread": followed, "own": own})
            ticket["follows"] = follows
        ticket["updatedAt"] = self.now()
        self.repo.tickets.save(ticket)
        return ticket

    def thread_closed(self, thread: dict, outcome: str, at: str) -> None:
        closed = thread_key(thread)
        for ticket in self.repo.tickets.open():
            follow = next((f for f in (ticket.get("follows") or []) if f["thread"] == closed and not f.get("closed")), None)
            if not follow:
                continue
            follow["closed"] = {"at": at, "outcome": outcome}
            self.repo.tickets.save(ticket)
            done = self.follows_done(ticket)
            if not done or self._unfinished(ticket, at):
                continue
            latest = latest_assignment(ticket)
            resumed = time_ms((latest or {}).get("createdAt", "")) > time_ms(at)
            self.end(ticket["id"], "executor", done["outcome"], self.now() if resumed else done["at"])

    def follows_done(self, ticket: dict):
        follows = ticket.get("follows") or []
        own = [f for f in follows if f.get("own")]
        counted = own if own else follows
        if not counted or any(not f.get("closed") for f in counted):
            return None
        closed_list = sorted((f["closed"] for f in counted), key=lambda c: c["at"])
        return closed_list[-1] if closed_list else None

    def attachable(self, ticket_id: str, trigger_id: str) -> dict:
        trigger = self.repo.triggers.get(trigger_id)
        if not trigger:
            raise UserError("unknownTrigger")
        self.left_to_workers(trigger)
        current = self.require(ticket_id)
        if (current.get("ended") or {}).get("by") == "human":
            raise UserError("ticketStopped")
        if not live_ticket(current) and not self.policy().revalidate(current["mandate"]):
            raise UserError("mandateRevoked")
        return trigger

    def attach(self, ticket_id: str, trigger: dict, note: str | None = None) -> None:
        for ref in trigger.get("threads", []):
            self._subscribe(ticket_id, ref)
        current = self.repo.triggers.disposition(trigger["id"]) or {"fanout": [], "at": self.now()}
        context = current.get("context")
        requeued = current.get("requeued")
        disposition = {k: v for k, v in current.items() if k not in ("context", "requeued")}
        followed = [c for c in (context or []) if c != ticket_id]
        fanout = list(set([*disposition.get("fanout", []), ticket_id]))
        saved = {**disposition, "fanout": fanout}
        if followed:
            saved["context"] = followed
        self.repo.triggers.saveDisposition(trigger["id"], saved)
        ticket = self.require(ticket_id)
        inp_id = key("input", ticket_id, "trigger", trigger["id"])
        existing = next((i for i in ticket.get("inputs", []) if i["id"] == inp_id), None)
        at = self.now()

        def make_input(input_id: str) -> dict:
            inp = {"id": input_id, "kind": "trigger", "triggerId": trigger["id"], "at": at}
            if note:
                inp["note"] = note
            if trigger.get("human"):
                inp["human"] = True
            return inp

        if not existing or existing.get("kind") != "trigger":
            self.add_input(ticket, make_input(inp_id))
        elif note and inp_id in ticket.get("undelivered", []):
            existing["note"] = note
            ticket["updatedAt"] = at
            self.repo.tickets.save(ticket)
        elif requeued or ticket_id not in current.get("fanout", []) or ticket_id in (context or []) or not live_ticket(ticket):
            self.add_input(ticket, make_input(key("input", ticket_id, "trigger", trigger["id"], "attached", at)))
        elif note:
            self.add_input(ticket, make_input(key("input", ticket_id, "trigger", trigger["id"], "note", note)))

    def request_status(self, ticket_id: str) -> bool:
        ticket = self.require(ticket_id)
        if not live_ticket(ticket) or (running_assignment(ticket) or {}).get("report", {}).get("state") != "waiting":
            return False
        self.add_input(
            ticket,
            {"id": f"status:{ticket_id}:{ticket['updatedAt']}", "kind": "text", "text": STATUS_REQUEST, "at": self.now()},
        )
        return True

    def stop(self, ticket_id: str, reason: str | None = None) -> dict:
        self.end(ticket_id, "human", "stopped", self.now(), reason)
        return self.require(ticket_id)

    def retry(self, ticket_id: str) -> dict:
        ticket = self.require(ticket_id)
        latest = latest_assignment(ticket)
        if not can_retry(ticket) or not latest:
            raise UserError("notRetryable")
        self._end_assignment(ticket, latest, "retried")
        self._requeue_untaken(ticket, latest)
        assignment = self._new_assignment(ticket)
        ticket["updatedAt"] = self.now()
        self.repo.tickets.save(ticket)
        self.options.executor.stop(ticket, latest, "retried")
        self.options.executor.start(ticket, assignment)
        return ticket

    def mark_unrelated(self, ticket_id: str, thread: str, reason: str) -> None:
        ticket = self.require(ticket_id)
        src = self.source(ticket)
        if src and any(thread_key(ref) == thread for ref in src.get("threads", [])):
            raise UserError("sourceThread")
        if any(f["thread"] == thread for f in (ticket.get("follows") or [])):
            raise UserError("followedThread")
        current = self.repo.threads.get(thread)
        if not current or ticket_id not in current.get("subscriptions", []):
            raise UserError("notFollowing")
        self.drop_subscriptions(thread, "executor", ticket_id, reason)
        for trigger in self._triggers_of(ticket):
            if any(thread_key(ref) == thread for ref in trigger.get("threads", [])) and ticket_id in self.owners(trigger):
                self.hand_back(ticket_id, trigger, reason)

    def policy_changed(self, from_version: str, to_version: str, effects: dict | None = None) -> None:
        effects = effects or {}
        at = self.now()
        for ticket in sorted(self.repo.tickets.open(), key=lambda t: t.get("createdAt", "")):
            effect = effects.get(ticket["id"])
            if effect:
                self.add_input(
                    ticket,
                    {
                        "id": key("input", ticket["id"], "policy", from_version, to_version, at),
                        "kind": "policy-changed",
                        "from": from_version,
                        "to": to_version,
                        "at": at,
                        "effect": effect,
                    },
                )

    def drop_subscriptions(self, thread: str, by: str, ticket_id: str | None = None, reason: str | None = None) -> bool:
        current = self.repo.threads.get(thread)
        tickets = [t for t in (current or {}).get("subscriptions", []) if ticket_id is None or t == ticket_id]
        if not current or not tickets:
            return False
        current["subscriptions"] = [t for t in current["subscriptions"] if t not in tickets]
        self.repo.threads.save(current)
        for tid in tickets:
            ticket = self.repo.tickets.get(tid)
            if not ticket:
                continue
            follows = ticket.get("follows")
            if not follows or not any(f["thread"] == thread for f in follows):
                continue
            new_follows = [f for f in follows if f["thread"] != thread]
            saved = {k: v for k, v in ticket.items() if k != "follows"}
            if new_follows:
                saved["follows"] = new_follows
            self.repo.tickets.save(saved)
        for tid in tickets:
            payload = {"threadKey": current["key"], "ticketId": tid, "by": by}
            if reason:
                payload["reason"] = reason
            self.emit("thread.unsubscribed", payload)
        return True

    def add_input(self, ticket: dict, inp: dict) -> None:
        if any(i["id"] == inp["id"] for i in ticket.get("inputs", [])):
            return
        ticket.setdefault("inputs", []).append(inp)
        ticket["updatedAt"] = self.now()
        held = live_ticket(ticket) and automatic(inp) and waits_for_person(ticket)
        if not held:
            ticket.setdefault("undelivered", []).append(inp["id"])
        self.repo.tickets.save(ticket)
        if not live_ticket(ticket) or held:
            return
        running = running_assignment(ticket)
        latest = latest_assignment(ticket)
        if running:
            self.options.executor.deliver(ticket, running)
        elif latest and latest.get("state") == "pending" and not waits_for_person(ticket):
            self.options.executor.start(ticket, latest)

    def wake(self, ticket_id: str) -> None:
        ticket = self.repo.tickets.get(ticket_id)
        if not ticket or live_ticket(ticket) or (ticket.get("ended") or {}).get("by") == "human":
            return
        if not ticket.get("undelivered"):
            return
        policy = self.policy()
        if policy.paused:
            return
        if not policy.revalidate(ticket["mandate"]):
            self._refuse(ticket)
            return
        first = None
        for uid in ticket.get("undelivered", []):
            inp = next((i for i in ticket.get("inputs", []) if i["id"] == uid), None)
            if inp and inp.get("kind") == "trigger" and inp.get("human"):
                first = inp
                break
        if not first:
            return
        at = self.now()
        ticket.pop("ended", None)
        ticket["updatedAt"] = at
        assignment = self._new_assignment(ticket)
        resumed = {"id": key("input", ticket["id"], "resumed", first["triggerId"]), "kind": "resumed", "triggerId": first["triggerId"], "at": at}
        ticket.setdefault("inputs", []).append(resumed)
        ticket.setdefault("undelivered", []).insert(0, resumed["id"])
        self.repo.tickets.save(ticket)
        self.options.executor.start(ticket, assignment)
        self.emit("ticket.resumed", {"ticketId": ticket_id, "triggerId": first["triggerId"]})

    def restart(self, ticket: dict, running: dict, reason: str, note: str) -> None:
        self._end_assignment(ticket, running, reason)
        self._requeue_untaken(ticket, running)
        assignment = self._new_assignment(ticket)
        at = self.now()
        inp = {"id": key("input", ticket["id"], "restart", assignment["id"]), "kind": "text", "text": note, "at": at}
        ticket.setdefault("inputs", []).append(inp)
        ticket.setdefault("undelivered", []).append(inp["id"])
        ticket["updatedAt"] = at
        self.repo.tickets.save(ticket)
        self.options.executor.start(ticket, assignment)

    def end(self, ticket_id: str, by: str, outcome: str, at: str | None = None, reason: str | None = None) -> None:
        at = at or self.now()
        ticket = self.require(ticket_id)
        if not live_ticket(ticket):
            return
        stopping = [a for a in ticket.get("assignments", []) if a.get("state") != "ended"]
        why = {"reason": reason} if reason and reason != outcome else {}
        until = read_until(ticket)
        waiting = self._triggers_of(ticket, ticket.get("undelivered"))
        left_ids = [
            inp["id"]
            for inp in ticket.get("inputs", [])
            if inp.get("kind") == "trigger"
            and (not _read_before(inp, until) or inp["id"] in ticket.get("undelivered", []))
        ]
        left = self._untaken(ticket, left_ids)
        ticket["ended"] = {"at": at, "by": by, "outcome": outcome, **why}
        ticket["undelivered"] = []
        ticket["updatedAt"] = self.now()
        ticket.pop("attention", None)
        ticket.pop("delivery", None)
        for assignment in stopping:
            assignment["state"] = "ended"
            assignment["endedAt"] = at
        self.repo.tickets.save(ticket)
        self._let_go(ticket_id, waiting, left)
        self.emit("ticket.ended", {"ticketId": ticket_id, "by": by, "outcome": outcome, **why})
        for assignment in stopping:
            self.options.executor.stop(ticket, assignment, reason or outcome)

    def note(self, ticket_id: str, text: str) -> dict:
        ticket = self.require(ticket_id)
        if not live_ticket(ticket):
            raise UserError("ticketEnded")
        at = self.now()
        self.add_input(ticket, {"id": key("input", ticket_id, "note", at, text), "kind": "note", "text": text, "at": at})
        return self.require(ticket_id)

    def follow_for_context(self, ticket_id: str, ref: dict) -> bool:
        if not live_ticket(self.require(ticket_id)):
            raise UserError("ticketEnded")
        thread = self.thread(ref)
        if ticket_id in thread.get("subscriptions", []):
            return False
        self._subscribe(ticket_id, ref)
        thread = self.thread(ref)
        ctx = list(thread.get("context") or [])
        ctx.append(ticket_id)
        thread["context"] = ctx
        self.repo.threads.save(thread)
        return True

    def _triggers_of(self, ticket: dict, ids: list[str] | None = None) -> list[dict]:
        result = []
        for inp in ticket.get("inputs", []):
            if inp.get("kind") != "trigger":
                continue
            if ids is not None and inp["id"] not in ids:
                continue
            trigger = self.repo.triggers.get(inp["triggerId"])
            if trigger:
                result.append(trigger)
        return result

    def _taken_up(self, ticket: dict, trigger: dict) -> bool:
        from superagent.core.ids import key as id_key

        disposition = self.repo.triggers.disposition(trigger["id"])
        if disposition and disposition.get("acked"):
            return ticket["id"] in disposition["acked"]
        return self.options.outbox.status(id_key("ack", trigger["id"])) is not None

    def _untaken(self, ticket: dict, ids: list[str]) -> list[dict]:
        return [
            t
            for t in self._triggers_of(ticket, ids)
            if ticket["id"] in self.owners(t) and not self._taken_up(ticket, t)
        ]

    def _let_go(self, ticket_id: str, waiting: list[dict], left: list[dict]) -> None:
        for trigger in waiting + left:
            disposition = self.repo.triggers.disposition(trigger["id"])
            if disposition and ticket_id in disposition.get("fanout", []):
                fanout = [i for i in disposition["fanout"] if i != ticket_id]
                self.repo.triggers.saveDisposition(trigger["id"], {**disposition, "fanout": fanout})
        for trigger in left:
            disposition = self.repo.triggers.disposition(trigger["id"]) or {}
            decision = self.repo.decisions.get(disposition["decisionId"]) if disposition.get("decisionId") else None
            stopped = (
                decision
                and decision.get("kind") == "stop"
                and decision.get("ticketId") == ticket_id
                and not disposition.get("handbacks")
            )
            decided = bool(decision and (settled(decision) or stopped))
            acked = disposition.get("acked") or []
            if not self.owners(trigger) and not acked and not decided:
                self.queue_for_eval(trigger)

    def _refuse(self, ticket: dict) -> None:
        waiting = self._triggers_of(ticket, ticket.get("undelivered"))
        left = self._untaken(ticket, ticket.get("undelivered", []))
        ticket["undelivered"] = []
        self.repo.tickets.save(ticket)
        self._let_go(ticket["id"], waiting, left)

    def _requeue_untaken(self, ticket: dict, replaced: dict) -> None:
        untaken = [
            inp["id"]
            for inp in ticket.get("inputs", [])
            if inp.get("kind") == "trigger"
            and inp.get("readAt") is None
            and inp.get("deliveredTo") == replaced["id"]
            and inp["id"] not in ticket.get("undelivered", [])
        ]
        for uid in reversed(untaken):
            ticket.setdefault("undelivered", []).insert(0, uid)

    def _unfinished(self, ticket: dict, at: str) -> bool:
        until = read_until(ticket, False)
        taken = any(
            ticket["id"] in self.owners(t)
            and self._taken_up(ticket, t)
            for t in self._triggers_of(
                ticket,
                [inp["id"] for inp in ticket.get("inputs", []) if inp.get("kind") == "trigger" and not _read_before(inp, until)],
            )
        )
        latest = latest_assignment(ticket)
        if taken:
            return taken
        if not latest or time_ms(latest.get("createdAt", "")) <= time_ms(at):
            return False
        report = latest.get("report")
        later = any(
            inp.get("kind") != "policy-changed" and time_ms(inp.get("at", "")) > time_ms((report or {}).get("at", ""))
            for inp in ticket.get("inputs", [])
        )
        return (report or {}).get("state") != "ready" or bool(ticket.get("undelivered")) or later

    def _new_assignment(self, ticket: dict) -> dict:
        assignment = {
            "id": key("assignment", ticket["id"], str(len(ticket.get("assignments", [])))),
            "state": "pending",
            "createdAt": self.now(),
        }
        ticket.setdefault("assignments", []).append(assignment)
        ticket.pop("delivery", None)
        return assignment

    def _end_assignment(self, ticket: dict, assignment: dict, reason: str) -> None:
        assignment["state"] = "ended"
        assignment["endedAt"] = self.now()
        assignment["endedReason"] = reason
        self.emit("assignment.ended", {"ticketId": ticket["id"], "assignmentId": assignment["id"], "reason": reason})

    def _subscribe(self, ticket_id: str, ref: dict) -> None:
        thread = self.thread(ref)
        if ticket_id not in thread.get("subscriptions", []):
            thread.setdefault("subscriptions", []).append(ticket_id)
            self.emit("thread.subscribed", {"threadKey": thread["key"], "ticketId": ticket_id})
        if ticket_id in (thread.get("context") or []):
            thread["context"] = [c for c in thread["context"] if c != ticket_id]
        self.repo.threads.save(thread)
