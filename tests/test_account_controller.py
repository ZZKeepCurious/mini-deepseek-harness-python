"""deepseek-account + account-controller（P1-21）验收。

对齐上游 `packages/credentials/deepseek-account`（Service Definition）+ 
`packages/api/account-controller`（Remote namespace `account/*`）。

载体差异（登记）：浏览器 PKCE + Platform HTTP 是浏览器宿主载体——mini 以
本地落空实现（恒 signed-out）承载服务面；startSignIn 因无浏览器 fail loud。
"""
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


class AccountControllerWireTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="acct-web")
        install_deepseek_account(self.ctx)
        self.api = WebApi(self.ctx, FakeLlmAdapter())

    def tearDown(self):
        self.ctx.dispose()

    def test_get_state_route(self):
        response = self.api.dispatch("account/getState", "r1", {})
        self.assertTrue(response["result"]["ok"])
        self.assertEqual(response["result"]["value"]["status"], "signed-out")

    def test_get_profile_route(self):
        response = self.api.dispatch("account/getProfile", "r2", {})
        self.assertTrue(response["result"]["ok"])
        self.assertIsNone(response["result"]["value"])

    def test_get_balance_route(self):
        response = self.api.dispatch("account/getBalance", "r3", {})
        self.assertTrue(response["result"]["ok"])
        self.assertIsNone(response["result"]["value"])

    def test_sign_out_route(self):
        response = self.api.dispatch("account/signOut", "r4", {})
        self.assertTrue(response["result"]["ok"])
        self.assertEqual(response["result"]["value"]["status"], "signed-out")

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


if __name__ == "__main__":
    unittest.main()