"""C16 Schedule 边界验收：域 fail-closed 分支、工具错误路径、运行时退化路径。

运行：python -m unittest tests.test_schedule_edges -v
"""
import asyncio
import unittest

from miniharness.schedule import (
    MIN_EVERY_INTERVAL_SECONDS,
    ScheduleInputError,
    ScheduleLogError,
    allocate_schedule_id,
    create_after_schedule_record,
    create_at_schedule_record,
    create_every_schedule_record,
    decode_schedule_change,
    fold_schedule_events,
    register_schedule_tools,
    resolve_every_occurrence,
    schedule_view,
)
from miniharness.schedule.domain import (
    _MIN_EPOCH,
    _MAX_EPOCH,
    _epoch_ms,
    _format_epoch_ms,
    canonicalize_time_zone,
    now_ms,
    apply_schedule_changes,
)
from miniharness.schedule.runtime import ScheduleRuntime
from tests.test_schedule import _call, _epoch_of, _parent_loop, _RuntimeAgent, _RuntimeHarness


def _create_change(record):
    return {"version": 1, "operation": "create", "schedule": record}


def _future_canonical(seconds_ahead=60):
    return _format_epoch_ms(now_ms() + seconds_ahead * 1000)


# ===== 域 fail-closed =====

class DomainFailClosedTest(unittest.TestCase):
    def test_decode_id_empty(self):
        from miniharness.schedule.domain import _decode_id
        with self.assertRaises(ScheduleLogError):
            _decode_id("")

    def test_decode_id_whitespace(self):
        from miniharness.schedule.domain import _decode_id
        with self.assertRaises(ScheduleLogError):
            _decode_id(" x ")

    def test_decode_instant_bad_format(self):
        from miniharness.schedule.domain import _decode_instant
        with self.assertRaises(ScheduleLogError):
            _decode_instant("not-a-date")

    def test_decode_instant_impossible_date(self):
        # Python 的 strptime 会将 2025-02-30 规范化为 3 月 2 日；回读不匹配仍应不可用。
        from miniharness.schedule.domain import _decode_instant
        with self.assertRaises(ScheduleLogError):
            _decode_instant("2025-02-30T00:00:00.000Z")

    def test_decode_after_wrong_keys(self):
        with self.assertRaises(ScheduleLogError):
            from miniharness.schedule.domain import _decode_after_record
            _decode_after_record({"id": "x", "kind": "after"})

    def test_decode_after_bad_prompt(self):
        with self.assertRaises(ScheduleLogError):
            from miniharness.schedule.domain import _decode_after_record
            _decode_after_record({"id": "x", "kind": "after", "prompt": "  ",
                                   "afterSeconds": 1, "scheduledAt": _future_canonical()})

    def test_decode_after_bad_after_seconds(self):
        with self.assertRaises(ScheduleLogError):
            from miniharness.schedule.domain import _decode_after_record
            _decode_after_record({"id": "x", "kind": "after", "prompt": "hi",
                                   "afterSeconds": 0, "scheduledAt": _future_canonical()})

    def test_decode_at_wrong_keys(self):
        with self.assertRaises(ScheduleLogError):
            from miniharness.schedule.domain import _decode_at_record
            _decode_at_record({"id": "x", "kind": "at"})

    def test_decode_at_bad_prompt(self):
        with self.assertRaises(ScheduleLogError):
            from miniharness.schedule.domain import _decode_at_record
            _decode_at_record({"id": "x", "kind": "at", "prompt": "  ",
                                "scheduledAt": _future_canonical()})

    def test_decode_every_too_small(self):
        with self.assertRaises(ScheduleLogError):
            from miniharness.schedule.domain import _decode_every_record
            _decode_every_record({"id": "x", "kind": "every", "prompt": "hi",
                                   "everySeconds": 299, "scheduledAt": _future_canonical()})

    def test_decode_schedule_record_unknown_kind(self):
        with self.assertRaises(ScheduleLogError):
            from miniharness.schedule.domain import _decode_schedule_record
            _decode_schedule_record({"id": "x", "kind": "none", "prompt": "hi"})

    def test_decode_change_non_object(self):
        with self.assertRaises(ScheduleLogError):
            decode_schedule_change("x")

    def test_decode_change_bad_version(self):
        with self.assertRaises(ScheduleLogError):
            decode_schedule_change({"version": 2, "operation": "create",
                                     "schedule": {"id": "x", "kind": "after",
                                                  "prompt": "hi", "afterSeconds": 1,
                                                  "scheduledAt": _future_canonical()}})

    def test_decode_change_create_bad_keys(self):
        with self.assertRaises(ScheduleLogError):
            decode_schedule_change({"version": 1, "operation": "create", "id": "x"})

    def test_decode_change_delete_bad_keys(self):
        with self.assertRaises(ScheduleLogError):
            decode_schedule_change({"version": 1, "operation": "delete"})

    def test_decode_change_dispatch_bad_keys(self):
        with self.assertRaises(ScheduleLogError):
            decode_schedule_change({"version": 1, "operation": "dispatch", "foo": "bar"})

    def test_decode_change_unknown_operation(self):
        with self.assertRaises(ScheduleLogError):
            decode_schedule_change({"version": 1, "operation": "explode"})

    def test_decode_change_dispatch_no_accepted_at(self):
        with self.assertRaises(ScheduleLogError):
            decode_schedule_change({"version": 1, "operation": "dispatch", "id": "x",
                                     "extra": 1})

    def test_apply_id_reuse(self):
        folded = {"active": (), "seenIds": ("schedule-1",)}
        with self.assertRaises(ScheduleLogError):
            apply_schedule_changes(folded, [_create_change(
                {"id": "schedule-1", "kind": "after", "prompt": "hi",
                 "afterSeconds": 1, "scheduledAt": _future_canonical()})])

    def test_apply_delete_inactive(self):
        folded = {"active": (), "seenIds": ()}
        with self.assertRaises(ScheduleLogError):
            apply_schedule_changes(folded, [{"version": 1, "operation": "delete", "id": "nope"}])

    def test_apply_dispatch_inactive(self):
        folded = {"active": (), "seenIds": ()}
        with self.assertRaises(ScheduleLogError):
            apply_schedule_changes(folded, [{"version": 1, "operation": "dispatch", "id": "nope"}])

    def test_apply_one_shot_with_accepted_at(self):
        record = create_after_schedule_record("schedule-1", "hi", 1, now_ms())
        folded = {"active": [record], "seenIds": ("schedule-1",)}
        with self.assertRaises(ScheduleLogError):
            apply_schedule_changes(folded, [{"version": 1, "operation": "dispatch",
                                              "id": "schedule-1", "acceptedAt": _future_canonical()}])

    def test_apply_every_missing_accepted_at(self):
        record = create_every_schedule_record("schedule-1", "hi", 300, now_ms())
        folded = {"active": [record], "seenIds": ("schedule-1",)}
        with self.assertRaises(ScheduleLogError):
            apply_schedule_changes(folded, [{"version": 1, "operation": "dispatch", "id": "schedule-1"}])

    def test_apply_unknown_decoded_operation(self):
        folded = {"active": (), "seenIds": ()}
        with self.assertRaises(ScheduleLogError):
            apply_schedule_changes(folded, [{"version": 1, "operation": "explode"}])

    def test_fold_inherited_out_of_range(self):
        with self.assertRaises(ScheduleLogError):
            fold_schedule_events([], inherited_event_count=1)

    def test_allocate_id_collision(self):
        # 分配起点为 len(seen)+1 并跳过已有 id，不回填空洞
        folded = {"active": (), "seenIds": ("schedule-1", "schedule-2", "schedule-4")}
        self.assertEqual(allocate_schedule_id(folded), "schedule-5")

    def test_future_instant_time_out_of_range(self):
        with self.assertRaises(ScheduleInputError) as cm:
            create_after_schedule_record("x", "hi", 1, _MAX_EPOCH)
        self.assertEqual(cm.exception.code, "time_out_of_range")

    def test_future_instant_not_future(self):
        from miniharness.schedule.domain import _future_instant
        with self.assertRaises(ScheduleInputError) as cm:
            _future_instant(now_ms() - 1, now_ms())
        self.assertEqual(cm.exception.code, "not_future")

    def test_parse_offset_invalid_string(self):
        with self.assertRaises(ScheduleInputError):
            from miniharness.schedule.domain import _parse_offset_instant
            _parse_offset_instant("bad")

    def test_parse_offset_year_zero(self):
        with self.assertRaises(ScheduleInputError):
            from miniharness.schedule.domain import _parse_offset_instant
            _parse_offset_instant("0000-01-01T00:00:00Z")

    def test_parse_offset_hour_overflow(self):
        with self.assertRaises(ScheduleInputError):
            from miniharness.schedule.domain import _parse_offset_instant
            _parse_offset_instant("2026-01-01T25:00:00Z")

    def test_parse_offset_negative_offset_invalid(self):
        with self.assertRaises(ScheduleInputError):
            from miniharness.schedule.domain import _parse_offset_instant
            _parse_offset_instant("2026-01-01T00:00:00-00:00")

    def test_parse_offset_valid(self):
        from miniharness.schedule.domain import _parse_offset_instant
        result = _parse_offset_instant("2026-01-01T00:00:00+08:00")
        self.assertIsInstance(result, int)

    def test_parse_local_at_invalid_shape(self):
        with self.assertRaises(ScheduleInputError):
            from miniharness.schedule.domain import _parse_local_at
            _parse_local_at({"date": "bad", "time": "bad"})

    def test_parse_local_at_hour_overflow(self):
        with self.assertRaises(ScheduleInputError):
            from miniharness.schedule.domain import _parse_local_at
            _parse_local_at({"date": "2026-01-01", "time": "25:00:00"})

    def test_resolve_every_occurrence_before_target(self):
        record = create_every_schedule_record("x", "hi", 300, now_ms())
        with self.assertRaises(ScheduleLogError):
            resolve_every_occurrence(record, _epoch_of(record["scheduledAt"]) - 1)

    def test_resolve_every_occurrence_bad_accepted(self):
        record = create_every_schedule_record("x", "hi", 300, now_ms())
        with self.assertRaises(ScheduleLogError):
            resolve_every_occurrence(record, "not-an-int")  # type: ignore[arg-type]

    def test_resolve_every_occurrence_interval_not_safe(self):
        record = create_every_schedule_record("x", "hi", 300, now_ms())
        record["everySeconds"] = 0
        with self.assertRaises(ScheduleLogError):
            resolve_every_occurrence(record, _epoch_of(record["scheduledAt"]))

    def test_canonicalize_time_zone_invalid(self):
        with self.assertRaises(ScheduleInputError):
            canonicalize_time_zone("Not/A-Zone")

    def test_canonicalize_time_zone_empty(self):
        with self.assertRaises(ScheduleInputError):
            canonicalize_time_zone("")

    def test_schedule_view_overdue(self):
        record = create_after_schedule_record("x", "hi", 1, now_ms() - 10_000)
        view = schedule_view(record, now_ms())
        self.assertEqual(view["state"], "overdue")

    def test_json_stringify_u2029(self):
        from miniharness.schedule.domain import _json_stringify
        rendered = _json_stringify({"s": "\u2029"})
        self.assertIn("\\u2029", rendered)
        self.assertNotIn("\u2029", rendered)

    def test_create_at_past_not_future(self):
        with self.assertRaises(ScheduleInputError) as cm:
            create_at_schedule_record("x", "hi", {"date": "2020-01-01", "time": "00:00:00",
                                                      "time_zone": "UTC"}, now_ms())
        self.assertEqual(cm.exception.code, "not_future")

    def test_create_at_bad_time_zone_type(self):
        with self.assertRaises(ScheduleInputError):
            create_at_schedule_record("x", "hi", {"date": "2099-01-02", "time": "00:00:00",
                                                      "time_zone": 42}, now_ms())

    def test_create_at_non_string_non_dict(self):
        with self.assertRaises(ScheduleInputError):
            create_at_schedule_record("x", "hi", 99, now_ms())

    def test_create_after_bad_after_seconds(self):
        with self.assertRaises(ScheduleInputError) as cm:
            create_after_schedule_record("x", "hi", "1", now_ms())
        self.assertEqual(cm.exception.code, "invalid_rule")

    def test_create_after_zero(self):
        with self.assertRaises(ScheduleInputError) as cm:
            create_after_schedule_record("x", "hi", 0, now_ms())
        self.assertEqual(cm.exception.code, "invalid_rule")

    def test_create_after_bool(self):
        with self.assertRaises(ScheduleInputError) as cm:
            create_after_schedule_record("x", "hi", True, now_ms())
        self.assertEqual(cm.exception.code, "invalid_rule")

    def test_create_every_not_int(self):
        with self.assertRaises(ScheduleInputError) as cm:
            create_every_schedule_record("x", "hi", "300", now_ms())
        self.assertEqual(cm.exception.code, "invalid_rule")

    def test_create_after_past_not_future(self):
        from miniharness.schedule.domain import _future_instant
        with self.assertRaises(ScheduleInputError) as cm:
            _future_instant(now_ms() - 1_000, now_ms())
        self.assertEqual(cm.exception.code, "not_future")


# ===== 工具错误路径 =====

class ScheduleToolsPersistTest(unittest.TestCase):
    """flush 不参与 → persistence_uncertain / internal_error。"""

    def setUp(self):
        self.root, self.ctx, self.reg = _parent_loop()
        from miniharness.schedule.tools import register_schedule_tools
        self.disp = register_schedule_tools(self.ctx, self.root, lambda: None)

    def tearDown(self):
        self.disp()

    def test_create_persistence_uncertain(self):
        result = _call(self.reg, "schedule_create", {"prompt": "hi", "after_seconds": 1}, self.root)
        self.assertEqual(result["code"], "persistence_uncertain")

    def test_list_persistence_uncertain(self):
        result = _call(self.reg, "schedule_list", {}, self.root)
        self.assertEqual(result["code"], "persistence_uncertain")

    def test_delete_persistence_uncertain(self):
        result = _call(self.reg, "schedule_delete", {"id": "x"}, self.root)
        self.assertEqual(result["code"], "persistence_uncertain")

    def test_create_internal_error_on_agent_mismatch(self):
        exec_ = __import__("miniharness.core.tools", fromlist=["ToolExec"]).ToolExec(agent=None)
        value = self.reg.resolve("schedule_create").execute(
            {"prompt": "hi", "after_seconds": 1}, exec_)
        if __import__("inspect").isawaitable(value):
            value = asyncio.run(value)
        self.assertEqual(value["code"], "internal_error")


class ScheduleToolsFlushTest(unittest.TestCase):
    """flush 参与者 → 工具走完整流程。"""

    def setUp(self):
        self.root, self.ctx, self.reg = _parent_loop()
        self.ctx.on("session/flush", lambda payload: None)
        from miniharness.schedule.tools import register_schedule_tools
        self.disp = register_schedule_tools(self.ctx, self.root, lambda: None)

    def tearDown(self):
        self.disp()

    def test_delete_not_found(self):
        result = _call(self.reg, "schedule_delete", {"id": "schedule-none"}, self.root)
        self.assertFalse(result["deleted"])
        self.assertEqual(result["code"], "schedule_not_found")

    def test_list_empty(self):
        result = _call(self.reg, "schedule_list", {}, self.root)
        self.assertEqual(result, [])

    def test_create_present_call(self):
        from miniharness.core.tools import call_render
        tool = self.reg.resolve("schedule_create")
        exec_ = __import__("miniharness.core.tools", fromlist=["ToolExec"]).ToolExec(agent=self.root)
        value = call_render(tool, {"prompt": "hi", "after_seconds": 1},
                            _call(self.reg, "schedule_create", {"prompt": "hi", "after_seconds": 1}, self.root))
        self.assertIsInstance(value, list)
        self.assertEqual(value[0]["type"], "text")

    def test_notify_observer_failure(self):
        called = []
        def on_change():
            called.append(1)
            raise RuntimeError("observer boom")
        fresh_root, fresh_ctx, fresh_reg = _parent_loop()
        fresh_ctx.on("session/flush", lambda payload: None)
        disp = register_schedule_tools(fresh_ctx, fresh_root, on_change)
        _call(fresh_reg, "schedule_create", {"prompt": "hi", "after_seconds": 1}, fresh_root)
        self.assertTrue(called)
        disp()


# ===== 运行时退化路径 =====

class RuntimeEdgeTest(unittest.TestCase):
    def test_dispose_idempotent(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        runtime.start()
        async def main():
            await runtime.dispose()
            await runtime.dispose()
        asyncio.run(main())

    def test_decide_failure_warns(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        folded = {"active": [{"id": "x", "kind": "after", "prompt": "hi",
                               "afterSeconds": 1, "scheduledAt": "bad-utc"}], "seenIds": ("x",)}
        runtime._read_folded = lambda: folded  # type: ignore[method-assign]
        decision = runtime._decide(folded, now_ms())
        self.assertIsNone(decision)

    def test_maintenance_wait_branch(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        runtime._read_folded = lambda: {"active": [], "seenIds": ()}  # type: ignore[method-assign]
        result = runtime._maintenance({"kind": "wait"})
        self.assertFalse(result)

    def test_maintenance_overdue_one_shot(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        record = {"id": "x", "kind": "after", "prompt": "hi",
                   "afterSeconds": 1, "scheduledAt": _format_epoch_ms(now_ms() - 10_000)}
        runtime._read_folded = lambda: {"active": [record], "seenIds": ("x",)}  # type: ignore[method-assign]
        result = runtime._maintenance({"kind": "one-shot"})
        self.assertTrue(result)

    def test_maintenance_append_failure_faults(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        record = {"id": "x", "kind": "after", "prompt": "hi",
                   "afterSeconds": 1, "scheduledAt": _format_epoch_ms(now_ms() - 10_000)}
        runtime._read_folded = lambda: {"active": [record], "seenIds": ("x",)}  # type: ignore[method-assign]
        orig_append = agent.session.append
        def raise_append(*args, **kwargs):
            raise RuntimeError("disk full")
        agent.session.append = raise_append  # type: ignore[method-assign]
        try:
            result = runtime._maintenance({"kind": "one-shot"})
            self.assertFalse(result)
            self.assertTrue(runtime._faulted)
        finally:
            agent.session.append = orig_append

    def test_maintenance_followup_failure_warns(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        record = {"id": "x", "kind": "after", "prompt": "hi",
                   "afterSeconds": 1, "scheduledAt": _format_epoch_ms(now_ms() - 10_000)}
        runtime._read_folded = lambda: {"active": [record], "seenIds": ("x",)}  # type: ignore[method-assign]
        orig_followup = agent.followup
        def raise_followup(*args, **kwargs):
            raise RuntimeError("oom")
        agent.followup = raise_followup  # type: ignore[method-assign]
        try:
            result = runtime._maintenance({"kind": "one-shot"})
            self.assertFalse(result)
            self.assertTrue(harness.warns)
        finally:
            agent.followup = orig_followup

    def test_loopless_start_then_drive(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        runtime.start()  # 无 loop
        self.assertIsNone(runtime._run)
        record = create_after_schedule_record("x", "hi", 1, now_ms())
        agent.append_change({"version": 1, "operation": "create", "schedule": record})
        async def main():
            runtime.request_drive()
            await asyncio.sleep(1.1)
            await runtime.dispose()
        asyncio.run(main())
        self.assertEqual(agent.maintenance_calls, 1)

    def test_request_drive_while_running(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        async def main():
            runtime.start()
            runtime.request_drive()
            runtime.request_drive()
            self.assertIsNotNone(runtime._run)
            await runtime.dispose()
        asyncio.run(main())

    def test_is_live_false(self):
        class FakeCtx:
            def get(self, key, strict=True):
                return self._reg
            def __init__(self, reg): self._reg = reg
        class FakeRegistry:
            def get(self, aid): return None
            def roots(self): return []
        runtime = ScheduleRuntime(FakeCtx(FakeRegistry()), _RuntimeAgent())
        self.assertFalse(runtime.is_live())

    def test_is_live_true(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        self.assertTrue(runtime.is_live())

    def test_retire_runtime_with_loop(self):
        from miniharness.schedule import _retire_runtime
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        runtime.start()

        async def main():
            runtime.stop()
            retired = _retire_runtime(runtime)
            if retired is not None:
                await retired
            await asyncio.sleep(0.01)

        asyncio.run(main())

    def test_install_schedule_requires_agents_and_sessions(self):
        from miniharness.schedule import install_schedule
        ctx = type("Ctx", (), {"get": lambda self, k: None})()
        with self.assertRaisesRegex(RuntimeError, "requires ctx.agents and ctx.sessions"):
            install_schedule(ctx)



    def test_maintenance_faulted_request_drive_ignored(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        runtime._faulted = True
        runtime._request_drive()
        self.assertIsNone(runtime._run)

    def test_request_drive_stops_when_stopping(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        runtime._stopping = True
        runtime._request_drive()
        self.assertIsNone(runtime._run)

    def test_is_live_registry_no_get(self):
        class FakeCtx:
            def get(self, key, strict=True):
                return self._reg
            def __init__(self, reg): self._reg = reg
        class FakeRegistry:
            pass  # no get method
        runtime = ScheduleRuntime(FakeCtx(FakeRegistry()), _RuntimeAgent())
        self.assertFalse(runtime.is_live())

    def test_maintenance_run_requested_faulted(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        runtime._requested = True
        runtime._stopping = True
        async def main():
            await runtime._run_requested()
        asyncio.run(main())
        self.assertFalse(runtime._run)

    def test_maintenance_read_folded_corrupt(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        def raise_corrupt():
            raise RuntimeError("corrupt")
        agent.session.own_events = raise_corrupt  # type: ignore[method-assign]
        result = runtime._read_folded()
        self.assertIsNone(result)
        self.assertTrue(runtime._faulted)

    def test_run_requested_base_exception(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        runtime._requested = True
        import miniharness.schedule.runtime as rtmod
        orig_txn = rtmod.run_schedule_transaction
        async def bad_txn(*args, **kwargs):
            raise RuntimeError("boom")
        rtmod.run_schedule_transaction = bad_txn
        try:
            async def main():
                await runtime._run_requested()
            asyncio.run(main())
            self.assertTrue(runtime._faulted)
        finally:
            rtmod.run_schedule_transaction = orig_txn

    def test_create_at_empty_prompt(self):
        with self.assertRaises(ScheduleInputError) as cm:
            create_at_schedule_record("x", "  ", {"date": "2099-01-02",
                "time": "00:00:00", "time_zone": "UTC"}, now_ms())
        self.assertEqual(cm.exception.code, "invalid_prompt")

    def test_create_at_local_bad_keys(self):
        with self.assertRaises(ScheduleInputError):
            create_at_schedule_record("x", "hi", {"date": "2099-01-02",
                "time": "00:00:00", "time_zone": "UTC", "extra": 1}, now_ms())


# ===== 全局拆解 allSettled =====

class TeardownAllSettledTest(unittest.TestCase):
    def test_teardown_async_skips_none_and_awaits(self):
        from miniharness.schedule import _teardown_async

        settled = []

        async def one():
            settled.append("one")

        async def two():
            settled.append("two")

        async def main():
            await _teardown_async([None, one(), None, two()])

        asyncio.run(main())
        self.assertEqual(settled, ["one", "two"])

    def test_teardown_async_contains_failure(self):
        from miniharness.schedule import _teardown_async

        async def boom():
            raise RuntimeError("cleanup boom")

        async def main():
            await _teardown_async([boom(), None])

        asyncio.run(main())

    def test_retire_cleanup_contains_sync_failure(self):
        from miniharness.schedule import _retire_cleanup

        def broken():
            raise RuntimeError("sync boom")

        self.assertIsNone(_retire_cleanup(broken))

    def test_teardown_mixed_owner_cleanup(self):
        from miniharness.schedule import _teardown_async, _retire_cleanup

        async def good():
            return None

        def broken():
            raise RuntimeError("sync boom")

        retired = [_retire_cleanup(broken), _retire_cleanup(good)]
        pending = [item for item in retired if item is not None]
        self.assertEqual(len(pending), 1)

        async def main():
            await _teardown_async(retired)

        asyncio.run(main())


if __name__ == "__main__":
    unittest.main()
