from __future__ import annotations


def deliver_and_decide(engine, assessor_requests: list, trigger_id: str, outcome: dict) -> dict:
    """After assessor.request, mark candidate delivered then decide (assessment protocol subset)."""
    candidate = assessor_requests[-1]
    engine.candidate_delivered(candidate["evalId"])
    return engine.decide(trigger_id, {**outcome, "evalId": candidate["evalId"]})
