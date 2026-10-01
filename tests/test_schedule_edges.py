"""Schedule 边界验收：fail-closed 解码、时区/DST 边界、storage 记录准入、
归档准入、工具与运行时退化路径。

运行：python -m unittest tests.test_schedule_edges -v
"""
import asyncio
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone

from miniharness.core.agents import install_agents
from miniharness.core.schema import ValidationError, validate_schema_value
from miniharness.core.scope import Context
from miniharness.core.session_store import install_sessions
from miniharness.core.tools import ToolExec
from miniharness.storage import install_storage
from miniharness.storage.error import DomainError

from miniharness.schedule import (
    MAX_TITLE_LENGTH,
    MIN_EVERY_INTERVAL_SECONDS,
    ScheduleInputError,
    ScheduleLogError,
    allocate_schedule_id,
    apply_schedule_changes,
    canonicalize_time_zone,
    create_after_schedule_record,
    create_at_schedule_record,
    create_cron_schedule_record,
    create_daily_schedule_record,
    create_every_schedule_record,
    create_weekly_schedule_record,
    decode_schedule_change,
    decode_schedule_record,
    decode_stored_title,
    fold_schedule_events,
    install_schedule,
    parse_cron_input,
    parse_daily_input,
    parse_weekly_input,
    schedule_domain,
    schedule_task_schema,
)
from miniharness.schedule import domain as _d
from tests.test_schedule import _ms, _iso, ServiceHarness, _ms_local


# ===== 域 fail-closed =====

class DomainFailClosedTest(unittest.TestCase):
    def test_decode_id(self):
        for bad in ("", " x", "x "):
            with self.assertRaises(ScheduleLogError):
                _d._decode_id(bad)

    def test_decode_instant(self):
        for bad in ("not-a-date", "2025-02-30T00:00:00.000Z",
                    "2026-01-01T00:00:00Z"):
            with self.assertRaises(ScheduleLogError):
                _d._decode_instant(bad)

    def test_decode_after_record_shape(self):
        good = {"id": "x", "kind": "after", "prompt": "hi", "afterSeconds": 5,
                "scheduledAt": "2099-01-01T00:00:00.000Z"}
        for mutate in (
            lambda r: {k: v for k, v in r.items() if k != "scheduledAt"},
            lambda r: {**r, "prompt": "  "},
            lambda r: {**r, "afterSeconds": 0},
            lambda r: {**r, "title": " padded"},
            lambda r: {**r, "title": "x" * (MAX_TITLE_LENGTH + 1)},
        ):
            with self.assertRaises(ScheduleLogError):
                _d._decode_after_record(mutate(good))

    def test_decode_schedule_record_unknown_kind(self):
        with self.assertRaises(ScheduleLogError):
            decode_schedule_record({"id": "x", "kind": "nope", "prompt": "hi"})

    def test_decode_change_variants(self):
        for bad in (
            "x",
            {"version": 2, "operation": "create", "schedule": {}},
            {"version": 1, "operation": "create", "id": "x"},
            {"version": 1, "operation": "delete"},
            {"version": 1, "operation": "dispatch", "foo": "bar"},
            {"version": 1, "operation": "explode"},
            {"version": 1, "operation": "dispatch", "id": "x", "extra": 1},
        ):
            with self.assertRaises(ScheduleLogError):
                decode_schedule_change(bad)

    def test_fold_and_apply_errors(self):
        with self.assertRaises(ScheduleLogError):
            fold_schedule_events([], inherited_event_count=1)
        folded = {"active": (), "seenIds": ("schedule-1",)}
        with self.assertRaises(ScheduleLogError):
            apply_schedule_changes(folded, [{
                "operation": "create",
                "schedule": {"id": "schedule-1", "kind": "after", "prompt": "hi",
                             "afterSeconds": 1, "scheduledAt": "2099-01-01T00:00:00.000Z"}}])
        with self.assertRaises(ScheduleLogError):
            apply_schedule_changes({"active": (), "seenIds": ()},
                                   [{"operation": "delete", "id": "nope"}])
        with self.assertRaises(ScheduleLogError):
            apply_schedule_changes({"active": (), "seenIds": ()},
                                   [{"operation": "dispatch", "id": "nope"}])
        with self.assertRaises(ScheduleLogError):
            apply_schedule_changes({"active": (), "seenIds": ()},
                                   [{"operation": "explode"}])

    def test_allocate_collision(self):
        self.assertEqual(allocate_schedule_id(
            {"active": (), "seenIds": ("schedule-1", "schedule-2", "schedule-4")}),
            "schedule-5")

    def test_deadline_boundaries(self):
        with self.assertRaises(ScheduleInputError) as ctx:
            create_after_schedule_record("x", "hi", 1, _d._MAX_FOUR_DIGIT_YEAR_MS, "hi")
        self.assertEqual(ctx.exception.code, "time_out_of_range")
        with self.assertRaises(ScheduleInputError) as ctx:
            _d._future_instant(_ms_local() - 1, _ms_local())
        self.assertEqual(ctx.exception.code, "not_future")

    def test_parse_offset_invalid(self):
        for bad in ("bad", "0000-01-01T00:00:00Z", "2026-01-01T25:00:00Z",
                    "2026-01-01T00:00:00-00:00", "2026-02-30T00:00:00Z"):
            with self.assertRaises(ScheduleInputError):
                _d._parse_offset_instant(bad)

    def test_parse_local_at_invalid(self):
        for bad in ({"date": "bad", "time": "bad"},
                    {"date": "2026-01-01", "time": "25:00:00"}):
            with self.assertRaises(ScheduleInputError):
                _d._parse_local_at(bad)

    def test_canonicalize_time_zone(self):
        for bad in ("Not/A-Zone", "", "CST", "+08:00", " UTC"):
            with self.assertRaises(ScheduleInputError):
                canonicalize_time_zone(bad)
        self.assertEqual(canonicalize_time_zone("UTC"), "UTC")

    def test_local_at_gap_and_past(self):
        with self.assertRaises(ScheduleInputError) as ctx:
            create_at_schedule_record(
                "x", "hi", {"date": "2011-12-30", "time": "12:00:00",
                            "time_zone": "Pacific/Apia"},
                _ms("2011-12-29T00:00:00Z"), "hi")
        self.assertEqual(ctx.exception.code, "invalid_rule")
        with self.assertRaises(ScheduleInputError) as ctx:
            create_at_schedule_record(
                "x", "hi", {"date": "2020-01-01", "time": "00:00:00",
                            "time_zone": "UTC"}, _ms("2026-01-01T00:00:00Z"), "hi")
        self.assertEqual(ctx.exception.code, "not_future")
        with self.assertRaises(ScheduleInputError):
            create_at_schedule_record("x", "hi", 99, _ms_local(), "hi")

    def test_view_overdue(self):
        record = create_after_schedule_record(
            "x", "hi", 1, _ms_local() - 10_000, "hi")
        self.assertEqual(_d.schedule_view(record, _ms_local())["state"], "overdue")

    def test_json_stringify(self):
        rendered = _d._json_stringify({"s": "\u2029", "a": "\u2028"})
        self.assertIn("\\u2029", rendered)
        self.assertNotIn("\u2029", rendered)

    def test_local_date_at_low_year_boundary(self):
        # UTC year 1 with a negative offset resolves to local year 0 without raising.
        fields = _d._civil_fields(_d._MIN_FOUR_DIGIT_YEAR_MS
                                  + _d._utc_offset_ms("Etc/GMT+1", _d._MIN_FOUR_DIGIT_YEAR_MS))
        self.assertEqual(fields[:3], (0, 12, 31))
        self.assertEqual(fields[3], 23)


# ===== 选择器 / cron 边界 =====

class SelectorEdgesTest(unittest.TestCase):
    def test_daily_selector_shapes(self):
        for bad in (None, [], {}, {"time": "23:00:00"}, {"time_zone": "UTC"},
                    {"time": 23, "time_zone": "UTC"},
                    {"time": "23:00:00", "time_zone": "Unknown/Zone"},
                    {"time": "23:00:00", "time_zone": "UTC", "extra": 1}):
            with self.assertRaises(ScheduleInputError):
                parse_daily_input(bad)

    def test_weekly_selector_shapes(self):
        for bad in (None, [], {}, {"time": "09:00:00", "time_zone": "UTC"},
                    {"time": "09:00:00", "time_zone": "UTC", "weekdays": []},
                    {"time": "09:00:00", "time_zone": "UTC", "weekdays": [1, 1]},
                    {"time": "09:00:00", "time_zone": "UTC", "weekdays": [1], "extra": 1}):
            with self.assertRaises(ScheduleInputError):
                parse_weekly_input(bad)

    def test_cron_selector_shapes(self):
        for bad in (None, [], {}, {"expression": "0 0 * * *"},
                    {"time_zone": "UTC"},
                    {"expression": "0 0 * * *", "time_zone": 7},
                    {"expression": "0 0 * * *", "time_zone": "+08:00"},
                    {"expression": "0 0 * * *", "time_zone": "UTC", "extra": 1}):
            with self.assertRaises(ScheduleInputError):
                parse_cron_input(bad)

    # 上游 packages/schedule/schedule/tests/cron.spec.ts 的四位数年份边界四组用例。
    @staticmethod
    def _at(year: int, month: int, day: int, hour: int = 0, minute: int = 0,
            second: int = 0, micro: int = 0) -> int:
        return _d._epoch_ms(datetime(year, month, day, hour, minute, second,
                                     micro, tzinfo=timezone.utc))

    def _cron(self, expression: str, now: int, time_zone: str = "UTC") -> dict:
        return create_cron_schedule_record(
            "t", "Reminder", {"expression": expression, "time_zone": time_zone}, now, "Reminder")

    def test_cron_lowest_utc_year_boundaries(self):
        # cron.spec.ts「resolves local dates on either side of the lowest
        # supported UTC year」：年 1 下界的 scheduledAt 与 backward search。
        self.assertEqual(self._cron("30 0 * * *", self._at(1, 1, 1))["scheduledAt"],
                         "0001-01-01T00:30:00.000Z")
        self.assertEqual(self._cron("0 0 * * *", self._at(1, 1, 1), "Etc/GMT-1")["scheduledAt"],
                         "0001-01-01T23:00:00.000Z")
        record = self._cron("0 0 * * *", self._at(1, 1, 1))
        decision = _d.resolve_cron_occurrence(record, self._at(1, 1, 2))
        self.assertEqual(decision, {
            "occurrenceAt": "0001-01-02T00:00:00.000Z",
            "nextScheduledAt": "0001-01-03T00:00:00.000Z",
        })

    def test_cron_negative_offset_due_minute_inside_floor_date(self):
        # cron.spec.ts「resolves a negative-offset due minute inside the floor
        # date of its own zone」：四位数下限是 instant，本时区当日回溯早一天。
        record = self._cron("* * * * *", self._at(1, 1, 1, 0, 1), "Etc/GMT+1")
        decision = _d.resolve_cron_occurrence(record, self._at(1, 1, 1, 0, 30))
        self.assertEqual(decision, {
            "occurrenceAt": "0001-01-01T00:30:00.000Z",
            "nextScheduledAt": "0001-01-01T00:31:00.000Z",
        })

    def test_cron_final_occurrence_and_exhaustion(self):
        # cron.spec.ts「retains the final occurrence and reports exhaustion
        # without a five-digit target」：上界保留末次 occurrence，之后创建抛
        # time_out_of_range（不得产生五位年份目标）。
        record = self._cron("* * * * *", self._at(9999, 12, 31, 23, 0))
        decision = _d.resolve_cron_occurrence(record, _d._MAX_FOUR_DIGIT_YEAR_MS)
        self.assertEqual(decision, {"occurrenceAt": "9999-12-31T23:59:00.000Z"})
        with self.assertRaises(ScheduleInputError) as ctx:
            self._cron("* * * * *", self._at(9999, 12, 31, 23, 59))
        self.assertEqual(ctx.exception.code, "time_out_of_range")

    def test_cron_impossible_schedule_and_unsupported_instant(self):
        # cron.spec.ts「rejects an impossible schedule and an unsupported
        # creation instant」：2 月 30 日无解须报 four-digit-year；创建时刻须为
        # 四位数年份安全整数（NaN/小数/年 0/年 10000 一律 time_out_of_range）。
        with self.assertRaises(ScheduleInputError) as ctx:
            self._cron("0 0 30 2 *", self._at(2026, 1, 1))
        self.assertIn("four-digit-year", str(ctx.exception))
        # 年 0 / 年 10000 超出 datetime 年界，用 civil days 手算（等价上游
        # Date.parse('0000-12-31T23:59:59.999Z') 与 '+010000-01-01T00:00:00Z'）
        for now in (float("nan"), 0.5, _d._MIN_FOUR_DIGIT_YEAR_MS - 1,
                    _d._days_from_civil(10000, 1, 1) * 86_400_000):
            with self.subTest(now=now), self.assertRaises(ScheduleInputError) as ctx:
                self._cron("0 0 * * *", now)
            self.assertEqual(ctx.exception.code, "time_out_of_range")

    def test_every_and_after_types(self):
        with self.assertRaises(ScheduleInputError) as ctx:
            create_every_schedule_record("x", "hi", "300", _ms_local(), "hi")
        self.assertEqual(ctx.exception.code, "invalid_rule")
        with self.assertRaises(ScheduleInputError) as ctx:
            create_after_schedule_record("x", "hi", True, _ms_local(), "hi")
        self.assertEqual(ctx.exception.code, "invalid_rule")
        with self.assertRaises(ScheduleInputError) as ctx:
            create_after_schedule_record("x", "  ", 5, _ms_local(), "hi")
        self.assertEqual(ctx.exception.code, "invalid_prompt")

    def test_decode_stored_title(self):
        for bad in ("", "  ", " padded", "x" * (MAX_TITLE_LENGTH + 1)):
            with self.assertRaises(ScheduleLogError):
                decode_stored_title(bad)
        self.assertEqual(decode_stored_title("ok"), "ok")


# ===== storage 记录准入 =====

class StorageSchemaTest(unittest.TestCase):
    def _task(self, **overrides):
        record = create_after_schedule_record(
            "t", "p", 1, _ms("2026-09-15T00:00:00Z"), "t")
        task = {"sessionId": "s1", "record": record, "status": "active"}
        task.update(overrides)
        return task

    def test_valid_task_and_default_status(self):
        task = {"sessionId": "s1", "record": create_after_schedule_record(
            "t", "p", 1, _ms("2026-09-15T00:00:00Z"), "t")}
        parsed = validate_schema_value(schedule_task_schema, task)
        self.assertEqual(parsed["status"], "active")

    def test_rejections(self):
        daily = create_daily_schedule_record(
            "d", "D", {"time": "09:00:00", "time_zone": "UTC"},
            _ms("2026-09-15T00:00:00Z"), "D")
        bad_tasks = [
            {"sessionId": "", "record": self._task()["record"]},
            {"sessionId": "s1", "record": {**daily, "title": ""}},
            self._task(status="bogus"),
            self._task(extra=True),
            self._task(lastDelivery={"scheduledAt": "bad", "deliveredAt": "bad",
                                     "messageId": "m"}),
            self._task(deliveryHistory={"records": [], "earlierRecordsUnavailable": "no"}),
            # lastDelivery must match the latest retained receipt.
            self._task(lastDelivery={"scheduledAt": "2026-09-15T00:00:00.000Z",
                                     "deliveredAt": "2026-09-15T00:00:01.000Z",
                                     "messageId": "m"},
                       deliveryHistory={"records": [
                           {"scheduledAt": "2026-09-15T00:00:00.000Z",
                            "deliveredAt": "2026-09-15T00:00:01.000Z",
                            "messageId": "other", "prompt": "p"}],
                           "earlierRecordsUnavailable": False}),
        ]
        for bad in bad_tasks:
            with self.assertRaises(ValidationError):
                validate_schema_value(schedule_task_schema, bad)

    def test_malformed_task_rejects_opening_domain(self):
        tmp = tempfile.mkdtemp()
        # A single-layout unit whose stored task fails the schema must refuse open.
        document = {
            "unit": {"name": "schedule", "version": 1},
            "global": None,
            "tables": {"tasks": {"kept": {"sessionId": "s1", "record": {
                "id": "kept", "kind": "after", "prompt": "p", "afterSeconds": 1,
                "scheduledAt": "2099-01-01T00:00:00.000Z"}, "status": "active"}}},
        }
        with open(os.path.join(tmp, "schedule.json"), "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        ctx = Context(name="malformed")
        self.addCleanup(ctx.dispose)
        install_sessions(ctx)
        install_agents(ctx)
        install_storage(ctx, tmp)
        service = install_schedule(ctx)
        with self.assertRaises(DomainError):
            asyncio.run(service.list({"sessionId": "s1"}))

    def test_key_mismatch_rejects_opening(self):
        tmp = tempfile.mkdtemp()
        record = create_after_schedule_record(
            "right", "p", 1, _ms("2026-09-15T00:00:00Z"), "p")
        document = {
            "unit": {"name": "schedule", "version": 1},
            "global": None,
            "tables": {"tasks": {"wrong": {"sessionId": "s1", "record": record,
                                           "status": "active"}}},
        }
        with open(os.path.join(tmp, "schedule.json"), "w", encoding="utf-8") as handle:
            json.dump(document, handle)
        ctx = Context(name="mismatch")
        self.addCleanup(ctx.dispose)
        install_sessions(ctx)
        install_agents(ctx)
        install_storage(ctx, tmp)
        service = install_schedule(ctx)
        with self.assertRaises(RuntimeError):
            asyncio.run(service.list({"sessionId": "s1"}))


# ===== 归档准入 =====

class ArchiveAdmissionTest(ServiceHarness):
    def test_activity_reports_schedule_kind(self):
        async def run():
            await self.service.create(
                "root", {"prompt": "提醒", "title": "提醒", "after_seconds": 60})
            activities = await self.ctx.awaterfall(
                "workspace/session-activity", {"sessionId": "root"},
                base=lambda _payload: [])
            kinds = [entry.kind for entry in activities]
            self.assertIn("schedule", kinds)
            own = next(entry for entry in activities if entry.kind == "schedule")
            self.assertTrue(own.items)
            self.assertEqual(own.items[0].label, "提醒")
        asyncio.run(run())

    def test_stop_deletes_active_tasks(self):
        async def run():
            record = await self.service.create(
                "root", {"prompt": "提醒", "title": "提醒", "after_seconds": 60})
            await self.ctx.aparallel("workspace/session-stop", {"sessionId": "root"})
            self.assertIsNone(self.service._table.get(record["id"]))
            self.assertEqual(await self.service.list({"sessionId": "root"}), [])
        asyncio.run(run())


# ===== 工具 / 运行时退化 =====

class ToolRuntimeEdgesTest(ServiceHarness):
    def _call(self, name, args):
        return self.agent.tools.resolve(name).execute(args, ToolExec(agent=self.agent))

    def test_agent_mismatch_internal_error(self):
        async def run():
            value = await self.agent.tools.resolve("schedule_create").execute(
                {"prompt": "p", "title": "t", "after_seconds": 1},
                ToolExec(agent=None))
            self.assertEqual(value["code"], "internal_error")
        asyncio.run(run())

    def test_update_needs_a_change(self):
        async def run():
            created = await self._call("schedule_create",
                                       {"prompt": "p", "title": "t", "every_seconds": 300})
            result = await self._call("schedule_update", {"id": created["id"]})
            self.assertEqual(result["code"], "invalid_selector")
            result = await self._call("schedule_update", {"id": " x ", "title": "y"})
            self.assertEqual(result["code"], "invalid_rule")
        asyncio.run(run())

    def test_update_ended_via_catalog(self):
        async def run():
            created = await self._call("schedule_create",
                                       {"prompt": "p", "title": "t", "every_seconds": 300})
            await self.service._table.put(created["id"], {
                **self.service._table.get(created["id"]), "status": "inactive"})
            result = await self._call("schedule_update",
                                      {"id": created["id"], "title": "renamed"})
            self.assertEqual(result["code"], "schedule_ended")
        asyncio.run(run())

    def test_future_task_only_arms_timer(self):
        async def run():
            await self.service._ensure_domain()
            record = create_after_schedule_record(
                "schedule-1", "p", 3600, _ms_local(), "t")
            await self.service._table.put("schedule-1", {
                "sessionId": "root", "record": record, "status": "active"})
            self.service._runtime.request_drive()
            await asyncio.sleep(0.05)
            self.assertEqual(self.followups, [])
            self.assertIsNotNone(self.service._runtime._timer)
            await self.service._runtime.dispose()
        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
