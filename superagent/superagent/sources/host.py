from __future__ import annotations

from typing import Any, Protocol


HEALTH_TEXT = {
    "setupPending": "Waiting for the deployment check",
    "sourceOff": "Off",
}


def health(state: str, code: str, values: dict[str, str] | None = None) -> dict:
    values = values or {}
    detail = HEALTH_TEXT.get(code, code)
    for name, value in values.items():
        detail = detail.replace("{" + name + "}", value)
    result: dict[str, Any] = {"state": state, "detail": detail, "code": code}
    if values:
        result["values"] = values
    return result
