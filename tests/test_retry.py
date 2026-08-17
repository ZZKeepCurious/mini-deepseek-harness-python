"""阶段 4：重试/退避 —— retry-policy 解析、llm-retry 恢复、loop 接线。

上游对照：packages/llm/llm/src/retry-policy.ts + packages/llm/llm-retry/src/index.ts
+ packages/core/agent/src/runtime-types.ts（agent/request-error）。
"""
import asyncio
import email.utils
import random
import unittest
from datetime import datetime, timedelta, timezone

from miniharness.core.scope import Context
from miniharness.llm import (
    AUTH,
    CONTEXT_WINDOW_EXCEEDED,
    EMPTY_RESPONSE,
    LlmAdapter,
    LlmFailure,
    RATE_LIMIT,
    provider_retry_after_ms,
    request_id,
)
from miniharness.llm.retry import (
    apply_retry_planner,
    cancellable_delay,
    local_delay,
    recover_llm_failure,
    retry_policy_key,
)
from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.llm.retry_policy import (
    DEFAULT_INITIAL_DELAY_MS,
    DEFAULT_MAX_DELAY_MS,
    DEFAULT_MAX_RETRIES,
    DEFAULT_RETRYABLE_CODES,
    MAX_TIMER_DELAY_MS,
    resolve_retry_policy,
)
from miniharness.core.session import Session
from miniharness.core.tools import ToolRegistry


# ---------- retry-policy 解析 ----------

class ResolveRetryPolicyTest(unittest.TestCase):
    def test_defaults_to_normal(self):
        p = resolve_retry_policy()
        self.assertEqual(p["mode"], "normal")
        self.assertEqual(p["maxRetries"], DEFAULT_MAX_RETRIES)
        self.assertEqual(p["initialDelayMs"], DEFAULT_INITIAL_DELAY_MS)
        self.assertEqual(p["maxDelayMs"], DEFAULT_MAX_DELAY_MS)
        self.assertEqual(p["jitterRatio"], 0.1)
        self.assertEqual(p["retryableCodes"], DEFAULT_RETRYABLE_CODES)

    def test_normal_custom(self):
        p = resolve_retry_policy({
            "mode": "normal", "maxRetries": 5,
            "retryableCodes": ["RATE_LIMIT"],
            "backoff": {"initialDelayMs": 100, "maxDelayMs": 900, "jitterRatio": 0.5},
        })
        self.assertEqual(p["maxRetries"], 5)
        self.assertEqual(p["retryableCodes"], ("RATE_LIMIT",))
        self.assertEqual(p["initialDelayMs"], 100)
        self.assertEqual(p["jitterRatio"], 0.5)

    def test_always_mode(self):
        p = resolve_retry_policy({"mode": "always"})
        self.assertEqual(p["mode"], "always")
        self.assertNotIn("maxRetries", p)
        self.assertNotIn("retryableCodes", p)

    def test_unknown_key_rejected(self):
        with self.assertRaises(ValueError):
            resolve_retry_policy({"mode": "normal", "nope": 1})
        with self.assertRaises(ValueError):
            resolve_retry_policy({"mode": "always", "maxRetries": 2})

    def test_bad_mode(self):
        with self.assertRaises(ValueError):
            resolve_retry_policy({"mode": "sometimes"})

    def test_backoff_validation(self):
        for bad in [
            {"initialDelayMs": 0}, {"initialDelayMs": -5},
            {"initialDelayMs": MAX_TIMER_DELAY_MS + 1},
            {"maxDelayMs": 0}, {"jitterRatio": -0.1}, {"jitterRatio": 1.5},
            {"initialDelayMs": 500, "maxDelayMs": 100},
        ]:
            with self.assertRaises(ValueError, msg=str(bad)):
                resolve_retry_policy({"mode": "normal", "backoff": bad})

    def test_max_retries_validation(self):
        for bad in [-1, 1.5, "two"]:
            with self.assertRaises(ValueError):
                resolve_retry_policy({"mode": "normal", "maxRetries": bad})

    def test_retryable_codes_validation(self):
        with self.assertRaises(ValueError):
            resolve_retry_policy({"mode": "normal", "retryableCodes": []})
        with self.assertRaises(ValueError):
            resolve_retry_policy({"mode": "normal", "retryableCodes": ["RATE_LIMIT", "RATE_LIMIT"]})
        with self.assertRaises(ValueError):
            resolve_retry_policy({"mode": "normal", "retryableCodes": ["RATE_LIMIT", ""]})

    def test_policy_key_stable_and_distinct(self):
        a = resolve_retry_policy()
        b = resolve_retry_policy()
        self.assertEqual(retry_policy_key(a), retry_policy_key(b))
        self.assertNotEqual(retry_policy_key(a), retry_policy_key(
            resolve_retry_policy({"mode": "normal", "maxRetries": 3})))
        self.assertNotEqual(retry_policy_key(a), retry_policy_key(
            resolve_retry_policy({"mode": "always"})))


# ---------- 退避计算 ----------

class LocalDelayTest(unittest.TestCase):
    def test_bounds_and_growth(self):
        policy = resolve_retry_policy({"mode": "normal",
                                       "backoff": {"initialDelayMs": 100, "maxDelayMs": 10000, "jitterRatio": 0}})
        d1 = local_delay(policy, 1, random.random)
        d2 = local_delay(policy, 2, random.random)
        d3 = local_delay(policy, 3, random.random)
        self.assertEqual(d1, 100)
        self.assertEqual(d2, 200)
        self.assertEqual(d3, 400)

    def test_caps_at_max(self):
        policy = resolve_retry_policy({"mode": "normal",
                                       "backoff": {"initialDelayMs": 100, "maxDelayMs": 150, "jitterRatio": 0}})
        self.assertEqual(local_delay(policy, 1, random.random), 100)
        for retry in (2, 10, 2000):
            self.assertEqual(local_delay(policy, retry, random.random), 150)

    def test_jitter_within_band(self):
        policy = resolve_retry_policy({"mode": "normal", "backoff": {"jitterRatio": 0.25}})
        for retry in (1, 2, 5):
            lo = int(500 * (2 ** min(retry - 1, 1024)) * 0.75)
            hi = int(min(500 * (2 ** min(retry - 1, 1024)), 10000) * 1.25)
            for _ in range(20):
                d = local_delay(policy, retry, random.random)
                self.assertGreaterEqual(d, min(lo, hi))
                self.assertLessEqual(d, min(hi, 10000))


# ---------- provider 失败 facts ----------

class ProviderFactsTest(unittest.TestCase):
    def test_retry_after_seconds(self):
        self.assertEqual(provider_retry_after_ms("2"), 2000)
        self.assertEqual(provider_retry_after_ms("0"), None)
        self.assertEqual(provider_retry_after_ms(""), None)
        self.assertEqual(provider_retry_after_ms("abc"), None)

    def test_retry_after_http_date(self):
        future = datetime.now(timezone.utc) + timedelta(seconds=5)
        value = email.utils.format_datetime(future)
        delay = provider_retry_after_ms(value)
        self.assertIsNotNone(delay)
        self.assertGreaterEqual(delay, 1000)
        self.assertLessEqual(delay, 9000)

    def test_request_id(self):
        self.assertEqual(request_id({"x-request-id": "r1"}), "r1")
        self.assertEqual(request_id({"x-deepseek-request-id": "r2"}), "r2")
        self.assertEqual(request_id({"x-request-id": "r1", "x-deepseek-request-id": "r3"}), "r1")
        self.assertEqual(request_id({"other": "x"}), None)


# ---------- recover 决策 ----------

def _session():
    return Session("retry-test")


def _recover(*args, **kwargs):
    """同步包装：recover_llm_failure 已 async 化（asyncio 化重构），
    单测在普通线程直接经一次性事件循环驱动。"""
    return asyncio.run(recover_llm_failure(*args, **kwargs))


class RecoverDecisionTest(unittest.TestCase):
    def setUp(self):
        self.session = _session()
        self.calls = 0
        self.policy = resolve_retry_policy({
            "mode": "normal", "maxRetries": 2,
            "backoff": {"initialDelayMs": 1, "maxDelayMs": 2, "jitterRatio": 0},
        })
        self.failure = LlmFailure(RATE_LIMIT, "too many", status=429,
                                  provider_retry_after_ms=None)

    def delegate(self):
        self.calls += 1
        return None

    def test_no_policy_delegates(self):
        result = _recover(self.session, 1, 1, "deepseek-official",
                                     self.failure, None, next_fn=self.delegate)
        self.assertIsNone(result)
        self.assertEqual(self.calls, 1)

    def test_non_retryable_code_delegates(self):
        failure = LlmFailure(AUTH, "bad key")
        result = _recover(self.session, 1, 1, "p", failure, self.policy,
                                     next_fn=self.delegate)
        self.assertIsNone(result)
        self.assertEqual(self.calls, 1)
        self.assertEqual([e["type"] for e in self.session.events], [])

    def test_first_retry_appends_events_and_returns_action(self):
        result = _recover(self.session, 1, 1, "p", self.failure, self.policy)
        self.assertEqual(result, {"kind": "retry"})
        types = [e["type"] for e in self.session.events]
        self.assertEqual(types, ["llm/retry", "llm/retry-started"])
        retry = self.session.events[0]["data"]
        self.assertEqual(retry["mode"], "normal")
        self.assertEqual(retry["retry"], 1)
        self.assertEqual(retry["maxRetries"], 2)
        self.assertEqual(retry["turn"], 1)
        self.assertEqual(retry["step"], 1)
        self.assertEqual(retry["provider"], "p")
        self.assertEqual(retry["failure"]["code"], RATE_LIMIT)
        self.assertEqual(retry["failure"]["message"], "too many")
        self.assertEqual(retry["failure"]["status"], 429)
        self.assertEqual(retry["delayMs"], 1)
        self.assertTrue(retry["retryId"].startswith("retry_"))
        started = self.session.events[1]["data"]
        self.assertEqual(started["retryId"], retry["retryId"])
        self.assertEqual(started["retry"], 1)

    def test_second_retry_reuses_retry_id_and_increments(self):
        _recover(self.session, 1, 1, "p", self.failure, self.policy)
        result = _recover(self.session, 1, 1, "p", self.failure, self.policy)
        self.assertEqual(result, {"kind": "retry"})
        first = self.session.events[0]["data"]
        second = self.session.events[2]["data"]
        self.assertEqual(second["retry"], 2)
        self.assertEqual(second["retryId"], first["retryId"])

    def test_max_retries_exhausted_delegates(self):
        small = resolve_retry_policy({
            "mode": "normal", "maxRetries": 1,
            "backoff": {"initialDelayMs": 1, "maxDelayMs": 2, "jitterRatio": 0},
        })
        self.assertEqual(_recover(self.session, 1, 1, "p", self.failure, small),
                         {"kind": "retry"})
        result = _recover(self.session, 1, 1, "p", self.failure, small,
                                     next_fn=self.delegate)
        self.assertIsNone(result)
        self.assertEqual(self.calls, 1)

    def test_other_step_does_not_count(self):
        _recover(self.session, 1, 1, "p", self.failure, self.policy)
        result = _recover(self.session, 1, 2, "p", self.failure, self.policy)
        self.assertEqual(result, {"kind": "retry"})
        self.assertEqual(self.session.events[2]["data"]["retry"], 1)

    def test_provider_retry_after_used(self):
        wide = resolve_retry_policy({
            "mode": "normal", "maxRetries": 2,
            "backoff": {"initialDelayMs": 1, "maxDelayMs": 5000, "jitterRatio": 0},
        })
        failure = LlmFailure(RATE_LIMIT, "slow down", provider_retry_after_ms=3000)
        result = _recover(self.session, 1, 1, "p", failure, wide)
        self.assertEqual(result, {"kind": "retry"})
        self.assertEqual(self.session.events[0]["data"]["delayMs"], 3000)

    def test_provider_retry_after_over_max_normal_delegates(self):
        failure = LlmFailure(RATE_LIMIT, "slow down", provider_retry_after_ms=99999)
        result = _recover(self.session, 1, 1, "p", failure, self.policy,
                                     next_fn=self.delegate)
        self.assertIsNone(result)
        self.assertEqual(self.calls, 1)
        self.assertEqual(list(self.session.events), [])

    def test_provider_retry_after_over_max_always_uses_local(self):
        always = {"mode": "always", "initialDelayMs": 1, "maxDelayMs": 2, "jitterRatio": 0}
        failure = LlmFailure(RATE_LIMIT, "slow down", provider_retry_after_ms=99999)
        result = _recover(self.session, 1, 1, "p", failure, always)
        self.assertEqual(result, {"kind": "retry"})
        self.assertEqual(self.session.events[0]["data"]["delayMs"], 1)
        self.assertEqual(self.session.events[0]["data"]["mode"], "always")
        self.assertNotIn("maxRetries", self.session.events[0]["data"])

    def test_always_mode_prefers_downstream_decision(self):
        always = {"mode": "always", "initialDelayMs": 1, "maxDelayMs": 2, "jitterRatio": 0}
        failure = LlmFailure(AUTH, "not retryable but always")
        result = _recover(
            self.session, 1, 1, "p", failure, always,
            next_fn=lambda: {"kind": "retry"})
        self.assertEqual(result, {"kind": "retry"})
        self.assertEqual(list(self.session.events), [])

    def test_always_mode_retries_after_downstream_error(self):
        always = {"mode": "always", "initialDelayMs": 1, "maxDelayMs": 2, "jitterRatio": 0}
        failure = LlmFailure(AUTH, "x")
        result = _recover(self.session, 1, 1, "p", failure, always,
                                     next_fn=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        self.assertEqual(result, {"kind": "retry"})
        self.assertEqual([e["type"] for e in self.session.events], ["llm/retry", "llm/retry-started"])

    def test_aborted_signal_before_wait(self):
        signal = type("S", (), {"aborted": True})()
        result = _recover(self.session, 1, 1, "p", self.failure, self.policy,
                                     signal=signal)
        self.assertIsNone(result)
        # 对齐上游：normal 分支不在派发前检查 abort——llm/retry 仍落，
        # 等待段（可取消）立即放弃，不落 llm/retry-started
        self.assertEqual([e["type"] for e in self.session.events], ["llm/retry"])

    def test_aborted_during_wait(self):
        class FlipSignal:
            def __init__(self):
                self.aborted = False
            def set(self):
                self.aborted = True
        signal = FlipSignal()
        policy = resolve_retry_policy({
            "mode": "normal", "maxRetries": 2,
            "backoff": {"initialDelayMs": 200, "maxDelayMs": 500, "jitterRatio": 0},
        })
        import threading
        t = threading.Timer(0.12, signal.set)
        t.start()
        try:
            result = _recover(self.session, 1, 1, "p", self.failure, policy,
                                         signal=signal)
        finally:
            t.cancel()
        self.assertIsNone(result)
        types = [e["type"] for e in self.session.events]
        self.assertEqual(types, ["llm/retry"])   # started 不落

    def test_cancellable_delay_zero(self):
        self.assertTrue(asyncio.run(cancellable_delay(0, None)))

    def test_dict_failure_facts(self):
        wide = resolve_retry_policy({
            "mode": "normal", "maxRetries": 2,
            "backoff": {"initialDelayMs": 1, "maxDelayMs": 5000, "jitterRatio": 0},
        })
        result = _recover(
            self.session, 1, 1, "p",
            {"code": RATE_LIMIT, "message": "m", "providerRetryAfterMs": 2500},
            wide)
        self.assertEqual(result, {"kind": "retry"})
        self.assertEqual(self.session.events[0]["data"]["delayMs"], 2500)


# ---------- loop 接线 ----------

class FlakyAdapter(LlmAdapter):
    provider = "flaky"
    retry_policy = None

    def __init__(self, fail_times: int, code: str = RATE_LIMIT):
        self.fail_times = fail_times
        self.code = code
        self.retry_policy = resolve_retry_policy({
            "mode": "normal", "maxRetries": 2,
            "backoff": {"initialDelayMs": 1, "maxDelayMs": 2, "jitterRatio": 0},
        })

    async def stream(self, messages, tools, signal=None):
        if self.fail_times > 0:
            self.fail_times -= 1
            raise LlmFailure(self.code, f"HTTP 429: {self.code}")
        yield {"type": "block-start", "index": 0, "blockType": "text"}
        yield {"type": "text-delta", "index": 0, "text": "恢复成功。"}
        yield {"type": "block-end", "index": 0, "block": {"type": "text", "text": "恢复成功。"}}
        yield {"type": "finish", "reason": {"kind": "stop"}}


class LoopRetryTest(unittest.TestCase):
    def _run(self, adapter):
        ctx = Context(name="root")
        apply_retry_planner(ctx)
        loop = AgentLoop(Session("loop-retry"), adapter, ToolRegistry(ctx), ctx)
        loop.followup("重试一次")
        return loop

    def test_retry_succeeds_then_completes(self):
        loop = self._run(FlakyAdapter(fail_times=1))
        events = loop.session.events
        types = [e["type"] for e in events]
        self.assertIn("llm/retry", types)
        self.assertIn("llm/retry-started", types)
        retry = next(e for e in events if e["type"] == "llm/retry")["data"]
        self.assertEqual(retry["retry"], 1)
        self.assertEqual(retry["provider"], "flaky")
        self.assertEqual(retry["failure"]["code"], RATE_LIMIT)
        turn_end = next(e for e in events if e["type"] == "turn/end")["data"]["reason"]
        self.assertEqual(turn_end["kind"], "completed")
        headers = [e for e in events if e["type"] == "request/header"]
        self.assertEqual(len(headers), 1)   # 重试不重复落 header
        started = next(e for e in events if e["type"] == "llm/retry-started")["data"]
        self.assertEqual(started["retryId"], retry["retryId"])
        self.assertLess(next(e for e in events if e["type"] == "llm/retry")["seq"],
                        next(e for e in events if e["type"] == "assistant/chunk")["seq"])

    def test_max_retries_exhausted_turn_error(self):
        adapter = FlakyAdapter(fail_times=99)
        ctx = Context(name="root")
        apply_retry_planner(ctx)
        loop = AgentLoop(Session("loop-retry"), adapter, ToolRegistry(ctx), ctx)
        # 对齐既有契约：失败在 followup 冒泡（turn/end 仍由 finally 落日志）
        with self.assertRaises(LlmFailure) as cm:
            loop.followup("重试一次")
        self.assertEqual(cm.exception.code, RATE_LIMIT)
        turn_end = next(e for e in loop.session.events if e["type"] == "turn/end")["data"]["reason"]
        self.assertEqual(turn_end["kind"], "error")
        self.assertEqual(turn_end["error"]["code"], RATE_LIMIT)
        retries = [e for e in loop.session.events if e["type"] == "llm/retry"]
        self.assertEqual(len(retries), 2)   # maxRetries=2 → 两次后放弃
        self.assertEqual([e["data"]["retry"] for e in retries], [1, 2])

    def test_non_retryable_code_terminal(self):
        adapter = FlakyAdapter(fail_times=99, code=AUTH)
        ctx = Context(name="root")
        apply_retry_planner(ctx)
        loop = AgentLoop(Session("loop-retry"), adapter, ToolRegistry(ctx), ctx)
        with self.assertRaises(LlmFailure) as cm:
            loop.followup("重试一次")
        self.assertEqual(cm.exception.code, AUTH)
        turn_end = next(e for e in loop.session.events if e["type"] == "turn/end")["data"]["reason"]
        self.assertEqual(turn_end["kind"], "error")
        self.assertEqual(turn_end["error"]["code"], AUTH)
        self.assertNotIn("llm/retry", [e["type"] for e in loop.session.events])

    def test_no_policy_adapter_terminal(self):
        class BareAdapter(LlmAdapter):
            provider = "bare"
            retry_policy = None

            async def stream(self, messages, tools, signal=None):
                raise LlmFailure(RATE_LIMIT, "x")
                yield  # pragma: no cover - 使函数成为 async 生成器（首个 __anext__ 即抛）
        ctx = Context(name="root")
        apply_retry_planner(ctx)
        loop = AgentLoop(Session("loop-retry"), BareAdapter(), ToolRegistry(ctx), ctx)
        with self.assertRaises(LlmFailure):
            loop.followup("重试一次")
        turn_end = next(e for e in loop.session.events if e["type"] == "turn/end")["data"]["reason"]
        self.assertEqual(turn_end["kind"], "error")
        self.assertNotIn("llm/retry", [e["type"] for e in loop.session.events])

    def test_default_policy_is_normal(self):
        from miniharness.llm import DeepSeekAdapter
        adapter = DeepSeekAdapter(api_key="sk-test")
        self.assertEqual(adapter.retry_policy["mode"], "normal")
        self.assertEqual(adapter.retry_policy["maxRetries"], 2)
        self.assertEqual(adapter.retry_policy["retryableCodes"], DEFAULT_RETRYABLE_CODES)

    def test_empty_response_is_retryable(self):
        policy = resolve_retry_policy()
        self.assertIn(EMPTY_RESPONSE, policy["retryableCodes"])


class TestHttpErrorCode(unittest.TestCase):
    """_http_error_code：复刻上游 error.ts 正则集（adapter.ts:138-149），
    400 上下文判定不做裸子串误判（"context" 普通参数名 → INVALID_REQUEST）。"""

    def _code(self, status, body):
        from miniharness.llm.deepseek import _http_error_code
        return _http_error_code(status, body)

    def test_400_context_window_exceeded(self):
        cases = [
            "This model's maximum context length is 16385 tokens. However, "
            "your messages resulted in 17000 tokens.",
            '{"error":{"code":"context_length_exceeded","message":"..."}}',
            "Please reduce the length of the messages. input is too long for the model",
            "The request is too large for the model's context window",
            "Your input message exceeds the model context length",
        ]
        for body in cases:
            self.assertEqual(self._code(400, body), CONTEXT_WINDOW_EXCEEDED, body)

    def test_400_plain_context_word_is_invalid_request(self):
        # 裸 "context" 子串不得判上下文超限（U2#8）
        self.assertEqual(
            self._code(400, "Invalid parameter: context is not a valid field"),
            "INVALID_REQUEST",
        )
        self.assertEqual(
            self._code(400, "temperature must be between 0 and 2"), "INVALID_REQUEST"
        )
        self.assertEqual(
            self._code(400, "Malformed JSON in request body"), "INVALID_REQUEST"
        )

    def test_quota_wording_wins_over_status(self):
        # quota 措辞任意状态先于 429/500（adapter.ts:141）
        self.assertEqual(self._code(429, "insufficient_quota"), "QUOTA")
        self.assertEqual(self._code(429, "Quota exceeded"), "QUOTA")
        self.assertEqual(self._code(500, "insufficient balance"), "QUOTA")
        self.assertEqual(self._code(400, "usage limit reached"), "QUOTA")

    def test_remaining_status_mapping(self):
        self.assertEqual(self._code(401, "x"), AUTH)
        self.assertEqual(self._code(403, "x"), AUTH)
        self.assertEqual(self._code(429, "Rate limit reached"), RATE_LIMIT)
        self.assertEqual(self._code(500, "boom"), "SERVER")
        self.assertEqual(self._code(503, "boom"), "SERVER")
        self.assertEqual(self._code(418, "teapot"), "HTTP_418")


if __name__ == "__main__":
    unittest.main()