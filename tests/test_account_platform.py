from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from miniharness.core.scope import Context
from miniharness.deepseek_account import install_deepseek_account
from miniharness.deepseek_account.protocol import (
    PlatformAuthError,
    browser_url,
    login_origin,
    platform_headers,
    platform_origin,
)
from miniharness.llm import FakeLlmAdapter
from miniharness.seams import install_authorization, install_credentials
from miniharness.seams.credentials_local import LocalCredentialProvider
from miniharness.web.api import WebApi
from miniharness.web.server import create_app

_KEY = "deepseek-account-platform/default"


def _client(locale: str = "en") -> dict:
    return {"version": "1.0.0", "locale": locale, "timezoneOffsetSeconds": 0}


class _Platform:
    def __init__(self) -> None:
        self.requests: list[dict] = []
        self.init_event = threading.Event()
        self.exchange_event = threading.Event()
        self.logout_event = threading.Event()
        self.cancelled_event = threading.Event()
        self.unauthorized = False
        self.profile_fail = False
        self.summary_fail = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.daemon_threads = True
        self.server.platform = self
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def origin(self) -> str:
        host, port = self.server.server_address
        return f"http://{host}:{port}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()

    def find(self, path: str) -> dict | None:
        return next((item for item in reversed(self.requests) if item["path"] == path), None)


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    @property
    def platform(self) -> _Platform:
        return self.server.platform

    def log_message(self, _format, *args) -> None:
        pass

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError):
            self.close_connection = True

    def do_GET(self) -> None:
        self._handle("GET")

    def do_POST(self) -> None:
        self._handle("POST")

    def _handle(self, method: str) -> None:
        parts = urlsplit(self.path)
        length = int(self.headers.get("content-length", "0"))
        raw = self.rfile.read(length) if length else b""
        body = json.loads(raw) if raw else None
        record = {"method": method, "path": parts.path, "query": parts.query,
                  "headers": dict(self.headers), "body": body}
        self.platform.requests.append(record)
        platform = self.platform
        if parts.path == "/auth-api/v0/dsh/auth_init":
            state = body["state"]
            self.platform.init_event.set()
            self._send({"code": 0, "data": {"biz_code": 0, "biz_data": {
                "authorize_url": f"{platform.origin}/dsh/authorize?state={state}&x=1",
                "authorize_id": "auth-1", "expires_in": 30}}})
            return
        if parts.path == "/auth-api/v0/dsh/auth_exchange":
            self.platform.exchange_event.set()
            self._send({"code": 0, "data": {"biz_code": 0, "biz_data": {
                "token": "tok-abc",
                "authorized_url": f"{platform.origin}/dsh/authorized?x=1",
                "user": {"id": "user-1", "email": "a@example.com",
                         "mobile": "", "id_profile": {"name": "Alice", "picture": None}}}}})
            return
        if parts.path == "/auth-api/v0/dsh/auth_cancel":
            self.platform.cancelled_event.set()
            self._send({"code": 0, "data": {"biz_code": 0, "biz_data": None}})
            return
        if parts.path == "/auth-api/v0/users/current":
            if platform.unauthorized:
                self._send({"code": 40003}, status=401)
                return
            if platform.profile_fail:
                self._send({"code": 500, "data": {"biz_code": 1, "biz_data": None}})
                return
            self._send({"code": 0, "data": {"biz_code": 0, "biz_data": {
                "id": "user-1", "email": "a@example.com", "mobile": "",
                "id_profile": {"name": "Alice", "picture": None}}}})
            return
        if parts.path == "/api/v0/users/get_user_summary":
            if platform.summary_fail:
                self._send({"code": 500, "data": {"biz_code": 1, "biz_data": None}})
                return
            self._send({"code": 0, "data": {"biz_code": 0, "biz_data": {
                "normal_wallets": [{"currency": "CNY", "balance": "12.50"}],
                "bonus_wallets": [{"currency": "CNY", "balance": "1"}]}}})
            return
        if parts.path == "/api/v0/users/get_unnotified_bonuses":
            self._send({"code": 0, "data": {"biz_code": 0, "biz_data": [{
                "order_id": "order-1", "msg": "bonus", "title": "Welcome",
                "expires_at": None, "campaign": {"id": "c"}, "amount": "1",
                "currency": "CNY", "granted_at": "2026-10-22"}]}})
            return
        if parts.path == "/api/v0/users/ack_bonus_notified":
            self._send({"code": 0, "data": {"biz_code": 0, "biz_data": None}})
            return
        if parts.path == "/auth-api/v0/users/logout":
            self.platform.logout_event.set()
            self._send({"code": 0, "data": {"biz_code": 0, "biz_data": None}})
            return
        self._send({"code": 404}, status=404)

    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProtocolTest(unittest.TestCase):
    def test_origin_normalization_and_loopback_policy(self) -> None:
        self.assertEqual(
            platform_origin("https://Platform.DeepSeek.com:443/", False),
            "https://platform.deepseek.com")
        self.assertEqual(
            platform_origin("http://127.0.0.1:3080/", True),
            "http://127.0.0.1:3080")
        with self.assertRaises(ValueError):
            platform_origin("http://127.0.0.1:3080", False)
        with self.assertRaises(ValueError):
            platform_origin("https://platform.deepseek.com/path", False)

    def test_login_origin_and_browser_url_validation(self) -> None:
        self.assertEqual(login_origin("http://LOCALHOST:03080/"),
                         "http://localhost:3080")
        self.assertEqual(login_origin("http://[::1]:3080"),
                         "http://[::1]:3080")
        for value in ("https://localhost:3080", "http://localhost",
                      "http://localhost:0", "http://localhost:65536",
                      "http://user@localhost:3080"):
            with self.assertRaises(PlatformAuthError):
                login_origin(value)
        self.assertEqual(
            browser_url("https://platform.deepseek.com/authorize?state=x",
                        "https://platform.deepseek.com", "/authorize"),
            "https://platform.deepseek.com/authorize?state=x")
        self.assertEqual(
            browser_url("https://cdn.example/authorize?state=x",
                        "http://127.0.0.1:3080", "/authorize", True),
            "http://127.0.0.1:3080/authorize?state=x")
        with self.assertRaises(PlatformAuthError):
            browser_url("https://platform.deepseek.com/other",
                        "https://platform.deepseek.com", "/authorize")

    def test_headers_are_normalized_and_reserved_rejected(self) -> None:
        self.assertEqual(platform_headers({"X-Test": " value "}), {"x-test": "value"})
        with self.assertRaises(ValueError):
            platform_headers({"Host": "example.com"})
        with self.assertRaises(ValueError):
            platform_headers({"X-Test": "a", "x-test": "b"})


class PlatformAccountTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.platform = _Platform()
        self.ctx = Context(name="platform-account")
        install_credentials(self.ctx, LocalCredentialProvider(
            os.path.join(self.temp.name, "credentials.json"), read_env=False))
        install_authorization(self.ctx)
        self.account = install_deepseek_account(
            self.ctx, platform_origin=self.platform.origin,
            allow_loopback_http=True, inference_origin=self.platform.origin,
            attempt_timeout_ms=5_000, logout_retry_delay_ms=10)

    def tearDown(self) -> None:
        self.ctx.dispose()
        self.platform.close()
        self.temp.cleanup()

    def _start_and_wait(self) -> dict:
        self.account.start_sign_in(_client(), "http://127.0.0.1:3080", "web")
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            view = self.account.get_state()
            attempt = view.get("attempt")
            if attempt is not None and attempt.get("phase") == "waiting-browser":
                return attempt
            time.sleep(0.01)
        self.fail("sign-in did not reach waiting-browser")

    def test_complete_browser_flow(self) -> None:
        attempt = self._start_and_wait()
        init = self.platform.find("/auth-api/v0/dsh/auth_init")
        self.assertEqual(init["body"]["code_challenge_method"], "S256")
        self.assertEqual(init["body"]["redirect_uri"], "http://127.0.0.1:3080/oauth/callback")
        state = init["body"]["state"]
        self.assertEqual(attempt["authorizeUrl"],
                         f"{self.platform.origin}/dsh/authorize?state={state}&x=1")

        status, headers, body = self.account.handle_callback(
            "GET", "/oauth/callback", {"state": [state], "code": ["code-1"]})
        self.assertEqual((status, body), (302, b""))
        self.assertEqual(headers["location"],
                         f"{self.platform.origin}/dsh/authorized?x=1&login_source=web")
        self.assertTrue(self.platform.exchange_event.is_set())
        exchange = self.platform.find("/auth-api/v0/dsh/auth_exchange")
        self.assertEqual(exchange["body"]["code"], "code-1")
        self.assertTrue(exchange["body"]["device_id"])

        stored = self.ctx.get("credentials").read_record(_KEY)
        self.assertEqual(stored, {"kind": "grant", "payload": {
            "version": 1, "token": "tok-abc", "issuer": self.platform.origin}})
        view = self.account.get_state()
        self.assertEqual(view["status"], "credential-stored")
        self.assertEqual(view["attempt"]["phase"], "succeeded")
        self.assertEqual(view["links"]["usageUrl"], f"{self.platform.origin}/usage")

        profile = self.account.get_profile(_client())
        self.assertEqual(profile, {"status": "ready", "value": {
            "id": "user-1", "avatarUrl": None, "name": "Alice",
            "contact": "a@example.com"}})
        self.assertEqual(self.account.get_balance(_client()), {
            "status": "ready",
            "value": [{"currency": "CNY", "balance": "12.50"}],
            "bonusWallets": [{"currency": "CNY", "balance": "1"}]})
        self.assertEqual(self.account.get_unnotified_bonuses(_client()), {
            "accountId": "user-1", "bonuses": [{
                "orderId": "order-1", "expiresAt": "", "message": "bonus",
                "campaign": {"id": "c"}, "amount": "1", "currency": "CNY",
                "grantedAt": "2026-10-22", "title": "Welcome"}]})
        self.assertTrue(self.account.ack_bonus_notified(
            "user-1", "order-1", _client()))
        self.assertEqual(self.account.resolve_token(
            f"{self.platform.origin}/v1/chat"), "tok-abc")
        self.assertIsNone(self.account.resolve_token("https://api.deepseek.com/v1"))

        session = self.account.get_platform_session()
        self.assertEqual(session["userId"], "user-1")
        self.assertEqual(session["token"], "tok-abc")

        self.assertEqual(self.account.sign_out(_client())["status"], "signed-out")
        self.assertTrue(self.platform.logout_event.wait(3))
        self.assertIsNone(self.ctx.get("credentials").read_record(_KEY))
        self.assertEqual(
            self.account.handle_callback("GET", "/oauth/callback",
                                         {"state": [state], "code": ["late"]})[0], 410)

    def test_callback_route_completes_attempt(self) -> None:
        from fastapi.testclient import TestClient

        attempt = self._start_and_wait()
        state = self.platform.find("/auth-api/v0/dsh/auth_init")["body"]["state"]
        api = WebApi(self.ctx, FakeLlmAdapter())
        client = TestClient(create_app(api, api.gateway))
        try:
            response = client.get(
                f"/oauth/callback?state={state}&code=code-1",
                follow_redirects=False)
        finally:
            client.close()
        self.assertEqual(response.status_code, 302)
        self.assertEqual(response.headers["location"],
                         f"{self.platform.origin}/dsh/authorized?x=1&login_source=web")
        self.assertEqual(self.account.get_state()["status"], "credential-stored")
        self.assertEqual(attempt["phase"], "waiting-browser")

    def test_callback_rejects_invalid_state_and_method(self) -> None:
        self._start_and_wait()
        state = self.platform.find("/auth-api/v0/dsh/auth_init")["body"]["state"]
        for method, path, query in (
                ("GET", "/oauth/callback", {"state": ["wrong"], "code": ["code-1"]}),
                ("GET", "/oauth/callback", {"state": [state]}),
                ("POST", "/oauth/callback", {"state": [state], "code": ["code-1"]}),
                ("GET", "/other", {"state": [state], "code": ["code-1"]})):
            status, headers, body = self.account.handle_callback(method, path, query)
            self.assertEqual((status, headers, body), (400, {"cache-control": "no-store"}, b""))
        self.assertFalse(self.platform.exchange_event.is_set())
        status, _, _ = self.account.handle_callback(
            "GET", "/oauth/callback", {"state": [state], "code": ["code-1"]})
        self.assertEqual(status, 302)
        self.assertTrue(self.platform.exchange_event.is_set())

    def test_cancel_attempt(self) -> None:
        attempt = self._start_and_wait()
        view = self.account.cancel_sign_in(attempt["id"])
        self.assertEqual(view["attempt"]["phase"], "cancelled")
        self.assertTrue(self.platform.cancelled_event.wait(3))
        self.assertEqual(self.account.get_state()["status"], "signed-out")

    def test_start_is_idempotent_and_unknown_cancel_is_noop(self) -> None:
        attempt = self._start_and_wait()
        again = self.account.start_sign_in(
            _client(), "http://127.0.0.1:3080", "web")
        self.assertEqual(again["attempt"]["id"], attempt["id"])
        unchanged = self.account.cancel_sign_in("unknown")
        self.assertEqual(unchanged["attempt"]["id"], attempt["id"])
        self.assertEqual(unchanged["attempt"]["phase"], "waiting-browser")

    def test_unauthorized_profile_removes_grant(self) -> None:
        attempt = self._start_and_wait()
        state = self.platform.find("/auth-api/v0/dsh/auth_init")["body"]["state"]
        self.account.handle_callback(
            "GET", "/oauth/callback", {"state": [state], "code": ["code-1"]})
        self.assertEqual(self.account.get_profile(_client())["status"], "ready")
        self.platform.unauthorized = True
        self.assertIsNone(self.account.get_profile(_client()))
        self.assertEqual(self.account.get_state()["status"], "signed-out")
        self.assertIsNone(self.ctx.get("credentials").read_record(_KEY))

    def test_failed_profile_is_not_zero_balance(self) -> None:
        attempt = self._start_and_wait()
        state = self.platform.find("/auth-api/v0/dsh/auth_init")["body"]["state"]
        self.account.handle_callback(
            "GET", "/oauth/callback", {"state": [state], "code": ["code-1"]})
        self.platform.summary_fail = True
        self.assertEqual(self.account.get_balance(_client()), {"status": "failed"})
        self.assertEqual(self.ctx.get("credentials").read_record(_KEY)["payload"]["token"],
                         "tok-abc")


if __name__ == "__main__":
    unittest.main()
