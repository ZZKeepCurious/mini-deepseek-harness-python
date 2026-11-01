from superagent.core.model import latest_assignment, waits_for_person


def ticket_stage(ticket: dict) -> dict:
    if ticket.get("ended"):
        return {"stage": "ended"}
    if ticket.get("delivery"):
        return {"stage": "person", "action": "delivery"}
    if ticket.get("attention"):
        return {"stage": "person", "action": "answer"}
    assignment = latest_assignment(ticket)
    report = (assignment.get("report") or {}).get("state") if assignment else None
    if report == "ready":
        return {"stage": "person", "action": "review"}
    if report in ("blocked", "failed") and waits_for_person(ticket):
        return {"stage": "person", "action": report}
    if not assignment or assignment.get("state") == "pending":
        return {"stage": "working", "action": "queued"}
    if report == "waiting" or assignment.get("state") == "ended":
        return {"stage": "idle"}
    return {"stage": "working"}


def can_retry(ticket: dict) -> bool:
    return not ticket.get("ended") and (waits_for_person(ticket) or bool(ticket.get("delivery")))
