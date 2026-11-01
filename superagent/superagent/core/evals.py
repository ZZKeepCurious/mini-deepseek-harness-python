from __future__ import annotations

from datetime import datetime, timezone

from superagent.core.errors import UserError
from superagent.core.ids import key
from superagent.core.model import live_ticket, thread_key
from superagent.core.part import EnginePart, EngineOptions
from superagent.core.reasons import said

REQUEUES = 2

Outcome = dict


def _reason_of(reason) -> list[dict]:
    if isinstance(reason, str):
        return [said(reason)]
    return [reason]


def _stronger(a: str, b: str) -> str:
    return "explicit" if a == "explicit" or b == "explicit" else "inferred"


def _parse_iso(value: str) -> float:
    if value.endswith("Z"):
        value = value.replace("Z", "+00:00")
    return datetime.fromisoformat(value).timestamp()


class Evals(EnginePart):
    def __init__(self, options: EngineOptions, tickets) -> None:
        super().__init__(options)
        self.tickets = tickets

    def due(self, thread: dict, policy) -> None:
        evaluation = thread.get("evaluating")
        if not evaluation or (policy.paused and evaluation.get("pausedAt")):
            return
        now = self.now()
        if policy.paused:
            evaluation["pausedAt"] = now
        elif evaluation.get("pausedAt"):
            shift = _parse_iso(now) - _parse_iso(evaluation["pausedAt"])
            evaluation["deadline"] = datetime.fromtimestamp(
                _parse_iso(evaluation["deadline"]) + shift, tz=timezone.utc
            ).isoformat().replace("+00:00", "Z")
            evaluation.pop("pausedAt", None)
        elif evaluation.get("delivered") and evaluation["deadline"] <= now:
            self._expire(thread)
            return
        else:
            return
        self.repo.threads.save(thread)

    def delivered(self, eval_id: str, policy) -> None:
        thread = next((t for t in self.repo.threads.active() if (t.get("evaluating") or {}).get("id") == eval_id), None)
        if not thread or not thread.get("evaluating") or thread["evaluating"].get("delivered"):
            return
        now = self.now()
        thread["evaluating"]["delivered"] = now
        thread["evaluating"]["deadline"] = datetime.fromtimestamp(
            _parse_iso(now) + policy.evalTimeoutMs / 1000, tz=timezone.utc
        ).isoformat().replace("+00:00", "Z")
        self.repo.threads.save(thread)

    def _expire(self, thread: dict) -> None:
        expired = (thread.get("expired") or 0) + 1
        evaluation = thread["evaluating"]
        if expired > REQUEUES:
            self._finish(thread, evaluation, {"kind": "unavailable", "reason": [{"code": "timeout"}]})
            return
        eval_id = evaluation["id"]
        trigger_ids = evaluation["triggerIds"]
        thread.pop("evaluating", None)
        thread["expired"] = expired
        pending = [i for i in thread.get("pending", []) if i not in trigger_ids]
        thread["pending"] = [*trigger_ids, *pending]
        self.repo.threads.save(thread)
        release = getattr(self.options.assessor, "release", None)
        if release:
            release(eval_id)

    def start(self, thread_key_value: str, policy) -> None:
        thread = self.repo.threads.get(thread_key_value)
        if not thread:
            return
        at = self.now()

        def delivered(id: str) -> bool:
            trigger = self.repo.triggers.get(id)
            return bool(trigger and self.owners(trigger))

        if any(delivered(i) for i in thread.get("pending", [])):
            followups = [i for i in thread.get("pending", []) if delivered(i)]
            thread["pending"] = [i for i in thread.get("pending", []) if i not in followups]
            self.repo.threads.save(thread)
            for trigger_id in followups:
                self.emit("trigger.followup", {"triggerId": trigger_id})
            if not thread.get("pending"):
                return
        accepted = self._accepted(thread["key"])
        started = [i for i in thread.get("pending", []) if i in accepted]

        def started_ticket(id: str) -> dict:
            return {"code": "startedTicket", "triggerId": id, "values": {"ticket": accepted[id]["ticketId"]}}

        if started:
            self._ignore(thread, started, [started_ticket(i) for i in started], policy, at)
        triggers = []
        for id in thread.get("pending", []):
            trigger = self.repo.triggers.get(id)
            if trigger:
                triggers.append(trigger)
        tickets = self.tickets_on_thread(thread)
        reasons: list[dict] = []
        strongest = None
        for trigger in triggers:
            gate = policy.gate(trigger, thread, tickets)
            if not gate.get("admissible"):
                reasons.append({"code": gate["reason"], "triggerId": trigger["id"]})
                continue
            later = gate["strength"] == (strongest or {}).get("strength") and trigger["at"] >= strongest["trigger"]["at"]
            weaker = not strongest or _stronger(strongest["strength"], gate["strength"]) != strongest["strength"]
            if weaker or later:
                strongest = {"trigger": trigger, "strength": gate["strength"], "basis": gate["basis"]}
        if not strongest:
            if thread.get("pending"):
                self._ignore(
                    thread,
                    list(thread["pending"]),
                    reasons if reasons else [{"code": "noAdmissible"}],
                    policy,
                    at,
                )
            self.repo.threads.save(thread)
            return
        eval_id = key("eval", thread["key"], policy.version, at, *thread["pending"])
        deadline = datetime.fromtimestamp(_parse_iso(at) + policy.evalTimeoutMs / 1000, tz=timezone.utc).isoformat().replace(
            "+00:00", "Z"
        )
        strength = strongest["strength"]
        actor = strongest["trigger"]["actor"]
        basis = strongest["basis"]
        thread["evaluating"] = {
            "id": eval_id,
            "threadKey": thread["key"],
            "triggerIds": list(thread["pending"]),
            "primary": strongest["trigger"]["id"],
            "strength": strength,
            "actor": actor,
            "basis": basis,
            "policyVersion": policy.version,
            "startedAt": at,
            "deadline": deadline,
        }
        thread["pending"] = []
        self.repo.threads.save(thread)
        for trigger in triggers:
            disposition = self.repo.triggers.disposition(trigger["id"]) or {"fanout": [], "at": at}
            self.repo.triggers.saveDisposition(trigger["id"], {**disposition, "evalId": eval_id})
        self.emit(
            "eval.started",
            {"evalId": eval_id, "threadKey": thread["key"], "triggerIds": thread["evaluating"]["triggerIds"], "strength": strength},
        )
        self.options.assessor.request(self._candidate_of(thread))

    def candidate(self, trigger_id: str):
        thread = self._holding(trigger_id)
        return self._candidate_of(thread) if thread else None

    def decide(self, trigger_id: str, outcome: dict) -> dict:
        trigger = self.repo.triggers.get(trigger_id)
        if not trigger:
            raise UserError("unknownTrigger")
        held = self._holding(trigger_id)
        if outcome.get("evalId") and (held or {}).get("evaluating", {}).get("id") != outcome.get("evalId"):
            raise UserError("evalFinished", {"eval": outcome["evalId"]})
        earlier = None
        if outcome.get("kind") == "accept":
            earlier = self._accepted(thread_key(trigger["threads"][0])).get(trigger_id)
        if earlier:
            others = [i for i in (held or {}).get("evaluating", {}).get("triggerIds", []) if i != trigger_id]
            if held and others:
                self._reassess(held, {"code": "acceptedBefore", "values": {"trigger": trigger_id}}, [trigger_id])
            elif held:
                self._finish(
                    held,
                    held["evaluating"],
                    {
                        "kind": "ignore",
                        "reason": [{"code": "startedTicket", "triggerId": trigger_id, "values": {"ticket": earlier["ticketId"]}}],
                    },
                )
            return earlier
        if outcome.get("kind") != "stop":
            self.left_to_workers(trigger)
        thread = held or self.thread(trigger["threads"][0])
        evaluation = (held or {}).get("evaluating") or self._direct_eval(
            thread, trigger, outcome["kind"], outcome.get("ticketId") if outcome.get("kind") == "stop" else None
        )
        if outcome.get("kind") == "accept" and evaluation.get("strength") != "explicit":
            if not self.policy().admitsInferred(outcome.get("rules") or []):
                raise UserError("notExplicit")
        return self._decide_eval(thread, evaluation, outcome)

    def reevaluate(self, trigger_id: str) -> bool:
        if not self._requeue(trigger_id):
            return False
        self.emit("trigger.requeued", {"triggerId": trigger_id})
        return True

    def _requeue(self, trigger_id: str) -> bool:
        trigger = self.repo.triggers.get(trigger_id)
        if not trigger:
            raise UserError("unknownTrigger")
        if self._holding(trigger_id):
            return False
        self.queue_for_eval(trigger)
        disposition = self.repo.triggers.disposition(trigger_id) or {"fanout": [], "at": self.now()}
        self.repo.triggers.saveDisposition(trigger_id, {**disposition, "requeued": True})
        return True

    def handback(self, ticket_id: str, trigger_id: str, reason: str) -> bool:
        ticket = self.require(ticket_id)
        if not any(i.get("kind") == "trigger" and i.get("triggerId") == trigger_id for i in ticket.get("inputs", [])):
            raise UserError("notReached")
        disposition = self.repo.triggers.disposition(trigger_id) or {}
        if any(h.get("ticketId") == ticket_id for h in disposition.get("handbacks") or []):
            raise UserError("handedBack")
        return self.hand_back(ticket_id, self.repo.triggers.get(trigger_id), reason)

    def dismiss(self, decision_id: str) -> dict:
        decision = self.repo.decisions.get(decision_id)
        if not decision or decision.get("kind") != "unavailable":
            raise UserError("unknownProblem")
        decision["dismissed"] = True
        self.repo.decisions.save(decision)
        return decision

    def withdraw(self, trigger: dict) -> None:
        thread = self.repo.threads.get(thread_key(trigger["threads"][0]))
        if not thread or trigger["id"] not in thread.get("pending", []):
            return
        thread["pending"] = [i for i in thread["pending"] if i != trigger["id"]]
        self.repo.threads.save(thread)

    def routed(self, trigger_id: str, ticket_id: str) -> None:
        held = self._holding(trigger_id)
        reason = {"code": "routed", "values": {"ticket": ticket_id}}
        if (held or {}).get("evaluating", {}).get("triggerIds") == [trigger_id]:
            self._finish(held, held["evaluating"], {"kind": "ignore", "reason": [reason]})
        elif held:
            self._reassess(held, {**reason, "triggerId": trigger_id}, [trigger_id])

    def policy_changed(self, from_version: str, to_version: str) -> None:
        for thread in self.repo.threads.active():
            if thread.get("evaluating"):
                self._reassess(thread, {"code": "policyChanged"})
        self.emit("policy.changed", {"from": from_version, "to": to_version})

    def _reassess(self, thread: dict, reason: dict, without: list[str] | None = None) -> dict:
        without = without or []
        ids = [i for i in thread["evaluating"]["triggerIds"] if i not in without]
        decision = self._finish(thread, thread["evaluating"], {"kind": "unavailable", "reason": [reason]})
        current = self.repo.threads.get(thread["key"])
        if current:
            pending = [i for i in current.get("pending", []) if i not in ids]
            current["pending"] = [*ids, *pending]
            self.repo.threads.save(current)
        return decision

    def _holding(self, trigger_id: str):
        trigger = self.repo.triggers.get(trigger_id)
        if not trigger:
            return None
        thread = self.repo.threads.get(thread_key(trigger["threads"][0]))
        if thread and trigger_id in (thread.get("evaluating") or {}).get("triggerIds", []):
            return thread
        return None

    def _candidate_of(self, thread: dict) -> dict:
        evaluation = thread["evaluating"]
        triggers = []
        for id in evaluation["triggerIds"]:
            trigger = self.repo.triggers.get(id)
            if trigger:
                triggers.append(trigger)
        return {
            "evalId": evaluation["id"],
            "thread": thread,
            "triggers": triggers,
            "strength": evaluation["strength"],
            "policyVersion": evaluation["policyVersion"],
            "tickets": self.tickets_on_thread(thread),
        }

    def _ignore(self, thread: dict, ids: list[str], reason: list[dict], policy, at: str) -> None:
        evaluation = {
            "id": key("eval", thread["key"], "ignored", policy.version, at, *ids),
            "threadKey": thread["key"],
            "triggerIds": ids,
            "strength": "inferred",
            "actor": {"source": thread["ref"]["source"], "id": ""},
            "basis": {"via": "followup"},
            "policyVersion": policy.version,
            "startedAt": at,
            "deadline": at,
        }
        self._record(thread, evaluation, {"kind": "ignore", "reason": reason})
        thread["pending"] = [i for i in thread.get("pending", []) if i not in ids]
        thread.pop("expired", None)

    def _accepted(self, thread_key_value: str) -> dict[str, dict]:
        accepted: dict[str, dict] = {}
        for decision in self.repo.decisions.byThread(thread_key_value):
            if decision.get("kind") != "accept" or not decision.get("ticketId"):
                continue
            for id in decision.get("triggerIds", []):
                if id not in accepted:
                    accepted[id] = decision
        return accepted

    def _direct_eval(self, thread: dict, trigger: dict, kind: str, ticket_id: str | None = None) -> dict:
        policy = self.policy()
        gate = policy.gate(trigger, thread, self.tickets_on_thread(thread))
        if not gate.get("admissible"):
            raise UserError("notAdmissible", {"reason": gate["reason"]})
        at = self.now()
        if trigger["id"] in thread.get("pending", []):
            thread["pending"] = [i for i in thread["pending"] if i != trigger["id"]]
            self.repo.threads.save(thread)
        parts = ["eval", thread["key"], "direct", trigger["id"], kind]
        if ticket_id:
            parts.append(ticket_id)
        parts.append(at)
        eval_id = key(*parts)
        return {
            "id": eval_id,
            "threadKey": thread["key"],
            "triggerIds": [trigger["id"]],
            "primary": trigger["id"],
            "strength": gate["strength"],
            "actor": trigger["actor"],
            "basis": gate["basis"],
            "policyVersion": policy.version,
            "startedAt": at,
            "deadline": at,
        }

    def _decide_eval(self, thread: dict, evaluation: dict, outcome: dict) -> dict:
        audit = {"audit": outcome["audit"]} if outcome.get("audit") else {}
        if self.policy().version != evaluation["policyVersion"]:
            if (thread.get("evaluating") or {}).get("id") == evaluation["id"]:
                return self._reassess(thread, {"code": "policyChanged"})
            return self._finish(thread, evaluation, {"kind": "unavailable", "reason": [{"code": "policyChanged"}], **audit})
        unassessed = (
            [{"code": "ignoredFromCard"}]
            if outcome.get("kind") == "ignore" and evaluation.get("strength") != "explicit" and not outcome.get("audit")
            else []
        )
        if outcome.get("kind") in ("ignore", "unavailable"):
            return self._finish(
                thread,
                evaluation,
                {"kind": outcome["kind"], "reason": [*unassessed, *_reason_of(outcome["reason"])], **audit},
            )
        if outcome.get("kind") == "stop":
            ticket = self.repo.tickets.get(outcome["ticketId"])
            if not ticket or not live_ticket(ticket):
                return self._finish(thread, evaluation, {"kind": "ignore", "reason": [{"code": "stopUnknown"}], **audit})
            if evaluation.get("strength") != "explicit":
                return self._finish(thread, evaluation, {"kind": "ignore", "reason": [{"code": "stopNotExplicit"}], **audit})
            decision = self._finish(
                thread,
                evaluation,
                {"kind": "stop", "reason": _reason_of(outcome["reason"]), "ticketId": ticket["id"], **audit},
            )
            reason = outcome["reason"] if isinstance(outcome["reason"], str) else None
            self.tickets.end(ticket["id"], "human", "stopped", self.now(), reason)
            return decision
        return self._accept(thread, evaluation, outcome)

    def _accept(self, thread: dict, evaluation: dict, outcome: dict) -> dict:
        at = self.now()
        basis = {**evaluation["basis"], **({"rules": outcome["rules"]} if outcome.get("rules") else {})}
        mandate = {
            "strength": evaluation["strength"],
            "policyVersion": evaluation["policyVersion"],
            "basis": basis,
            "actor": evaluation["actor"],
            "scope": outcome["scope"],
        }
        ticket_id = key("ticket", evaluation["id"])
        audit = {"audit": outcome["audit"]} if outcome.get("audit") else {}
        decision = self._finish(
            thread,
            evaluation,
            {"kind": "accept", "reason": _reason_of(outcome["reason"]), **audit, "mandate": mandate, "ticketId": ticket_id},
        )
        self.tickets.create(ticket_id, decision["id"], mandate, evaluation, outcome, at)
        return decision

    def _finish(self, thread: dict, evaluation: dict, outcome: dict) -> dict:
        decision = self._record(thread, evaluation, outcome)
        if (thread.get("evaluating") or {}).get("id") != evaluation["id"]:
            return decision
        thread.pop("evaluating", None)
        thread.pop("expired", None)
        self.repo.threads.save(thread)
        release = getattr(self.options.assessor, "release", None)
        if release:
            release(evaluation["id"])
        return decision

    def _record(self, thread: dict, evaluation: dict, outcome: dict) -> dict:
        at = self.now()
        decision = {
            "id": key("decision", evaluation["id"]),
            "evalId": evaluation["id"],
            "threadKey": thread["key"],
            "triggerIds": evaluation["triggerIds"],
            "policyVersion": evaluation["policyVersion"],
            "at": at,
            "kind": outcome["kind"],
            "reason": outcome["reason"],
            "strength": evaluation["strength"],
        }
        if outcome.get("ticketId"):
            decision["ticketId"] = outcome["ticketId"]
        if outcome.get("mandate"):
            decision["mandate"] = outcome["mandate"]
        if outcome.get("audit"):
            decision["audit"] = outcome["audit"]
        self.repo.decisions.save(decision)
        for id in evaluation["triggerIds"]:
            disposition = self.repo.triggers.disposition(id) or {"fanout": [], "at": at}
            self.repo.triggers.saveDisposition(
                id, {**disposition, "evalId": evaluation["id"], "decisionId": decision["id"]}
            )
        self.emit("decision.recorded", {"decisionId": decision["id"], "kind": decision["kind"], "threadKey": thread["key"]})
        return decision
