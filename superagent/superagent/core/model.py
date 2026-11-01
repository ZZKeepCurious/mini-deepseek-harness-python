from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal, TypedDict


def thread_key(ref: dict[str, str]) -> str:
    return f"{ref['source']}:{ref['id']}"


def time_ms(value: str) -> int:
    if not value:
        return 0
    normalized = iso_time(value)
    dt = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def iso_time(value: str) -> str:
    try:
        if value.endswith("Z"):
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        else:
            dt = datetime.fromisoformat(value)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
    except ValueError:
        return value


def live_ticket(ticket: dict[str, Any]) -> bool:
    return ticket.get("ended") is None


def ticket_outcome(ticket: dict[str, Any]) -> str | None:
    ended = ticket.get("ended")
    return ended.get("outcome") if ended else None


def running_assignment(ticket: dict[str, Any]) -> dict[str, Any] | None:
    for assignment in ticket.get("assignments", []):
        if assignment.get("state") == "running":
            return assignment
    return None


def latest_assignment(ticket: dict[str, Any]) -> dict[str, Any] | None:
    assignments = ticket.get("assignments", [])
    return assignments[-1] if assignments else None


def latest_report(ticket: dict[str, Any]) -> dict[str, Any] | None:
    for assignment in reversed(ticket.get("assignments", [])):
        report = assignment.get("report")
        if report:
            return report
    return None


def read_until(ticket: dict[str, Any], progress: bool = True) -> str:
    report_at = ""
    for assignment in reversed(ticket.get("assignments", [])):
        report = assignment.get("report")
        if report and (progress or report.get("state") != "progress"):
            report_at = report.get("at", "")
            break
    idle_times = [assignment.get("idleAt") or "" for assignment in ticket.get("assignments", [])]
    return max([report_at, *idle_times])


def waits_for_person(ticket: dict[str, Any]) -> bool:
    assignment = latest_assignment(ticket)
    report = assignment.get("report") if assignment else None
    if not report or report.get("state") not in ("blocked", "failed"):
        return False
    report_at = report.get("at", "")
    for inp in ticket.get("inputs", []):
        if (
            inp.get("kind") == "trigger"
            and inp.get("human")
            and not inp.get("context")
            and inp.get("at", "") > report_at
        ):
            return False
    return True


def reached(
    ticket: dict[str, Any],
    trigger: dict[str, Any],
    thread_getter,
    context: bool = True,
) -> bool:
    if ticket.get("origin", {}).get("triggerId") == trigger.get("id"):
        return True
    has_input = any(
        item.get("kind") == "trigger" and item.get("triggerId") == trigger.get("id")
        for item in ticket.get("inputs", [])
    )
    for ref in trigger.get("threads", []):
        current = thread_getter(thread_key(ref))
        if current and ticket["id"] in current.get("subscriptions", []):
            if context or ticket["id"] not in (current.get("context") or []):
                return True
    if has_input:
        if not context:
            all_context = all(
                ticket["id"] in (thread_getter(thread_key(ref)) or {}).get("context", [])
                for ref in trigger.get("threads", [])
            )
            return not all_context
        return True
    return False


def followup_owners(trigger: dict[str, Any], records) -> list[str]:
    if not trigger.get("human"):
        return []
    disposition = records.triggers.disposition(trigger["id"])
    if not disposition or disposition.get("requeued"):
        return []
    returned = {item["ticketId"] for item in (disposition.get("handbacks") or [])}
    context = disposition.get("context") or []
    reached_ids = [tid for tid in disposition.get("fanout", []) if tid not in returned and tid not in context]
    if not reached_ids:
        return []
    primary = trigger["threads"][0]
    thread = records.threads.get(thread_key(primary))
    evaluating = thread.get("evaluating") if thread else None
    if evaluating and trigger["id"] in evaluating.get("triggerIds", []):
        return []
    owners: list[str] = []
    for tid in reached_ids:
        ticket = records.tickets.get(tid)
        if not ticket:
            continue
        if live_ticket(ticket):
            owners.append(tid)
            continue
        ended = ticket.get("ended") or {}
        if ended.get("by") == "human":
            continue
        waiting = any(
            inp.get("kind") == "trigger"
            and inp.get("triggerId") == trigger["id"]
            and inp.get("id") in ticket.get("undelivered", [])
            for inp in ticket.get("inputs", [])
        )
        if waiting:
            owners.append(tid)
    return owners


def settled(decision: dict[str, Any]) -> bool:
    if decision.get("kind") in ("stop", "unavailable"):
        return False
    for reason in decision.get("reason", []):
        if reason.get("code") in ("stopNotExplicit", "stopUnknown", "routed"):
            return False
    return True
