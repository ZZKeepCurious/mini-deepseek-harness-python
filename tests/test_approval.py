"""第 13 章测试：审批 —— 策略两档 + 审计事件对。"""
import unittest

from miniharness.interaction.approval import (
    APPROVAL_OUTCOMES,
    APPROVAL_POLICIES,
    ApprovalService,
    effective_approval_policy,
    has_open_turn,
    set_approval_policy,
)
from miniharness.core.scope import Context
from miniharness.core.session import Session


def open_turn(session: Session) -> None:
    session.append("turn/start", {"turn": 1})


class TestApprovalService(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        self.service = ApprovalService(self.ctx)
        self.session = Session("approval-test")

    def test_ask_with_answerer_allowed_once_and_audit_pair(self):
        open_turn(self.session)
        self.ctx.on("approval/request",
                    lambda req, next: "allowed-once")
        outcome = self.service.request(self.session, "bash", call_id="call_0",
                                       reason="需要执行")
        self.assertEqual(outcome, "allowed-once")
        audit = [e for e in self.session.events
                 if e["type"].startswith("approval/")]
        self.assertEqual([e["type"] for e in audit],
                         ["approval/asked", "approval/decided"])
        self.assertEqual(audit[0]["data"]["toolName"], "bash")
        self.assertEqual(audit[0]["data"]["callId"], "call_0")
        self.assertEqual(audit[0]["data"]["reason"], "需要执行")
        self.assertEqual(audit[0]["data"]["id"], audit[1]["data"]["id"])
        self.assertNotIn("surfaceOp", audit[0])   # log-only，非 surface

    def test_never_policy_rejects_without_dispatch(self):
        open_turn(self.session)
        set_approval_policy(self.session, "never")
        consulted = []
        self.ctx.on("approval/request",
                    lambda req, next: consulted.append(req) or "allowed-once")
        outcome = self.service.request(self.session, "bash")
        self.assertEqual(outcome, "rejected")
        self.assertEqual(consulted, [])   # 'never' 在派发前决定，answerer 不被咨询

    def test_ask_without_answerer_fails_closed(self):
        open_turn(self.session)
        outcome = self.service.request(self.session, "bash")
        self.assertEqual(outcome, "unavailable")

    def test_throwing_answerer_fails_closed(self):
        open_turn(self.session)
        self.ctx.on("approval/request", lambda req, next: (_ for _ in ()).throw(
            RuntimeError("answerer 故障")))
        outcome = self.service.request(self.session, "bash")
        self.assertEqual(outcome, "unavailable")

    def test_rogue_answer_normalized_to_unavailable(self):
        open_turn(self.session)
        self.ctx.on("approval/request", lambda req, next: "yolo")
        outcome = self.service.request(self.session, "bash")
        self.assertEqual(outcome, "unavailable")

    def test_request_outside_open_turn_rejects_without_log(self):
        self.session.append("turn/start", {"turn": 1})
        self.session.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
        with self.assertRaises(RuntimeError):
            self.service.request(self.session, "bash")
        self.assertEqual([e for e in self.session.events if "approval/" in e["type"]], [])

    def test_aborted_signal_cancelled_and_still_audited(self):
        open_turn(self.session)

        class Signal:
            aborted = True

        outcome = self.service.request(self.session, "bash", signal=Signal())
        self.assertEqual(outcome, "cancelled")
        decided = [e for e in self.session.events
                   if e["type"] == "approval/decided"][0]
        self.assertEqual(decided["data"]["outcome"], "cancelled")

    def test_allowed_once_grants_only_the_requested_action(self):
        open_turn(self.session)
        self.ctx.on("approval/request", lambda req, next: "allowed-once")
        # 两次独立 ask 各走一次审计对：无跨调用豁免
        for _ in range(2):
            self.assertEqual(self.service.request(self.session, "bash"), "allowed-once")
        asked = [e for e in self.session.events if e["type"] == "approval/asked"]
        self.assertEqual(len(asked), 2)

    def test_waterfall_answerer_can_shortcircuit(self):
        open_turn(self.session)
        seen = []
        self.ctx.on("approval/request",
                    lambda req, next: seen.append("first") or "rejected")   # 不调 next 即短路
        self.ctx.on("approval/request",
                    lambda req, next: seen.append("second") or "allowed-once")
        outcome = self.service.request(self.session, "bash")
        self.assertEqual(outcome, "rejected")
        self.assertEqual(seen, ["first"])   # 短路后第二监听器不执行


class TestApprovalPolicy(unittest.TestCase):
    def test_effective_fold_no_events(self):
        self.assertIsNone(effective_approval_policy([]))

    def test_effective_fold_last_wins(self):
        session = Session("policy")
        set_approval_policy(session, "never")
        set_approval_policy(session, "ask")
        self.assertEqual(effective_approval_policy(session.events), "ask")
        set_approval_policy(session, "never")
        self.assertEqual(effective_approval_policy(session.events), "never")

    def test_set_invalid_policy_throws_before_log(self):
        session = Session("policy")
        with self.assertRaises(TypeError):
            set_approval_policy(session, "sometimes")
        self.assertEqual([e for e in session.events if e["type"] == "approval/policy"], [])

    def test_policy_switch_is_replayable_state(self):
        session = Session("policy")
        set_approval_policy(session, "never")
        # "恢复"：从日志重放即状态，无需追赶机制
        self.assertEqual(effective_approval_policy(session.events), "never")

    def test_policy_event_is_log_only_not_surface(self):
        session = Session("policy")
        set_approval_policy(session, "never")
        from miniharness.core.session import derive_messages
        self.assertEqual(derive_messages(session.events), [])   # 不产生模型消息

    def test_has_open_turn(self):
        session = Session("t")
        self.assertFalse(has_open_turn(session.events))
        session.append("turn/start", {"turn": 1})
        self.assertTrue(has_open_turn(session.events))
        session.append("turn/end", {"turn": 1, "reason": {"kind": "completed"}})
        self.assertFalse(has_open_turn(session.events))

    def test_vocabularies(self):
        self.assertEqual(APPROVAL_POLICIES, ("ask", "never"))
        self.assertEqual(APPROVAL_OUTCOMES,
                         ("allowed-once", "rejected", "cancelled", "unavailable"))

    def test_service_effective_policy_fallback(self):
        session = Session("t")
        service = ApprovalService(Context(name="root"), policy="never")
        self.assertEqual(service.effective_policy(session), "never")
        set_approval_policy(session, "ask")
        self.assertEqual(service.effective_policy(session), "ask")   # 覆盖优先

    def test_service_invalid_config_rejected(self):
        with self.assertRaises(TypeError):
            ApprovalService(Context(name="root"), policy="sometimes")


if __name__ == "__main__":
    unittest.main()