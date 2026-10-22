from __future__ import annotations

import time

from .protocol import PlatformAuthError, logout_account

__all__ = ["revoke_account"]


def revoke_account(origin: str, token: str, max_retries: int,
                   delay_ms: int, request_timeout_ms: int,
                   headers: dict[str, str], cancelled=lambda: False) -> None:
    """Revoke one removed grant in the background without restoring it locally."""
    for attempt in range(max_retries + 1):
        if cancelled():
            return
        if attempt > 0:
            deadline = time.monotonic() + (delay_ms * 2 ** (attempt - 1)) / 1000.0
            while time.monotonic() < deadline:
                if cancelled():
                    return
                time.sleep(0.02)
        try:
            logout_account(origin, token, request_timeout_ms, headers)
            return
        except PlatformAuthError:
            continue
