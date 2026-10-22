from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
import uuid
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urlencode, urlsplit, urlunsplit

from ..commands import AbortSignal
from ..core.scope import Context
from ..seams.credentials_local import credential_key
from . import DeepSeekAccount, SIGN_IN_ERROR_CODES
from .details import profile, read_account_detail, read_unnotified_bonuses, send_bonus_notified
from .logout import revoke_account
from .protocol import (
    AccountUnauthorizedError,
    PlatformAuthError,
    browser_url,
    login_origin,
    merge_platform_cookies,
    platform_client_headers,
    platform_headers,
    platform_origin,
    platform_wire_locale,
    request_platform,
)

__all__ = ["OAUTH_CALLBACK_PATH", "PlatformAccount", "run_in_background"]

_KEY = credential_key("deepseek-account-platform", "default")
_DEVICE = credential_key("deepseek-account-platform", "device")
_TOKEN = re.compile(r"^[\x21-\x7e]+$")
_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
                   r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_ACTIVE_PHASES = {"initializing", "waiting-browser", "exchanging", "committing"}
OAUTH_CALLBACK_PATH = "/oauth/callback"


@dataclass
class _Attempt:
    id: str
    origin: str
    login_source: str
    locale: str
    client_headers: dict[str, str]
    controller: AbortSignal
    view: dict = field(default_factory=lambda: {
        "phase": "initializing", "id": "", "authorizeUrl": None,
        "expiresAt": None, "errorCode": None,
    })
    completion_url: str | None = None
    initial_profile: tuple[str, dict] | None = None
    oauth_state: str | None = None
    received_code: str | None = None
    callback_lock: threading.Lock = field(default_factory=threading.Lock)
    callback_started: bool = False
    callback_event: threading.Event = field(default_factory=threading.Event)
    callback_status: int = 204
    callback_headers: dict[str, str] = field(default_factory=lambda: {
        "cache-control": "no-store"})
    callback_body: bytes = b""
    thread: threading.Thread | None = None


class PlatformAccount(DeepSeekAccount):
    def __init__(self, ctx: Context, *, platform_origin_value: str = "https://platform.deepseek.com",
                 inference_origin: str = "https://api.deepseek.com",
                 allow_loopback_http: bool = False,
                 rewrite_browser_origin: bool = False,
                 desktop_platform: str | None = None,
                 embedded_page_dist: str = "",
                 request_headers: dict[str, str] | None = None,
                 account_request_headers: dict[str, str] | None = None,
                 request_timeout_ms: int = 30_000,
                 balance_timeout_ms: int = 30_000,
                 logout_max_retries: int = 5,
                 logout_retry_delay_ms: int = 1_000,
                 attempt_timeout_ms: int = 600_000):
        super().__init__(ctx)
        if desktop_platform not in (None, "darwin", "win32"):
            raise ValueError("account: desktopPlatform must be darwin, win32 or null")
        for name, value, low, high in (
                ("requestTimeoutMs", request_timeout_ms, 1, 120_000),
                ("balanceTimeoutMs", balance_timeout_ms, 1, 120_000),
                ("logoutRetryDelayMs", logout_retry_delay_ms, 1, 60_000),
                ("attemptTimeoutMs", attempt_timeout_ms, 1, 3_600_000)):
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not low <= value <= high):
                raise ValueError(f"account: {name} is out of range")
        if (isinstance(logout_max_retries, bool)
                or not isinstance(logout_max_retries, int)
                or not 0 <= logout_max_retries <= 5):
            raise ValueError("account: logoutMaxRetries is out of range")
        self._origin = platform_origin(platform_origin_value, allow_loopback_http)
        inference = urlsplit(inference_origin)
        if not inference.hostname:
            raise ValueError("account: inferenceOrigin must include a host")
        if (inference.scheme not in ("http", "https") or inference.username
                or inference.password or inference.path not in ("", "/")
                or inference.query or inference.fragment):
            raise ValueError(
                "account: inferenceOrigin must be an HTTP(S) origin without "
                "credentials, path, query or fragment")
        try:
            inference_port = inference.port
        except ValueError as error:
            raise ValueError("account: inferenceOrigin has an invalid port") from error
        default_port = 443 if inference.scheme == "https" else 80
        inference_host = inference.hostname or ""
        inference_host = f"[{inference_host}]" if ":" in inference_host else inference_host
        inference_suffix = ("" if inference_port is None or inference_port == default_port
                            else f":{inference_port}")
        self._inference_origin = (
            f"{inference.scheme}://{inference_host}{inference_suffix}")
        self._rewrite_browser_origin = rewrite_browser_origin
        self._desktop_platform = desktop_platform
        self._embedded_page_dist = embedded_page_dist
        self._request_headers = platform_headers(request_headers or {})
        account_headers = platform_headers(account_request_headers or {})
        self._account_headers = {**self._request_headers, **account_headers}
        if "cookie" in account_headers:
            self._account_headers["cookie"] = merge_platform_cookies(
                self._request_headers.get("cookie", ""), account_headers["cookie"])
        self._request_timeout = request_timeout_ms
        self._balance_timeout = balance_timeout_ms
        self._attempt_timeout = attempt_timeout_ms
        self._logout_max_retries = logout_max_retries
        self._logout_delay = logout_retry_delay_ms
        self._closed = False
        self._attempt: _Attempt | None = None
        self._lock = threading.RLock()
        self._removal_lock = threading.RLock()
        self._removing = False
        self._last_profile: tuple[str, dict] | None = None
        self._revocations: set[threading.Thread] = set()
        self._details_generation = 0
        credentials = ctx.get("credentials")
        if credentials is None:
            raise ValueError("account: platform sign-in requires the credentials service")
        record = credentials.read_record(_KEY)
        if record is not None:
            if record.get("kind") != "grant":
                raise PlatformAuthError("storage")
            payload = record.get("payload") or {}
            if (payload.get("version") != 1 or not isinstance(payload.get("token"), str)
                    or not payload["token"]):
                raise PlatformAuthError("storage")
            if payload.get("issuer") != self._origin:
                credentials.delete_record(_KEY)
        authorization = ctx.get("authorization")
        if authorization is None:
            raise ValueError("account: platform sign-in requires the authorization service")
        self._authorization = authorization
        self._authorization.registerFlow({
            "key": _KEY,
            "label": "DeepSeek",
            "methods": [{"id": "browser", "label": "DeepSeek"}],
            "run": self._run_flow,
        })
        ctx.on("credentials/record-updated", self._record_updated)
        ctx.effect(lambda: self._dispose, "account: active attempt lifetime")

    def _dispose(self) -> None:
        with self._removal_lock:
            self._dispose_locked()

    def _dispose_locked(self) -> None:
        with self._lock:
            self._closed = True
            attempt = self._attempt
        if attempt is not None and attempt.view["phase"] != "committing":
            attempt.controller.abort()
            self._authorization.cancel(_KEY)
        if attempt is not None and attempt.thread is not None:
            attempt.thread.join()
        self._last_profile = None
        self._details_generation += 1
        for revocation in list(self._revocations):
            revocation.join()

    def get_state(self) -> dict:
        record = self.ctx.get("credentials").read_record(_KEY)
        if record is not None:
            if record.get("kind") != "grant" or not self._valid_grant(record.get("payload")):
                raise PlatformAuthError("storage")
        with self._lock:
            attempt = ({key: value for key, value in self._attempt.view.items()
                        if value is not None} if self._attempt is not None else None)
            if record is None and attempt is not None and attempt["phase"] == "succeeded":
                attempt = None
        return {
            "status": "signed-out" if record is None else "credential-stored",
            "attempt": attempt,
            "links": {"usageUrl": f"{self._origin}/usage",
                      "topUpUrl": f"{self._origin}/top_up"},
        }

    def get_profile(self, client: dict | None = None) -> dict | None:
        lifetime = self._details_generation
        stored = self._read_current_grant(lifetime)
        if stored is None:
            return None
        result = self._get_detail("profile", self._detail_headers(client or {}),
                                  lifetime, stored)
        if lifetime != self._details_generation:
            return None
        if result is not None:
            self._cache_profile(stored["token"], result)
        if result is None or result.get("status") != "failed":
            return result
        cached = self._last_profile
        return cached[1] if cached is not None and cached[0] == stored["token"] else result

    def get_balance(self, client: dict | None = None) -> dict | None:
        lifetime = self._details_generation
        stored = self._read_current_grant(lifetime)
        if stored is None:
            return None
        return self._get_detail("balance", self._detail_headers(client or {}),
                                lifetime, stored)

    def get_unnotified_bonuses(self, client: dict | None = None) -> dict | None:
        lifetime = self._details_generation
        headers = self._detail_headers(client or {})
        stored = self._read_current_grant(lifetime)
        if stored is None:
            return None
        account_id = self._current_account_id(lifetime, stored, headers)
        if account_id is None:
            return None
        try:
            bonuses = read_unnotified_bonuses(
                self._origin, stored["token"], self._request_timeout, headers)
        except AccountUnauthorizedError:
            if lifetime == self._details_generation:
                self._expire_credential(stored["token"], lifetime)
            return None
        return {"accountId": account_id, "bonuses": bonuses}

    def ack_bonus_notified(self, account_id: str, order_id: str,
                           client: dict | None = None) -> bool:
        lifetime = self._details_generation
        headers = self._detail_headers(client or {})
        stored = self._read_current_grant(lifetime)
        if stored is None:
            return False
        current = self._current_account_id(lifetime, stored, headers)
        if current is None or current != account_id:
            return False
        try:
            send_bonus_notified(self._origin, stored["token"], order_id,
                                self._request_timeout, headers)
            return True
        except AccountUnauthorizedError:
            if lifetime == self._details_generation:
                self._expire_credential(stored["token"], lifetime)
            return False

    def start_sign_in(self, client: dict, callback_origin: str,
                      login_source: str) -> dict:
        if login_source not in ("web", "desktop"):
            raise PlatformAuthError("protocol")
        origin = login_origin(callback_origin)
        with self._removal_lock:
            pass
        with self._lock:
            if self._closed:
                raise PlatformAuthError("protocol")
            active = self._attempt
            if active is not None and active.view["phase"] in _ACTIVE_PHASES:
                return self.get_state()
        if active is not None and active.thread is not None:
            active.thread.join()
            with self._lock:
                if self._closed:
                    raise PlatformAuthError("protocol")
                if self._attempt is not active:
                    return self.get_state()
        with self._lock:
            attempt = _Attempt(
                id=str(uuid.uuid4()), origin=origin, login_source=login_source,
                locale=platform_wire_locale(client["locale"]),
                client_headers=platform_client_headers(self._desktop_platform, client),
                controller=AbortSignal())
            attempt.view["id"] = attempt.id
            self._attempt = attempt
            thread = threading.Thread(target=self._start_attempt, args=(attempt,),
                                      name=f"account-sign-in-{attempt.id[:8]}",
                                      daemon=True)
            attempt.thread = thread
            thread.start()
        return self.get_state()

    def cancel_sign_in(self, attempt_id: str) -> dict:
        with self._lock:
            attempt = self._attempt
        if attempt is not None and attempt.id == attempt_id:
            if attempt.view["phase"] != "committing":
                attempt.controller.abort()
                self._authorization.cancel(_KEY)
            if attempt.thread is not None:
                attempt.thread.join()
        return self.get_state()

    def sign_out(self, client: dict | None = None) -> dict:
        with self._removal_lock:
            self._removing = True
            try:
                return self._sign_out(client)
            finally:
                self._removing = False

    def _sign_out(self, client: dict | None) -> dict:
        with self._lock:
            if self._closed:
                raise PlatformAuthError("protocol")
            active = self._attempt
        if active is not None:
            self.cancel_sign_in(active.id)
        credentials = self.ctx.get("credentials")
        record = credentials.read_record(_KEY)
        if record is not None:
            if record.get("kind") != "grant" or not self._valid_grant(record.get("payload")):
                raise PlatformAuthError("storage")
            payload = record["payload"]
            if payload["issuer"] != self._origin:
                raise PlatformAuthError("protocol")
            credentials.delete_record(_KEY)
            self._revoke(payload["token"], platform_client_headers(
                self._desktop_platform, client or {}))
        with self._lock:
            self._attempt = None
        self.ctx.emit("deepseek-account/signed-out")
        return self.get_state()

    def resolve_token(self, base_url: str) -> str | None:
        if self._closed or self._removing:
            return None
        parts = urlsplit(base_url)
        if (parts.scheme not in ("http", "https") or parts.username
                or parts.password or not parts.hostname):
            return None
        try:
            port = parts.port
        except ValueError:
            return None
        default_port = 443 if parts.scheme == "https" else 80
        hostname = f"[{parts.hostname}]" if ":" in parts.hostname else parts.hostname
        suffix = "" if port is None or port == default_port else f":{port}"
        origin = f"{parts.scheme}://{hostname}{suffix}"
        if origin != self._inference_origin:
            return None
        record = self.ctx.get("credentials").read_record(_KEY)
        if record is None:
            return None
        if record.get("kind") != "grant" or not self._valid_grant(record.get("payload")):
            raise PlatformAuthError("storage")
        payload = record["payload"]
        issuer = urlsplit(payload["issuer"])
        token = payload["token"]
        if token.startswith("dsh_mock_"):
            return None
        if self._inference_origin == "https://api.deepseek.com":
            if issuer.hostname in ("localhost", "127.0.0.1", "::1"):
                return None
        elif payload["issuer"] != self._origin:
            return None
        return token

    def reject_token(self, token: str) -> None:
        lifetime = self._details_generation
        stored = self._read_current_grant(lifetime)
        if stored is None or stored["token"] != token or lifetime != self._details_generation:
            return
        self._expire_credential(token, lifetime)

    def get_platform_session(self) -> dict | None:
        lifetime = self._details_generation
        stored = self._read_current_grant(lifetime)
        if stored is None:
            return None
        cached = self._last_profile
        session = {
            "origin": self._origin,
            "token": stored["token"],
            "userId": (cached[1].get("value", {}).get("id")
                       if cached is not None and cached[0] == stored["token"] else None),
            "requestHeaders": dict(self._account_headers),
        }
        if self._embedded_page_dist:
            session["embeddedPageDist"] = self._embedded_page_dist
        return session

    def get_device_identity(self) -> dict:
        record = self.ctx.get("credentials").read_record(_DEVICE)
        device_id = None
        if record is not None and record.get("kind") == "grant":
            value = (record.get("payload") or {}).get("id")
            if isinstance(value, str) and _UUID.fullmatch(value):
                device_id = value
        session = self.get_platform_session()
        result = {"osVersion": _device_os_version()}
        if device_id is not None:
            result["deviceId"] = device_id
        if session is not None and session.get("userId") is not None:
            result["userId"] = session["userId"]
        return result

    def handle_callback(self, method: str, path: str,
                        query: dict[str, list[str]]) -> tuple[int, dict[str, str], bytes]:
        states = query.get("state") or []
        codes = query.get("code") or []
        with self._lock:
            attempt = self._attempt
            if attempt is None or attempt.view["phase"] != "waiting-browser":
                return 410, {"cache-control": "no-store"}, b""
            valid_state = (len(states) == 1 and attempt.oauth_state is not None
                           and len(states[0].encode("utf-8")) == len(
                               attempt.oauth_state.encode("utf-8"))
                           and hmac.compare_digest(
                               states[0].encode("utf-8"),
                               attempt.oauth_state.encode("utf-8")))
            if (method != "GET" or path != OAUTH_CALLBACK_PATH or not valid_state
                    or len(codes) != 1 or not codes[0] or attempt.callback_started):
                return 400, {"cache-control": "no-store"}, b""
            attempt.callback_started = True
            attempt.received_code = codes[0]
        attempt.callback_event.wait(self._attempt_timeout / 1000.0 + 5)
        with attempt.callback_lock:
            return (attempt.callback_status, dict(attempt.callback_headers),
                    attempt.callback_body)

    def _start_attempt(self, attempt: _Attempt) -> None:
        def decline(_prompt):
            raise PlatformAuthError("protocol")

        try:
            outcome = self._authorization.begin({
                "key": _KEY,
                "signal": attempt.controller,
                "interaction": {"notify": lambda _notice: None, "prompt": decline},
            })
            phase = "succeeded" if outcome.get("status") == "authorized" else "cancelled"
            self._finish_attempt(attempt, phase)
        except BaseException as error:  # noqa: BLE001 - terminal attempt outcome
            code = getattr(error, "code", None)
            phase = "expired" if code == "expired" else "failed"
            self._finish_attempt(attempt, phase, code if code in SIGN_IN_ERROR_CODES else "protocol")

    def _finish_attempt(self, attempt: _Attempt, phase: str,
                        error_code: str | None = None) -> None:
        with self._lock:
            attempt.view = {
                **attempt.view,
                "id": attempt.id,
                "phase": phase,
                **({"errorCode": error_code} if error_code is not None else {}),
            }
            attempt.view.pop("authorizeUrl", None)
        with attempt.callback_lock:
            if phase == "succeeded" and attempt.completion_url is not None:
                attempt.callback_status = 302
                attempt.callback_headers = {
                    "location": attempt.completion_url, "cache-control": "no-store"}
                attempt.callback_body = b""
            elif phase == "failed" and attempt.login_source == "web":
                nonce = secrets.token_urlsafe(16)
                message = ("登录失败，请关闭此标签页并在原页面重试。"
                           if attempt.locale == "zh_CN" else
                           "Sign-in failed. Close this tab and try again in the original tab.")
                lang = "zh-CN" if attempt.locale == "zh_CN" else "en"
                attempt.callback_headers = {
                    "content-type": "text/html; charset=utf-8",
                    "cache-control": "no-store",
                    "content-security-policy":
                        f"default-src 'none'; script-src 'nonce-{nonce}'; "
                        "frame-ancestors 'none'",
                    "referrer-policy": "no-referrer",
                }
                attempt.callback_status = 200
                attempt.callback_body = (
                    f'<!doctype html><html lang="{lang}"><meta charset="utf-8">'
                    f"<title>{message}</title><body><p>{message}</p>"
                    f'<script nonce="{nonce}">window.close()</script></body></html>'
                ).encode("utf-8")
            else:
                attempt.callback_status = 204
                attempt.callback_headers = {"cache-control": "no-store"}
                attempt.callback_body = b""
            attempt.callback_event.set()

    def _run_flow(self, session) -> None:
        attempt = self._attempt
        if attempt is None:
            raise PlatformAuthError("protocol")
        verifier = _token(32)
        state = _token(32)
        attempt.oauth_state = state
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        authorize_id: str | None = None
        deadline = time.monotonic() + self._attempt_timeout / 1000.0
        try:
            init = request_platform(
                self._origin, "auth_init",
                {"code_challenge": challenge, "code_challenge_method": "S256",
                 "state": state, "redirect_uri": f"{attempt.origin}{OAUTH_CALLBACK_PATH}",
                 "locale": attempt.locale, "login_source": attempt.login_source},
                self._request_timeout,
                {**self._request_headers, **attempt.client_headers})
            authorize_url = browser_url(init.get("authorize_url"), self._origin,
                                        "/dsh/authorize", self._rewrite_browser_origin)
            authorize_id = init.get("authorize_id")
            expires_in = init.get("expires_in")
            if not isinstance(authorize_id, str) or not authorize_id \
                    or not isinstance(expires_in, (int, float)) or expires_in <= 0:
                raise PlatformAuthError("protocol")
            expires_at = min(deadline, time.monotonic() + float(expires_in))
            self._set_phase(attempt, "waiting-browser", authorizeUrl=authorize_url,
                            expiresAt=int(expires_at * 1000))
            received_code = self._wait_for_code(attempt, session.signal, expires_at)
            self._set_phase(attempt, "exchanging")
            device_id = self._device_id()
            result = request_platform(
                self._origin, "auth_exchange",
                {"code": received_code, "code_verifier": verifier,
                 "redirect_uri": f"{attempt.origin}{OAUTH_CALLBACK_PATH}",
                 "device_id": device_id,
                 "device_model": f"{_platform_name()}-{_machine()}",
                 "os_version": _device_os_version()},
                self._request_timeout,
                {**self._request_headers, **attempt.client_headers})
            token = result.get("token")
            if not isinstance(token, str) or _TOKEN.fullmatch(token) is None:
                raise PlatformAuthError("protocol")
            completion = urlsplit(browser_url(
                result.get("authorized_url"), self._origin, "/dsh/authorized",
                self._rewrite_browser_origin))
            query = parse_qs(completion.query, keep_blank_values=True)
            query["login_source"] = [attempt.login_source]
            attempt.completion_url = urlunsplit((
                completion.scheme, completion.netloc, completion.path,
                urlencode(query, doseq=True), ""))
            if time.monotonic() >= expires_at or session.signal.aborted:
                raise PlatformAuthError("expired")
            user = result.get("user")
            if user is not None:
                try:
                    attempt.initial_profile = (token, {
                        "status": "ready", "value": profile(user)})
                except PlatformAuthError:
                    attempt.initial_profile = None
            self._set_phase(attempt, "committing")
            try:
                session.commit({"kind": "grant", "payload": {
                    "version": 1, "token": token, "issuer": self._origin}})
            except BaseException as error:
                raise PlatformAuthError("storage") from error
        except PlatformAuthError as error:
            if error.code == "expired" and authorize_id is not None:
                self._cancel_request(authorize_id, verifier, attempt.client_headers)
                authorize_id = None
            raise
        except BaseException as error:
            raise PlatformAuthError("protocol") from error
        finally:
            if session.signal.aborted and authorize_id is not None:
                self._cancel_request(authorize_id, verifier, attempt.client_headers)

    def _wait_for_code(self, attempt: _Attempt, signal: AbortSignal,
                       expires_at: float) -> str:
        while True:
            if signal.aborted:
                raise PlatformAuthError("expired")
            remaining = expires_at - time.monotonic()
            if remaining <= 0:
                raise PlatformAuthError("expired")
            with self._lock:
                if attempt.received_code is not None:
                    return attempt.received_code
            time.sleep(min(0.05, remaining))

    def _device_id(self) -> str:
        credentials = self.ctx.get("credentials")

        def create(current):
            if current is not None:
                return None
            return {"kind": "grant", "payload": {"id": str(uuid.uuid4())}}

        record = credentials.modify_record(_DEVICE, create)
        value = (record.get("payload") or {}).get("id") if record is not None else None
        if not isinstance(value, str):
            raise PlatformAuthError("storage")
        return value

    def _cancel_request(self, authorize_id: str, verifier: str,
                        client_headers: dict[str, str]) -> None:
        def cancel() -> None:
            try:
                request_platform(self._origin, "auth_cancel",
                                 {"authorize_id": authorize_id, "code_verifier": verifier},
                                 self._request_timeout,
                                 {**self._request_headers, **client_headers})
            except PlatformAuthError:
                pass

        run_in_background(cancel, self._revocations)

    def _detail_headers(self, client: dict) -> dict[str, str]:
        return {**self._account_headers, **platform_client_headers(
            self._desktop_platform, client)}

    def _get_detail(self, field: str, headers: dict[str, str], lifetime: int,
                    stored: dict) -> dict | None:
        if lifetime != self._details_generation:
            return None
        if (field == "profile" and self._attempt is not None
                and self._attempt.initial_profile is not None
                and self._attempt.initial_profile[0] == stored["token"]):
            initial = self._attempt.initial_profile[1]
            self._attempt.initial_profile = None
            return initial
        try:
            timeout = self._balance_timeout if field == "balance" else self._request_timeout
            detail = read_account_detail(field, self._origin, stored["token"],
                                         timeout, headers)
        except AccountUnauthorizedError:
            if lifetime == self._details_generation:
                self._expire_credential(stored["token"], lifetime)
            return None
        if lifetime != self._details_generation:
            return None
        return detail

    def _current_account_id(self, lifetime: int, stored: dict,
                            headers: dict[str, str]) -> str | None:
        cached = self._last_profile
        if cached is not None and cached[0] == stored["token"]:
            return _identity_of(cached[1])
        detail = self._get_detail("profile", headers, lifetime, stored)
        if detail is None:
            return None
        if detail.get("status") == "failed":
            raise PlatformAuthError("network")
        self._cache_profile(stored["token"], detail)
        return _identity_of(detail)

    def _cache_profile(self, token: str, detail: dict) -> None:
        if detail.get("status") != "ready":
            return
        self._last_profile = (token, detail)

    def _record_updated(self, key: str) -> None:
        if key != _KEY:
            return
        self._last_profile = None
        self._details_generation += 1

    def _expire_credential(self, token: str, lifetime: int) -> None:
        with self._removal_lock:
            self._removing = True
            try:
                self._expire_credential_locked(token, lifetime)
            finally:
                self._removing = False

    def _expire_credential_locked(self, token: str, lifetime: int) -> None:
        with self._lock:
            if self._closed or lifetime != self._details_generation:
                return
            active = self._attempt
        if active is not None:
            self.cancel_sign_in(active.id)
        with self._lock:
            if self._closed or lifetime != self._details_generation:
                return
            record = self.ctx.get("credentials").read_record(_KEY)
            if record is None or record.get("kind") != "grant":
                return
            payload = record.get("payload") or {}
            if payload.get("token") != token or payload.get("issuer") != self._origin:
                return
            self.ctx.get("credentials").delete_record(_KEY)
            self.ctx.emit("deepseek-account/session-expired")
            self._attempt = None
            self.ctx.emit("deepseek-account/signed-out")

    def _read_current_grant(self, lifetime: int) -> dict | None:
        if self._closed:
            return None
        record = self.ctx.get("credentials").read_record(_KEY)
        if record is None or lifetime != self._details_generation:
            return None
        if record.get("kind") != "grant" or not self._valid_grant(record.get("payload")):
            raise PlatformAuthError("storage")
        payload = record["payload"]
        if payload["issuer"] != self._origin:
            raise PlatformAuthError("protocol")
        return payload

    def _valid_grant(self, payload) -> bool:
        return (isinstance(payload, dict) and payload.get("version") == 1
                and isinstance(payload.get("token"), str) and bool(payload["token"])
                and isinstance(payload.get("issuer"), str))

    def _revoke(self, token: str, headers: dict[str, str]) -> None:
        if self._closed:
            return

        def revoke() -> None:
            revoke_account(self._origin, token, self._logout_max_retries,
                           self._logout_delay, self._request_timeout,
                           {**self._request_headers, **headers},
                           cancelled=lambda: self._closed)

        run_in_background(revoke, self._revocations)

    def _set_phase(self, attempt: _Attempt, phase: str, **fields) -> None:
        with self._lock:
            attempt.view = {"id": attempt.id, "phase": phase, **fields}


def _identity_of(detail: dict) -> str:
    if detail.get("status") != "ready":
        raise PlatformAuthError("protocol")
    value = detail.get("value") or {}
    account_id = value.get("id")
    if not isinstance(account_id, str) or not account_id:
        raise PlatformAuthError("protocol")
    return account_id


def _token(size: int) -> str:
    return base64.urlsafe_b64encode(secrets.token_bytes(size)).rstrip(b"=").decode("ascii")


def _platform_name() -> str:
    import platform as platform_module
    return platform_module.system().lower()


def _machine() -> str:
    import platform as platform_module
    return platform_module.machine() or "unknown"


def _device_os_version() -> str:
    import platform as platform_module
    return f"{platform_module.system()} {platform_module.release()}"


def run_in_background(target, registry: set[threading.Thread]) -> None:
    def run() -> None:
        try:
            target()
        finally:
            registry.discard(threading.current_thread())

    thread = threading.Thread(target=run, daemon=True)
    registry.add(thread)
    thread.start()
