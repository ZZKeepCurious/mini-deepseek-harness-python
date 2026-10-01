"""deepseek-account + account-controller（P1-21）验收。

对齐上游 `packages/credentials/deepseek-account`（Service Definition）+ 
`packages/api/account-controller`（Remote namespace `account/*`）。

载体差异（登记）：浏览器 PKCE + Platform HTTP 是浏览器宿主载体——mini 以
本地落空实现（恒 signed-out）承载服务面；startSignIn 因无浏览器 fail loud。
"""
import asyncio
import unittest

from miniharness.core.scope import Context
from miniharness.deepseek_account import (
    LocalAccountService,
    SIGN_IN_ERROR_CODES,
    install_deepseek_account,
)
from miniharness.llm import FakeLlmAdapter
from miniharness.web.api import WebApi


class DeepSeekAccountServiceTest(unittest.TestCase):
    def test_error_codes(self):
        self.assertEqual(SIGN_IN_ERROR_CODES, ("network", "protocol", "expired", "storage"))

    def test_get_state_signed_out(self):
        ctx = Context(name="acct")
        try:
            svc = install_deepseek_account(ctx)
            state = svc.get_state()
            self.assertEqual(state["status"], "signed-out")
            self.assertEqual(state["attempt"], None)
            self.assertIn("links", state)
        finally:
            ctx.dispose()

    def test_get_profile_balance_null(self):
        ctx = Context(name="acct")
        try:
            svc = install_deepseek_account(ctx)
            self.assertIsNone(svc.get_profile())
            self.assertIsNone(svc.get_balance())
        finally:
            ctx.dispose()

    def test_start_sign_in_fails_loud(self):
        ctx = Context(name="acct")
        try:
            svc = install_deepseek_account(ctx)
            with self.assertRaises(RuntimeError):
                svc.start_sign_in("en", "http://127.0.0.1:3080", "web")
        finally:
            ctx.dispose()

    def test_sign_out_signed_out(self):
        ctx = Context(name="acct")
        try:
            svc = install_deepseek_account(ctx)
            state = svc.sign_out()
            self.assertEqual(state["status"], "signed-out")
        finally:
            ctx.dispose()

    def test_install_idempotent(self):
        ctx = Context(name="acct")
        try:
            svc = install_deepseek_account(ctx)
            self.assertIs(install_deepseek_account(ctx), svc)
        finally:
            ctx.dispose()


_CLIENT = {"version": "1.0.0", "locale": "en-US", "timezoneOffsetSeconds": 0}


class AccountControllerWireTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="acct-web")
        install_deepseek_account(self.ctx)
        self.api = WebApi(self.ctx, FakeLlmAdapter())

    def tearDown(self):
        self.ctx.dispose()

    def _value(self, response):
        self.assertTrue(response["result"]["ok"], response["result"].get("error"))
        return response["result"]["value"]

    def _error(self, response):
        self.assertFalse(response["result"]["ok"])
        return response["result"]["error"]

    def test_get_state_route(self):
        value = self._value(self.api.dispatch("account/getState", "r1", {}))
        self.assertEqual(value["status"], "signed-out")

    def test_get_profile_route(self):
        value = self._value(self.api.dispatch(
            "account/getProfile", "r2", {"client": _CLIENT}))
        self.assertIsNone(value)

    def test_get_balance_route(self):
        value = self._value(self.api.dispatch(
            "account/getBalance", "r3", {"client": _CLIENT}))
        self.assertIsNone(value)

    def test_get_unnotified_bonuses_route(self):
        value = self._value(self.api.dispatch(
            "account/getUnnotifiedBonuses", "r3b", {"client": _CLIENT}))
        self.assertIsNone(value)

    def test_ack_bonus_notified_route(self):
        value = self._value(self.api.dispatch(
            "account/ackBonusNotified", "r3c",
            {"accountId": "u1", "orderId": "o1", "client": _CLIENT}))
        self.assertFalse(value)

    def test_has_running_account_tasks_route(self):
        value = self._value(self.api.dispatch(
            "account/hasRunningAccountTasks", "r3d", {}))
        self.assertFalse(value)

    def test_sign_out_route(self):
        value = self._value(self.api.dispatch(
            "account/signOut", "r4", {"client": _CLIENT}))
        self.assertEqual(value["status"], "signed-out")

    def test_client_metadata_required(self):
        error = self._error(self.api.dispatch("account/getProfile", "r4b", {}))
        self.assertEqual(error["code"], "gateway/arguments-invalid")

    def test_malformed_client_metadata_rejected(self):
        error = self._error(self.api.dispatch(
            "account/getProfile", "r4c", {"client": {"version": "1"}}))
        self.assertEqual(error["code"], "gateway/bad-request")

    def test_account_not_mounted(self):
        from miniharness.core.scope import Context as Ctx
        ctx2 = Ctx(name="bare-acct")
        try:
            api = WebApi(ctx2, FakeLlmAdapter())
            response = api.dispatch("account/getState", "r5", {})
            self.assertFalse(response["result"]["ok"])
            self.assertEqual(response["result"]["error"]["code"],
                             "gateway/invocation-unavailable")
        finally:
            ctx2.dispose()


class AccountWatchExpiryTest(unittest.TestCase):
    def test_watch_expiry_emits_on_session_expired(self):
        """`account/watchExpiry`：无重放，订阅期间的 session-expired 产出一帧。"""
        async def go():
            ctx = Context(name="acct-watch")
            try:
                service = install_deepseek_account(ctx, token="tok")
                api = WebApi(ctx, FakeLlmAdapter())
                gen = api.gateway.open_stream("account/watchExpiry", {"args": {}})
                pending = asyncio.ensure_future(gen.__anext__())
                await asyncio.sleep(0.05)
                service.reject_token("tok")   # 发 deepseek-account/session-expired
                frame = await asyncio.wait_for(pending, timeout=1.0)
                self.assertEqual(frame, "session-expired")
                await gen.aclose()
            finally:
                ctx.dispose()
        asyncio.run(go())


if __name__ == "__main__":
    unittest.main()