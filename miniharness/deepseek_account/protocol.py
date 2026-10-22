from __future__ import annotations

import json
import re
from urllib.parse import urlsplit

import httpx

__all__ = [
    "AccountUnauthorizedError",
    "PlatformAuthError",
    "browser_url",
    "login_origin",
    "logout_account",
    "merge_platform_cookies",
    "platform_client_headers",
    "platform_headers",
    "platform_origin",
    "platform_wire_locale",
    "request_account",
    "request_bonus_notified",
    "request_platform",
    "request_unnotified_bonuses",
]

_RESERVED_HEADERS = {
    "authorization", "x-dsh-auth-token", "host", "content-length",
    "transfer-encoding", "connection", "content-type",
}
_BODY_LIMIT = 65_536
_LOGIN_ORIGIN = re.compile(
    r"^http://(?:localhost|127\.0\.0\.1|\[::1\]):([0-9]+)/?$", re.IGNORECASE)
_LOOPBACK = {"localhost", "127.0.0.1", "::1"}


class PlatformAuthError(Exception):
    """Stable account failure code, never a response body or authorization URL."""

    def __init__(self, code: str):
        super().__init__(f"account: {code}")
        self.code = code
        self.name = "PlatformAuthError"


class AccountUnauthorizedError(PlatformAuthError):
    """Platform rejected the stored grant with HTTP 401 or business code 40003."""

    def __init__(self):
        super().__init__("expired")
        self.name = "AccountUnauthorizedError"


def platform_origin(value: str, allow_loopback_http: bool) -> str:
    parts = urlsplit(value)
    host = parts.hostname or ""
    loopback = host in _LOOPBACK
    valid_scheme = parts.scheme == "https" or (
        allow_loopback_http and loopback and parts.scheme == "http")
    if (not host or parts.username or parts.password or parts.path not in ("", "/")
            or parts.query or parts.fragment or not valid_scheme):
        raise ValueError(
            "account: platformOrigin must be an HTTPS origin or explicitly enabled "
            "loopback HTTP origin")
    port = parts.port
    if port is not None and not 0 < port <= 65_535:
        raise ValueError("account: platformOrigin has an invalid port")
    default_port = 443 if parts.scheme == "https" else 80
    hostname = f"[{host}]" if ":" in host else host
    suffix = "" if port is None or port == default_port else f":{port}"
    return f"{parts.scheme}://{hostname}{suffix}"


def _origin_of(parts) -> str:
    host = parts.hostname or ""
    if not host:
        raise PlatformAuthError("protocol")
    try:
        port = parts.port
    except ValueError as error:
        raise PlatformAuthError("protocol") from error
    default_port = 443 if parts.scheme == "https" else 80
    hostname = f"[{host}]" if ":" in host else host
    suffix = "" if port is None or port == default_port else f":{port}"
    return f"{parts.scheme}://{hostname}{suffix}"


def browser_url(value: str, origin: str, path: str, rewrite_origin: bool = False) -> str:
    try:
        parts = urlsplit(value)
    except (TypeError, ValueError) as error:
        raise PlatformAuthError("protocol") from error
    candidate = _origin_of(parts)
    allowed_origin = candidate == origin or (
        rewrite_origin and parts.scheme == "https")
    if (not allowed_origin or parts.path != path or parts.username or parts.password
            or parts.fragment):
        raise PlatformAuthError("protocol")
    base = origin if rewrite_origin else candidate
    normalized = f"{base}{parts.path}"
    if parts.query:
        normalized = f"{normalized}?{parts.query}"
    return normalized


def platform_headers(values: dict[str, str]) -> dict[str, str]:
    headers: dict[str, str] = {}
    names: set[str] = set()
    for name, value in values.items():
        if (not isinstance(name, str) or not isinstance(value, str)
                or "\r" in value or "\n" in value):
            raise ValueError("account: requestHeaders contains an invalid header")
        key = name.lower()
        if key in _RESERVED_HEADERS or key in names:
            raise ValueError("account: requestHeaders contains a reserved or duplicate header")
        names.add(key)
        headers[key] = value.strip()
    return headers


def merge_platform_cookies(base: str, override: str) -> str:
    cookies: dict[str, str] = {}
    for header in (base, override):
        for pair in header.split(";"):
            separator = pair.find("=")
            if separator < 1:
                continue
            cookies[pair[:separator].strip()] = pair[separator + 1:].strip()
    return "; ".join(f"{name}={value}" for name, value in cookies.items())


def platform_wire_locale(locale: str) -> str:
    return "zh_CN" if locale.lower().split("-")[0].split("_")[0] == "zh" else "en_US"


def platform_client_headers(platform: str | None, client: dict) -> dict[str, str]:
    headers = {
        "x-client-bundle-id": "",
        "x-client-platform": "web",
        "x-client-version": client["version"],
        "x-client-locale": platform_wire_locale(client["locale"]),
        "x-client-timezone-offset": str(client["timezoneOffsetSeconds"]),
    }
    if platform == "win32":
        headers["x-client-platform"] = "desktop-win"
    elif platform == "darwin":
        headers["x-client-platform"] = "desktop-mac"
    return headers


def login_origin(value: str) -> str:
    match = _LOGIN_ORIGIN.fullmatch(value)
    if match is None or not 0 < int(match.group(1)) <= 65_535:
        raise PlatformAuthError("protocol")
    parts = urlsplit(value)
    if (parts.username or parts.password or parts.path not in ("", "/")
            or parts.query or parts.fragment):
        raise PlatformAuthError("protocol")
    host = parts.hostname or ""
    hostname = f"[{host}]" if ":" in host else host
    return f"http://{hostname}:{int(match.group(1))}"


def request_platform(origin: str, method: str, body: dict,
                     timeout_ms: int, headers: dict[str, str]):
    return _platform_request(
        f"{origin}/auth-api/v0/dsh/{method}",
        "POST", timeout_ms,
        {**headers, "content-type": "application/json"},
        body)


def request_account(origin: str, path: str, token: str,
                    timeout_ms: int, headers: dict[str, str]):
    return _platform_request(
        f"{origin}{path}", "GET", timeout_ms,
        {**headers, "x-dsh-auth-token": token}, None)


def request_unnotified_bonuses(origin: str, token: str,
                               timeout_ms: int, headers: dict[str, str]):
    return _platform_request(
        f"{origin}/api/v0/users/get_unnotified_bonuses", "GET", timeout_ms,
        {**headers, "x-dsh-auth-token": token}, None)


def request_bonus_notified(origin: str, token: str, order_id: str,
                           timeout_ms: int, headers: dict[str, str]):
    return _platform_request(
        f"{origin}/api/v0/users/ack_bonus_notified", "POST", timeout_ms,
        {**headers, "x-dsh-auth-token": token,
         "content-type": "application/json"},
        {"order_id": order_id})


def logout_account(origin: str, token: str, timeout_ms: int,
                   headers: dict[str, str]) -> None:
    _platform_request(
        f"{origin}/auth-api/v0/users/logout", "POST", timeout_ms,
        {**headers, "x-dsh-auth-token": token}, None)


def _platform_request(url: str, method: str, timeout_ms: int,
                      headers: dict[str, str], body: dict | None):
    timeout = httpx.Timeout(timeout_ms / 1000.0)
    authenticated = "x-dsh-auth-token" in {name.lower() for name in headers}
    kwargs = {"json": body} if body is not None else {}
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            with client.stream(method, url, headers=headers, **kwargs) as response:
                if response.status_code == 401 and authenticated:
                    raise AccountUnauthorizedError()
                if 300 <= response.status_code < 400:
                    raise PlatformAuthError("no-response")
                if not 200 <= response.status_code < 300:
                    raise PlatformAuthError("network")
                chunks: list[bytes] = []
                size = 0
                try:
                    for chunk in response.iter_bytes():
                        size += len(chunk)
                        if size > _BODY_LIMIT:
                            raise PlatformAuthError("protocol")
                        chunks.append(chunk)
                except httpx.HTTPError as error:
                    raise PlatformAuthError("protocol") from error
    except httpx.HTTPError as error:
        raise PlatformAuthError("no-response") from error
    if not chunks:
        raise PlatformAuthError("network")
    try:
        payload = json.loads(b"".join(chunks).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PlatformAuthError("protocol") from error
    if not isinstance(payload, dict):
        raise PlatformAuthError("protocol")
    code = payload.get("code")
    if isinstance(code, int) and not isinstance(code, bool) and code == 40003 \
            and authenticated:
        raise AccountUnauthorizedError()
    data = payload.get("data")
    if (isinstance(code, bool) or not isinstance(code, int) or code != 0
            or not isinstance(data, dict)):
        raise PlatformAuthError("protocol")
    biz_code = data.get("biz_code")
    if isinstance(biz_code, bool) or not isinstance(biz_code, int) or biz_code != 0:
        raise PlatformAuthError("protocol")
    return data.get("biz_data")
