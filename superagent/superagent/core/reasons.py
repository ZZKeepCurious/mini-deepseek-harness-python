from typing import Any

REASONS: dict[str, str] = {
    "said": "{text}",
    "timeout": "Assessment timed out",
    "policyChanged": "Policy changed during assessment",
    "noAdmissible": "No admissible trigger",
    "startedTicket": "Already started ticket {ticket}",
    "acceptedBefore": "Trigger {trigger} was accepted before",
    "routed": "Routed to ticket {ticket}",
    "explicitRouted": "Explicit request from an allowed sender routed by the orchestrator",
    "humanStop": "Human asked to stop",
    "legacyImport": "Imported from the pre-domain-model task record",
    "forwarded": "Forwarded to the worker of the Ticket already on this Thread",
    "senderNotMember": "The sender is not on the Source's list",
    "retried": "Retried by a person",
    "hostRestarted": "dsh-superagent restarted",
    "stopUnknown": "Stop target is unknown or already ended",
    "stopNotExplicit": "A stop needs an explicit request from a sender on the list",
    "directionUnsettled": "Implementation intent or target direction is not established, or a product/design decision is still required",
    "noRuleMatched": "No enabled admission rule matched",
    "ignoredFromCard": "Ignored from its card, without a rule assessment",
    "observed": "Observe mode: would accept ({text})",
}


def said(text: str) -> dict[str, Any]:
    return {"code": "said", "values": {"text": text}}


def reason_text(reasons: list[dict[str, Any]] | dict[str, Any] | None) -> str:
    items = reasons if isinstance(reasons, list) else ([] if reasons is None else [reasons])
    parts: list[str] = []
    for reason in items:
        template = REASONS.get(reason["code"], reason["code"])
        values = reason.get("values") or {}
        text = template
        for name, value in values.items():
            text = text.replace("{" + name + "}", value)
        trigger_id = reason.get("triggerId")
        parts.append(f"{trigger_id}: {text}" if trigger_id else text)
    return "; ".join(parts)
