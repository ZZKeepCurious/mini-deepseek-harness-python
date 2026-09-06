"""P2-21 测试：`ctx.authorization` 凭据获取授权 seam（上游 authorization.spec.ts 语义对齐）。"""
import os
import tempfile
import unittest

from miniharness.commands import AbortSignal
from miniharness.core.scope import Context
from miniharness.seams import (
    ALREADY_IN_FLIGHT,
    DECLINED,
    DUPLICATE_FLOW,
    NO_FLOW,
    NOT_COMMITTED,
    UNKNOWN_METHOD,
    AuthorizationDeclinedError,
    AuthorizationError,
    install_authorization,
    install_credentials,
)
from miniharness.seams.credentials_local import LocalCredentialProvider

_KEY = "scope/rec"


def _make_server(tmp=None):
    root = tmp or os.path.join(tempfile.mkdtemp(), "credentials.json")
    ctx = Context(name="auth-test")
    install_credentials(ctx, LocalCredentialProvider(root, read_env=False))
    auth = install_authorization(ctx)
    return ctx, auth, root


def _commit_flow(creds, key=_KEY):
    """上游契约版 flow：在 run 内经 ctx.credentials 提交记录。"""
    def run(session):
        creds.modify_record(key, lambda current: {"kind": "grant", "payload": {"t": 1}})
    return {
        "key": key,
        "label": "Test credential",
        "methods": [{"id": "oauth", "label": "Sign in"}, {"id": "manual", "label": "Paste code"}],
        "run": run,
    }


class TestAuthorizationRegistry(unittest.TestCase):
    def setUp(self):
        self.ctx, self.auth, self.root = _make_server()
        self.creds = self.ctx.get("credentials")

    def test_register_flow_then_begin_authorized(self):
        self.auth.registerFlow(_commit_flow(self.creds))
        request = {"key": _KEY, "interaction": None}
        outcome = self.auth.begin(request)
        self.assertEqual(outcome, {"status": "authorized"})

    def test_duplicate_flow_rejected(self):
        self.auth.registerFlow(_commit_flow(self.creds))
        with self.assertRaises(AuthorizationError) as cm:
            self.auth.registerFlow(_commit_flow(self.creds))
        self.assertEqual(cm.exception.code, DUPLICATE_FLOW)

    def test_disposer_removes_flow(self):
        dispose = self.auth.registerFlow(_commit_flow(self.creds))
        self.assertIsNotNone(self.auth.describe(_KEY))
        dispose()
        self.assertIsNone(self.auth.describe(_KEY))
        with self.assertRaises(AuthorizationError) as cm:
            self.auth.begin({"key": _KEY, "interaction": None})
        self.assertEqual(cm.exception.code, NO_FLOW)

    def test_list_and_describe(self):
        self.auth.registerFlow(_commit_flow(self.creds))
        entries = self.auth.list()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["key"], _KEY)
        self.assertEqual(entries[0]["inFlight"], False)
        self.assertEqual(len(entries[0]["methods"]), 2)
        self.assertEqual(self.auth.describe(_KEY)["label"], "Test credential")
        self.assertIsNone(self.auth.describe("other/scope"))

    def test_unregistered_begin_no_flow(self):
        with self.assertRaises(AuthorizationError) as cm:
            self.auth.begin({"key": _KEY, "interaction": None})
        self.assertEqual(cm.exception.code, NO_FLOW)

    def test_unknown_method(self):
        self.auth.registerFlow(_commit_flow(self.creds))
        with self.assertRaises(AuthorizationError) as cm:
            self.auth.begin({"key": _KEY, "method": "nope", "interaction": None})
        self.assertEqual(cm.exception.code, UNKNOWN_METHOD)

    def test_already_in_flight(self):
        self.auth.registerFlow(_commit_flow(self.creds))
        # 手动占槽：flow 为同步不阻塞，用直接注入 running 表模拟并发 attempt
        self.auth._running[_KEY] = {"controller": AbortSignal()}
        with self.assertRaises(AuthorizationError) as cm:
            self.auth.begin({"key": _KEY, "interaction": None})
        self.assertEqual(cm.exception.code, ALREADY_IN_FLIGHT)

    def test_preaborted_signal_cancelled_without_running(self):
        ran = []
        flow = {**_commit_flow(self.creds), "run": lambda session: ran.append(1)}
        self.auth.registerFlow(flow)
        sig = AbortSignal()
        sig.abort("nobody home")
        outcome = self.auth.begin({"key": _KEY, "interaction": None, "signal": sig})
        self.assertEqual(outcome, {"status": "cancelled"})
        self.assertEqual(ran, [])

    def test_not_committed_when_flow_writes_nothing(self):
        self.auth.registerFlow(_commit_flow(self.creds))
        # 不用 _commit_flow 的 run（它会写记录）：换一个什么都不写的 run
        self.auth._flows[_KEY] = {
            **_commit_flow(self.creds),
            "run": lambda session: None,
        }
        with self.assertRaises(AuthorizationError) as cm:
            self.auth.begin({"key": _KEY, "interaction": None})
        self.assertEqual(cm.exception.code, NOT_COMMITTED)

    def test_not_committed_when_flow_deletes_after_committing(self):
        def run(session):
            creds = self.ctx.get("credentials")
            creds.modify_record(_KEY, lambda c: {"kind": "grant", "payload": {"t": 1}})
            creds.delete_record(_KEY)

        self.auth.registerFlow({**_commit_flow(self.creds), "run": run})
        with self.assertRaises(AuthorizationError) as cm:
            self.auth.begin({"key": _KEY, "interaction": None})
        self.assertEqual(cm.exception.code, NOT_COMMITTED)

    def test_cancel_aborts_running_attempt(self):
        flow = {**_commit_flow(self.creds), "run": lambda s: s.signal.abort()}
        self.auth.registerFlow(flow)
        outcome = self.auth.begin({"key": _KEY, "interaction": None})
        self.assertEqual(outcome, {"status": "cancelled"})

    def test_declined_prompt_settles_cancelled(self):
        def run(session):
            try:
                session.prompt({"kind": "text", "message": "ok?"})
            except AuthorizationDeclinedError:
                return
        flow = {**_commit_flow(self.creds), "run": run}
        self.auth.registerFlow(flow)

        def interaction_prompt(prompt):
            raise AuthorizationDeclinedError()

        outcome = self.auth.begin({"key": _KEY, "interaction": {"notify": lambda n: None,
                                                               "prompt": interaction_prompt}})
        self.assertEqual(outcome, {"status": "cancelled"})

    def test_settled_events_fire_for_every_outcome(self):
        settled = []
        self.ctx.on("authorization/settled", lambda payload: settled.append(payload))
        # authorized
        dispose_ok = self.auth.registerFlow(_commit_flow(self.creds))
        self.auth.begin({"key": _KEY, "interaction": None})
        self.assertIn((_KEY, "authorized"), settled)
        dispose_ok()
        # failed
        self.auth.registerFlow({**_commit_flow(self.creds), "run": lambda s: (_ for _ in ()).throw(ValueError("boom"))})
        with self.assertRaises(ValueError):
            self.auth.begin({"key": _KEY, "interaction": None})
        self.assertIn((_KEY, "failed"), settled)
        # cancelled（attempt 内撤销才走 settle；提前 abort 的请求上游直接返回不占槽）
        dispose_fail = self.auth._flows.pop(_KEY)
        self.auth.registerFlow({**_commit_flow(self.creds), "run": lambda s: s.signal.abort()})
        self.auth.begin({"key": _KEY, "interaction": None})
        self.assertIn((_KEY, "cancelled"), settled)

    def test_settled_listener_failure_contained(self):
        def bad_listener(payload):
            raise RuntimeError("watcher broke")

        def good_listener(payload):
            self.good_seen = payload

        self.ctx.on("authorization/settled", bad_listener)
        self.ctx.on("authorization/settled", good_listener)
        self.auth.registerFlow(_commit_flow(self.creds))
        outcome = self.auth.begin({"key": _KEY, "interaction": None})
        self.assertEqual(outcome, {"status": "authorized"})
        self.assertEqual(self.good_seen, (_KEY, "authorized"))

    def test_credentials_service_emits_record_updated(self):
        seen = []
        self.ctx.on("credentials/record-updated", lambda key: seen.append(key))
        self.creds.modify_record(_KEY, lambda current: {"kind": "grant", "payload": {"t": 1}})
        self.assertEqual(seen, [_KEY])
        self.creds.delete_record(_KEY)
        self.assertEqual(seen, [_KEY, _KEY])


if __name__ == "__main__":
    unittest.main()