"""telemetry：会话统计 / 用量统计 fold + derive_turn_token_usage + service + wire 视图。

对照流程：直读上游源码（session-stats/src/projection.ts、token-meter/src/usage-
projection.ts、token-meter/src/turn-usage.ts）逐字段移植，测试断言与上游语义一致，
不凭「文档声称」。
"""
import json
import unittest

from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.telemetry import install_usage_stats, projection_values
from miniharness.telemetry.folds import (
    derive_turn_token_usage,
    fold_session_stats,
    fold_token_usage,
    init_session_stats,
    init_token_usage,
    session_stats_view,
    token_usage_view,
)


def _ev(etype, seq, time, **data):
    return {"type": etype, "seq": seq, "time": time, "data": data}


def _stream(*records):
    return [r for r in records]


def _text_delta(index, text, time):
    return {"type": "text-chunks", "time0": time, "index": index, "dt": [], "texts": [text]}


def _usage_chunk(usage, time):
    return {"type": "chunk", "time": time, "chunk": {"type": "usage", "usage": usage}}


def _full_turn_events():
    """一个含 usage 的完整 turn（带内嵌流首令牌 + usage chunk）。"""
    return [
        _ev("turn/start", 0, 1000, turn=1),
        _ev("step/start", 1, 1000, turn=1, step=1),
        _ev("assistant/message", 2, 4000, turn=1, step=1,
            message={"role": "assistant", "source": {"kind": "model", "provider": "deepseek", "model": "chat"},
                     "content": [{"type": "text", "text": "hello"}]},
            stream=_stream(_text_delta(0, "hello", 1200),
                           _usage_chunk({"inputTokens": 11, "outputTokens": 7,
                                         "cacheReadTokens": 2, "cacheWriteTokens": 0}, 3000)),
            usage={"inputTokens": 11, "outputTokens": 7,
                   "cacheReadTokens": 2, "cacheWriteTokens": 0}),
        _ev("step/end", 3, 4000, turn=1, step=1),
        _ev("turn/end", 4, 4000, turn=1),
    ]


class SessionStatsFoldTest(unittest.TestCase):
    def test_counts_steps_and_turns(self):
        st = init_session_stats()
        for e in _full_turn_events():
            st = fold_session_stats(st, e)
        self.assertEqual(st["turns"], 1)
        self.assertEqual(st["steps"], 1)

    def test_two_turns_counts_distinct(self):
        evs = _full_turn_events()
        st = init_session_stats()
        for e in evs:
            st = fold_session_stats(st, e)
        second = [
            _ev("turn/start", 5, 5000, turn=2),
            _ev("step/start", 6, 5000, turn=2, step=1),
            _ev("step/end", 7, 5000, turn=2, step=1),
            _ev("turn/end", 8, 5000, turn=2),
        ]
        for e in second:
            st = fold_session_stats(st, e)
        self.assertEqual(st["turns"], 2)
        self.assertEqual(st["steps"], 2)

    def test_llm_ms_and_first_token_decode(self):
        st = init_session_stats()
        for e in _full_turn_events():
            st = fold_session_stats(st, e)
        # llmMs = assistant/message.time(4000) - step/start(1000)
        self.assertEqual(st["llmMs"], 3000)
        # first token 1200, start 1000 -> ttftMs 200; ttftSteps 1
        self.assertEqual(st["ttftMs"], 200)
        self.assertEqual(st["ttftSteps"], 1)
        # decodeMs = 4000 - 1200 = 2800; decodeTokens = 7 (usage reported)
        self.assertEqual(st["decodeMs"], 2800)
        self.assertEqual(st["decodeTokens"], 7)

    def test_max_tokens_empty_message_uncounted_as_step(self):
        """step/end 计步（不是 message 计数）：空 content 的 usage-host message 也多一步。"""
        evs = [
            _ev("turn/start", 0, 1000, turn=1),
            _ev("step/start", 1, 1000, turn=1, step=1),
            _ev("assistant/message", 2, 2000, turn=1, step=1,
                message={"role": "assistant", "content": []},
                usage={"inputTokens": 5, "outputTokens": 0}),
            _ev("step/end", 3, 2000, turn=1, step=1),
            _ev("turn/end", 4, 2000, turn=1),
        ]
        st = init_session_stats()
        for e in evs:
            st = fold_session_stats(st, e)
        self.assertEqual(st["steps"], 1)
        self.assertEqual(st["llmMs"], 1000)

    def test_cancelled_step_not_counted_in_time(self):
        """cancelled step 无 assistant/message -> 流时长不计入 llmMs/ttft。"""
        evs = [
            _ev("turn/start", 0, 1000, turn=1),
            _ev("step/start", 1, 1000, turn=1, step=1),
            _ev("step/end", 2, 4000, turn=1, step=1),
            _ev("turn/end", 3, 4000, turn=1),
        ]
        st = init_session_stats()
        for e in evs:
            st = fold_session_stats(st, e)
        self.assertEqual(st["llmMs"], 0)
        self.assertEqual(st["ttftMs"], 0)
        self.assertEqual(st["steps"], 1)

    def test_tool_time_pairing_and_prototype_safety(self):
        evs = [
            _ev("turn/start", 0, 1000, turn=1),
            _ev("step/start", 1, 1000, turn=1, step=1),
            _ev("tool/call", 2, 2000, turn=1, step=1, callId="call_1",
                 name="bash", arguments="{}"),
            _ev("tool/result", 3, 3000, turn=1, step=1,
                message={"role": "user", "source": {"kind": "tool", "callId": "call_1"},
                         "content": [{"type": "tool-result", "toolCallId": "call_1",
                                      "content": [{"type": "text", "text": "ok"}]}]}),
            _ev("step/end", 4, 3000, turn=1, step=1),
            _ev("turn/end", 5, 3000, turn=1),
        ]
        st = init_session_stats()
        for e in evs:
            st = fold_session_stats(st, e)
        self.assertEqual(st["toolMs"], 1000)
        # turn/end 清残留：无未配对 call
        self.assertEqual(st["pendingCalls"], {})

    def test_unmatched_tool_result_ignored(self):
        """result 的 callId 无对应 call（含 prototype 名）不污染 toolMs。"""
        evs = [
            _ev("turn/start", 0, 1000, turn=1),
            _ev("tool/result", 1, 2000, turn=1, step=1,
                message={"role": "user", "source": {"kind": "tool", "callId": "toString"},
                         "content": []}),
            _ev("turn/end", 2, 2000, turn=1),
        ]
        st = init_session_stats()
        for e in evs:
            st = fold_session_stats(st, e)
        self.assertEqual(st["toolMs"], 0)

    def test_view_is_strict_eight_keys(self):
        st = init_session_stats()
        view = session_stats_view(st)
        self.assertEqual(set(view), {"turns", "steps", "llmMs", "toolMs", "ttftMs",
                                     "ttftSteps", "decodeMs", "decodeTokens"})


class TokenUsageFoldTest(unittest.TestCase):
    def test_totals_accumulate(self):
        ut = init_token_usage()
        for e in _full_turn_events():
            ut = fold_token_usage(ut, e)
        self.assertEqual(ut["totals"], {
            "uncachedInputTokens": 11, "outputTokens": 7,
            "cacheReadTokens": 2, "cacheWriteTokens": 0})

    def test_usage_from_stream_when_durable_absent(self):
        """assistant/attempt 只扫流；assistant/message 无 usage 字段也扫流。"""
        evs = [
            _ev("turn/start", 0, 1000, turn=1),
            _ev("step/start", 1, 1000, turn=1, step=1),
            _ev("assistant/attempt", 2, 3000, turn=1, step=1,
                stream=_stream(_usage_chunk({"inputTokens": 3, "outputTokens": 2}, 2000))),
            _ev("step/end", 3, 3000, turn=1, step=1),
            _ev("turn/end", 4, 3000, turn=1),
        ]
        ut = init_token_usage()
        for e in evs:
            ut = fold_token_usage(ut, e)
        self.assertEqual(ut["totals"]["uncachedInputTokens"], 3)
        self.assertEqual(ut["totals"]["outputTokens"], 2)

    def test_retry_started_opens_replacement_slot(self):
        """llm/retry-started 关闭同 turn/step 的替换槽：旧 attempt 永久计入，
        新 attempt 累加（上游「closes the replacement slot so the retried
        attempt adds to the total」）。"""
        evs = [
            _ev("turn/start", 0, 1000, turn=1),
            _ev("step/start", 1, 1000, turn=1, step=1),
            _ev("assistant/attempt", 2, 2000, turn=1, step=1,
                stream=_stream(_usage_chunk({"inputTokens": 5, "outputTokens": 1}, 1500))),
            _ev("llm/retry-started", 3, 2000, turn=1, step=1, retryId="r1"),
            _ev("assistant/message", 4, 4000, turn=1, step=1,
                message={"role": "assistant", "content": [{"type": "text", "text": "x"}]},
                usage={"inputTokens": 9, "outputTokens": 4}),
            _ev("step/end", 5, 4000, turn=1, step=1),
            _ev("turn/end", 6, 4000, turn=1),
        ]
        ut = init_token_usage()
        for e in evs:
            ut = fold_token_usage(ut, e)
        # 无 retry-started 时 9 会替换 5；有则 5 永久计入 + 9 累加 = 14
        self.assertEqual(ut["totals"]["uncachedInputTokens"], 14)
        self.assertEqual(ut["totals"]["outputTokens"], 5)

    def test_no_usage_no_change(self):
        ut = init_token_usage()
        evs = [_ev("turn/start", 0, 1000, turn=1), _ev("turn/end", 1, 1000, turn=1)]
        for e in evs:
            before = id(ut)
            after = fold_token_usage(ut, e)
            self.assertIs(after, ut)

    def test_view_four_keys(self):
        ut = init_token_usage()
        view = token_usage_view(ut)
        self.assertEqual(set(view), {"uncachedInputTokens", "outputTokens",
                                     "cacheReadTokens", "cacheWriteTokens"})


class DeriveTurnTokenUsageTest(unittest.TestCase):
    def test_exact_accounting_with_routes(self):
        result = derive_turn_token_usage(_full_turn_events())
        self.assertIsNotNone(result)
        self.assertEqual(result["uncachedInputTokens"], 11)
        self.assertEqual(result["outputTokens"], 7)
        self.assertEqual(result["totalTokens"], 20)
        self.assertEqual(result["routes"], [{"provider": "deepseek", "model": "chat"}])

    def test_missing_turn_end_returns_none(self):
        evs = _full_turn_events()[:-1]  # 去掉 turn/end
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_usage_missing_input_returns_none(self):
        evs = [
            _ev("turn/start", 0, 1000, turn=1),
            _ev("step/start", 1, 1000, turn=1, step=1),
            _ev("assistant/message", 2, 2000, turn=1, step=1,
                message={"role": "assistant", "content": [{"type": "text", "text": "x"}]},
                usage={"outputTokens": 3}),  # 缺 inputTokens
            _ev("step/end", 3, 2000, turn=1, step=1),
            _ev("turn/end", 4, 2000, turn=1),
        ]
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_retry_chain_accumulates(self):
        evs = [
            _ev("turn/start", 0, 1000, turn=1),
            _ev("step/start", 1, 1000, turn=1, step=1),
            _ev("assistant/attempt", 2, 2000, turn=1, step=1,
                stream=_stream(_usage_chunk({"inputTokens": 5, "outputTokens": 1,
                                             "cacheReadTokens": 0, "cacheWriteTokens": 0}, 1500))),
            _ev("llm/retry", 3, 2000, turn=1, step=1, retryId="r1", delayMs=0),
            _ev("llm/retry-started", 4, 2000, turn=1, step=1, retryId="r1"),
            _ev("assistant/message", 5, 4000, turn=1, step=1,
                message={"role": "assistant", "content": [{"type": "text", "text": "x"}]},
                usage={"inputTokens": 9, "outputTokens": 4,
                       "cacheReadTokens": 0, "cacheWriteTokens": 0}),
            _ev("step/end", 6, 4000, turn=1, step=1),
            _ev("turn/end", 7, 4000, turn=1),
        ]
        result = derive_turn_token_usage(evs)
        self.assertIsNotNone(result)
        self.assertEqual(result["uncachedInputTokens"], 14)
        self.assertEqual(result["outputTokens"], 5)


class DeriveTurnTokenUsageFailClosedTest(unittest.TestCase):
    """fail-closed 分支：任何缺失边界/不完整 usage/矛盾计数 → None。"""

    def _base(self):
        return [
            _ev("turn/start", 0, 1000, turn=1),
            _ev("step/start", 1, 1000, turn=1, step=1),
        ]

    def _msg(self, **data):
        return _ev("assistant/message", 2, 2000, turn=1, step=1,
                   message={"role": "assistant", "content": [{"type": "text", "text": "x"}]},
                   **data)

    def _end(self):
        return [_ev("step/end", 3, 2000, turn=1, step=1),
                _ev("turn/end", 4, 2000, turn=1)]

    def test_double_turn_start_invalid(self):
        evs = self._base() + [_ev("turn/start", 5, 2000, turn=1)] + self._end()
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_event_before_turn_start_invalid(self):
        evs = [_ev("step/start", 0, 1000, turn=1, step=1),
               _ev("turn/start", 1, 1000, turn=1)] + self._end()
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_turn_end_turn_mismatch_invalid(self):
        evs = self._base() + [_ev("turn/end", 5, 2000, turn=2)]
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_double_turn_end_invalid(self):
        evs = self._base() + self._end() + [_ev("turn/end", 5, 2000, turn=1)]
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_event_after_turn_end_invalid(self):
        evs = self._base() + [_ev("step/end", 3, 2000, turn=1, step=1),
                              _ev("turn/end", 4, 2000, turn=1),
                              _ev("step/start", 5, 2000, turn=1, step=2)]
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_step_start_turn_mismatch_invalid(self):
        evs = [_ev("turn/start", 0, 1000, turn=1),
               _ev("step/start", 1, 1000, turn=2, step=1)] + self._end()
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_nested_step_start_invalid(self):
        evs = self._base() + [_ev("step/start", 5, 1000, turn=1, step=2)] + self._end()
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_message_turn_mismatch_invalid(self):
        evs = [_ev("turn/start", 0, 1000, turn=1),
               _ev("step/start", 1, 1000, turn=1, step=1),
               _ev("assistant/message", 2, 2000, turn=2, step=1,
                   message={"role": "assistant", "content": []},
                   usage={"inputTokens": 1, "outputTokens": 1,
                          "cacheReadTokens": 0, "cacheWriteTokens": 0})]
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_valid_usage_requires_cache_pair_when_no_total(self):
        evs = self._base() + \
            [self._msg(usage={"inputTokens": 1, "outputTokens": 1,
                              "cacheReadTokens": 0})] + self._end()
        # 无 totalTokens 时 require cacheRead+cacheWrite 都在
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_reasoning_exceeds_output_invalid(self):
        evs = self._base() + \
            [self._msg(usage={"inputTokens": 1, "outputTokens": 2,
                              "cacheReadTokens": 0, "cacheWriteTokens": 0,
                              "reasoningTokens": 3, "totalTokens": 2})] + self._end()
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_retry_after_message_open_step_mismatch_invalid(self):
        evs = [self._base()[0], _ev("step/start", 1, 1000, turn=1, step=1),
               self._msg(usage={"inputTokens": 1, "outputTokens": 1,
                                "cacheReadTokens": 0, "cacheWriteTokens": 0}),
               _ev("llm/retry", 3, 2000, turn=1, step=2, retryId="r1")]
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_retry_started_wrong_state_invalid(self):
        evs = self._base() + [
            _ev("assistant/message", 2, 2000, turn=1, step=1,
                message={"role": "assistant", "content": []},
                usage={"inputTokens": 1, "outputTokens": 1,
                       "cacheReadTokens": 0, "cacheWriteTokens": 0}),
            _ev("llm/retry-started", 3, 2000, turn=1, step=1, retryId="r1"),
        ]
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_usage_without_cache_but_total_present(self):
        evs = self._base() + [
            self._msg(usage={"inputTokens": 1, "outputTokens": 1, "totalTokens": 2})] + \
            self._end()
        result = derive_turn_token_usage(evs)
        self.assertIsNotNone(result)
        self.assertEqual(result["totalTokens"], 2)

    def test_safe_sum_overflow_invalid(self):
        big = 2 ** 53
        evs = self._base() + [
            self._msg(usage={"inputTokens": big, "outputTokens": 1,
                             "cacheReadTokens": 0, "cacheWriteTokens": 0})] + self._end()
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_message_stream_usage_fallback(self):
        evs = self._base() + [
            _ev("assistant/message", 2, 2000, turn=1, step=1,
                message={"role": "assistant", "content": [{"type": "text", "text": "x"}]},
                stream=_stream(_usage_chunk({"inputTokens": 4, "outputTokens": 3,
                                             "cacheReadTokens": 0, "cacheWriteTokens": 0}, 1500))) ] + \
            self._end()
        result = derive_turn_token_usage(evs)
        self.assertIsNotNone(result)
        self.assertEqual(result["uncachedInputTokens"], 4)
        self.assertEqual(result["outputTokens"], 3)

    def test_attempt_step_mismatch_invalid(self):
        evs = [self._base()[0], _ev("step/start", 1, 1000, turn=1, step=1),
               _ev("assistant/attempt", 2, 2000, turn=1, step=2,
                   stream=_stream(_usage_chunk({"inputTokens": 1, "outputTokens": 1,
                                                "cacheReadTokens": 0, "cacheWriteTokens": 0}, 1500)))]
        self.assertIsNone(derive_turn_token_usage(evs))

    def test_retry_chain_with_route_and_dedup(self):
        evs = [
            _ev("turn/start", 0, 1000, turn=1),
            _ev("step/start", 1, 1000, turn=1, step=1),
            _ev("assistant/attempt", 2, 2000, turn=1, step=1,
                stream=_stream(_usage_chunk({"inputTokens": 5, "outputTokens": 1,
                                             "cacheReadTokens": 0, "cacheWriteTokens": 0}, 1500))),
            _ev("llm/retry", 3, 2000, turn=1, step=1, retryId="r1", delayMs=0),
            _ev("llm/retry-started", 4, 2000, turn=1, step=1, retryId="r1"),
            _ev("assistant/message", 5, 4000, turn=1, step=1,
                message={"role": "assistant",
                         "source": {"kind": "model", "provider": "deepseek", "model": "chat"},
                         "content": [{"type": "text", "text": "x"}]},
                usage={"inputTokens": 9, "outputTokens": 4,
                       "cacheReadTokens": 0, "cacheWriteTokens": 0}),
            _ev("step/end", 6, 4000, turn=1, step=1),
            _ev("turn/end", 7, 4000, turn=1),
        ]
        result = derive_turn_token_usage(evs)
        self.assertEqual(result["uncachedInputTokens"], 14)
        self.assertEqual(result["outputTokens"], 5)
        # 仅有 message 结算的 attempt 带 route；retry 结算的 attempt 无 route，
        # 故 attributes 不全 → 不加 routes 键（对齐 aggregateAttempts 全有才披露）
        self.assertNotIn("routes", result)


class UsageStatsServiceTest(unittest.TestCase):
    def _ctx_session(self):
        ctx = Context(name="telemetry-test")
        session = Session("s1", meta={"cwd": "."})
        for e in _full_turn_events():
            data = dict(e["data"])
            if e["type"] == "assistant/message":
                session.append(e["type"], data, surfaceOp="append")
            else:
                session.append(e["type"], data)
        return ctx, session

    def test_install_and_incremental(self):
        ctx, session = self._ctx_session()
        svc = install_usage_stats(ctx)
        self.assertIs(install_usage_stats(ctx), svc)
        stats = svc.session_stats(session)
        self.assertEqual(stats["turns"], 1)
        self.assertEqual(stats["steps"], 1)
        usage = svc.token_usage(session)
        self.assertEqual(usage["uncachedInputTokens"], 11)

    def test_projection_values_matches_service(self):
        ctx, session = self._ctx_session()
        values = projection_values(session, install_usage_stats(ctx))
        self.assertEqual(set(values), {"sessionStats", "tokenUsage"})
        self.assertEqual(values["tokenUsage"]["outputTokens"], 7)


if __name__ == "__main__":
    unittest.main()