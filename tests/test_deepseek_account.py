"""deepseek-account 凭据 seam 与事件（dsh-v0.2.0-rc.2 增补）。

对齐上游 `packages/credentials/deepseek-account/src/{index,types,account-tasks}.ts`：
抽象服务的 `resolveToken`/`rejectToken`、`deepseek-account/signed-out` /
`session-expired` / `model-sign-in-required` 事件，以及本地落空实现（恒
signed-out，可注入一个已存储 token 供宿主/测试使用）。
"""
import unittest

from miniharness.core.scope import Context
from miniharness.deepseek_account import (
    LocalAccountService,
    install_deepseek_account,
    is_running_account_task,
)


class _FakeAgent:
    def __init__(self, status="running", provider=None):
        self.status = status
        self.cancelled = []
        context = {"provider": provider, "model": "m"} if provider else None
        self.session = type("S", (), {"request_context": lambda _self: context})()

    def cancel(self, cause=None, keep_inbox=False):
        self.cancelled.append((cause, keep_inbox))


class LocalAccountTokenTest(unittest.TestCase):
    def _ctx(self):
        ctx = Context(name="acct-token")
        self.addCleanup(ctx.dispose)
        return ctx

    def test_default_is_signed_out_without_token(self):
        ctx = self._ctx()
        svc = install_deepseek_account(ctx)
        self.assertIsNone(svc.resolve_token("https://api.deepseek.com"))
        self.assertEqual(svc.get_state()["status"], "signed-out")

    def test_injected_token_resolves_and_reads_credential_stored(self):
        ctx = self._ctx()
        svc = LocalAccountService(ctx, token="acct-1")
        self.assertEqual(svc.resolve_token("https://api.deepseek.com"), "acct-1")
        self.assertEqual(svc.get_state()["status"], "credential-stored")

    def test_reject_matching_token_clears_and_emits_session_expired(self):
        ctx = self._ctx()
        svc = LocalAccountService(ctx, token="acct-1")
        events = []
        ctx.on("deepseek-account/session-expired", lambda _payload: events.append("expired"))
        svc.reject_token("acct-1")
        self.assertEqual(events, ["expired"])
        self.assertIsNone(svc.resolve_token("https://api.deepseek.com"))

    def test_reject_non_matching_token_is_silent(self):
        ctx = self._ctx()
        svc = LocalAccountService(ctx, token="acct-1")
        events = []
        ctx.on("deepseek-account/session-expired", lambda _payload: events.append("expired"))
        svc.reject_token("other")
        self.assertEqual(events, [])
        self.assertEqual(svc.resolve_token("https://api.deepseek.com"), "acct-1")

    def test_sign_out_clears_and_emits_signed_out(self):
        ctx = self._ctx()
        svc = LocalAccountService(ctx, token="acct-1")
        events = []
        ctx.on("deepseek-account/signed-out", lambda _payload: events.append("out"))
        state = svc.sign_out()
        self.assertEqual(events, ["out"])
        self.assertEqual(state["status"], "signed-out")


class ModelSignInRequiredTest(unittest.TestCase):
    def test_agent_error_with_sign_in_code_emits_guidance(self):
        ctx = Context(name="acct-guidance")
        self.addCleanup(ctx.dispose)
        install_deepseek_account(ctx)
        guidance = []
        ctx.on("deepseek-account/model-sign-in-required",
               lambda _payload: guidance.append(True))
        ctx.emit("agent/error", {"error": {"code": "ACCOUNT_SIGN_IN_REQUIRED"}})
        self.assertEqual(guidance, [True])

    def test_unrelated_agent_error_is_ignored(self):
        ctx = Context(name="acct-guidance-other")
        self.addCleanup(ctx.dispose)
        install_deepseek_account(ctx)
        guidance = []
        ctx.on("deepseek-account/model-sign-in-required",
               lambda _payload: guidance.append(True))
        ctx.emit("agent/error", {"error": {"code": "QUOTA"}})
        self.assertEqual(guidance, [])


class IsRunningAccountTaskTest(unittest.TestCase):
    def test_running_on_account_route(self):
        self.assertTrue(is_running_account_task(
            _FakeAgent("running", "deepseek-account")))

    def test_running_on_other_route(self):
        self.assertFalse(is_running_account_task(
            _FakeAgent("running", "deepseek-official")))

    def test_idle_not_running(self):
        self.assertFalse(is_running_account_task(
            _FakeAgent("idle", "deepseek-account")))

    def test_sign_out_cancels_running_account_agents(self):
        ctx = Context(name="acct-cancel")
        self.addCleanup(ctx.dispose)
        install_deepseek_account(ctx)
        account = _FakeAgent("running", "deepseek-account")
        other = _FakeAgent("running", "deepseek-official")
        ctx.provide("agents", type("R", (), {"list": lambda _self: [account, other]})())
        ctx.emit("deepseek-account/signed-out")
        self.assertEqual(account.cancelled,
                         [({"kind": "hook", "reason": "deepseek-account/signed-out"}, True)])
        self.assertEqual(other.cancelled, [])


if __name__ == "__main__":
    unittest.main()
