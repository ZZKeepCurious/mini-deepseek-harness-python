from __future__ import annotations

import re
from typing import Any

from .protocol import (
    AccountUnauthorizedError,
    PlatformAuthError,
    request_account,
    request_bonus_notified,
    request_unnotified_bonuses,
)

__all__ = [
    "profile",
    "read_account_detail",
    "read_unnotified_bonuses",
    "send_bonus_notified",
]

_DECIMAL = re.compile(r"^-?(?:\d+(?:\.\d*)?|\.\d+)(?:e[+-]?\d+)?$", re.IGNORECASE)


def profile(value: Any) -> dict:
    """Project Platform user data without retaining credentials or unneeded fields."""
    if not isinstance(value, dict):
        raise PlatformAuthError("protocol")
    email = value.get("email")
    if not isinstance(email, str):
        raise PlatformAuthError("protocol")
    identity = value.get("id_profile")
    if identity is not None and not isinstance(identity, dict):
        raise PlatformAuthError("protocol")
    name = identity.get("name") if identity is not None else None
    picture = identity.get("picture") if identity is not None else None
    if name is not None and not isinstance(name, str):
        raise PlatformAuthError("protocol")
    if picture is not None and not isinstance(picture, str):
        raise PlatformAuthError("protocol")
    account_id = value.get("id")
    if account_id is not None and not isinstance(account_id, str):
        raise PlatformAuthError("protocol")
    mobile = value.get("mobile")
    mobile_number = value.get("mobile_number")
    if "mobile" in value and not isinstance(mobile, str):
        raise PlatformAuthError("protocol")
    if "mobile_number" in value and not isinstance(mobile_number, str):
        raise PlatformAuthError("protocol")
    return {
        "id": account_id,
        "avatarUrl": picture or None,
        "name": name or None,
        "contact": mobile or mobile_number or email or None,
    }


def _wallet(value: Any) -> dict:
    if (not isinstance(value, dict) or value.get("currency") not in ("CNY", "USD")
            or not isinstance(value.get("balance"), str)
            or _DECIMAL.fullmatch(value["balance"]) is None):
        raise PlatformAuthError("protocol")
    return {"currency": value["currency"], "balance": value["balance"]}


def _summary(value: Any) -> dict:
    if not isinstance(value, dict):
        raise PlatformAuthError("protocol")
    normal = value.get("normal_wallets")
    bonus = value.get("bonus_wallets")
    if not isinstance(normal, list) or not isinstance(bonus, list):
        raise PlatformAuthError("protocol")
    return {
        "status": "ready",
        "value": [_wallet(item) for item in normal],
        "bonusWallets": [_wallet(item) for item in bonus],
    }


def read_account_detail(field: str, origin: str, token: str,
                        timeout_ms: int, headers: dict[str, str]):
    path = ("/auth-api/v0/users/current" if field == "profile"
            else "/api/v0/users/get_user_summary")
    try:
        payload = request_account(origin, path, token, timeout_ms, headers)
    except AccountUnauthorizedError:
        raise
    except PlatformAuthError:
        return {"status": "failed"}
    try:
        if field == "profile":
            return {"status": "ready", "value": profile(payload)}
        return _summary(payload)
    except PlatformAuthError:
        return {"status": "failed"}


def read_unnotified_bonuses(origin: str, token: str, timeout_ms: int,
                            headers: dict[str, str]) -> list[dict]:
    payload = request_unnotified_bonuses(origin, token, timeout_ms, headers)
    if not isinstance(payload, list):
        raise PlatformAuthError("protocol")
    bonuses = []
    for row in payload:
        if (not isinstance(row, dict) or not isinstance(row.get("order_id"), str)
                or not isinstance(row.get("msg"), str)):
            raise PlatformAuthError("protocol")
        title = row.get("title")
        expires_at = row.get("expires_at")
        if title is not None and not isinstance(title, str):
            raise PlatformAuthError("protocol")
        if expires_at is not None and not isinstance(expires_at, str):
            raise PlatformAuthError("protocol")
        item = {
            "orderId": row["order_id"],
            "expiresAt": expires_at or "",
            "message": row["msg"],
        }
        for key, target in (("campaign", "campaign"), ("amount", "amount"),
                            ("currency", "currency"), ("granted_at", "grantedAt")):
            if key in row:
                item[target] = row[key]
        if title is not None:
            item["title"] = title
        bonuses.append(item)
    return bonuses


def send_bonus_notified(origin: str, token: str, order_id: str,
                        timeout_ms: int, headers: dict[str, str]) -> None:
    request_bonus_notified(origin, token, order_id, timeout_ms, headers)
