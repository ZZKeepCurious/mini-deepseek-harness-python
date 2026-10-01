"""Schedule 域重写验收（对齐 packages/schedule/schedule/src/*）。

覆盖：六规则算术（every/daily/weekly/cron，Vixie DOM/DOW、DST 间隙跳过/重叠
取早、400 年搜索界）、title 必填、legacy `schedule/change` 只读重放、compare-and-
update、delivery-history 裁剪与身份游标分页、Host 全局 ScheduleService（storage-
domain 权威表）、工具四件套、单一 Host 定时器投递。

运行：python -m unittest tests.test_schedule -v
"""
import asyncio
import os
import tempfile
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.agents import install_agents
from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.session_store import install_sessions
from miniharness.core.tools import ToolExec, ToolRegistry, call_render
from miniharness.llm import FakeLlmAdapter
from miniharness.storage import install_storage

from miniharness.schedule import (
    MAX_TITLE_LENGTH,
    MIN_EVERY_INTERVAL_SECONDS,
    SCHEDULED_MESSAGE_FRAMING,
    ScheduleInputError,
    ScheduleLogError,
    append_delivery,
    canonicalize_cron_expression,
    canonicalize_time_zone,
    create_after_schedule_record,
    create_at_schedule_record,
    create_cron_schedule_record,
    create_daily_schedule_record,
    create_every_schedule_record,
    create_weekly_schedule_record,
    decode_schedule_change,
    decode_schedule_record,
    delivery_history_page,
    fold_schedule_events,
    install_schedule,
    is_recurring_schedule_record,
    normalize_weekdays,
    render_recurring_reminder_batch_framing,
    render_reminder_framing,
    resolve_cron_occurrence,
    resolve_daily_occurrence,
    resolve_every_occurrence,
    resolve_recurring_occurrence,
    resolve_schedule_update,
    resolve_weekly_occurrence,
    schedule_title,
    schedule_view,
)
from miniharness.schedule.domain import _format_epoch_ms, _parse_offset_instant
from miniharness.schedule.runtime import ScheduleRuntime


def _ms(value: str) -> int:
    return _parse_offset_instant(value)


def _iso(epoch: int) -> str:
    return _format_epoch_ms(epoch)


# ===== 规则算术 =====

class EveryRuleTest(unittest.TestCase):
    def test_create_and_resolve_latest_only(self):
        record = create_every_schedule_record(
            "e", "Every", 300, _ms("2026-09-15T00:00:00Z"), "Every")
        self.assertEqual(record["scheduledAt"], "2026-09-15T00:05:00.000Z")
        resolved = resolve_every_occurrence(record, _ms("2026-09-15T00:17:30Z"))
        self.assertEqual(resolved, {
            "occurrenceAt": "2026-09-15T00:15:00.000Z",
            "nextScheduledAt": "2026-09-15T00:20:00.000Z",
        })

    def test_interval_floor(self):
        with self.assertRaises(ScheduleInputError) as ctx:
            create_every_schedule_record("e", "E", MIN_EVERY_INTERVAL_SECONDS - 1,
                                         _ms("2026-09-15T00:00:00Z"), "E")
        self.assertEqual(ctx.exception.code, "frequency_too_high")

    def test_dispatch_before_target_rejected(self):
        record = create_every_schedule_record(
            "e", "E", 300, _ms("2026-09-15T00:00:00Z"), "E")
        with self.assertRaises(ScheduleLogError):
            resolve_every_occurrence(record, _ms("2026-09-15T00:00:00Z"))


class DailyRuleTest(unittest.TestCase):
    def test_strictly_future_and_view(self):
        record = create_daily_schedule_record(
            "d", "  Daily reminder  ", {"time": "23:00:00", "time_zone": "Asia/Shanghai"},
            _ms("2026-09-16T14:59:59.999Z"), "Daily reminder")
        self.assertEqual(record, {
            "id": "d", "kind": "daily", "title": "Daily reminder",
            "prompt": "Daily reminder", "time": "23:00:00.000",
            "timeZone": "Asia/Shanghai", "scheduledAt": "2026-09-16T15:00:00.000Z",
        })
        self.assertEqual(schedule_view(record, record_scheduled(record)),
                         {**record, "state": "overdue", "deliveryMode": "host"})
        self.assertEqual(schedule_view(record, record_scheduled(record) - 1)["state"], "scheduled")

    def test_next_decision(self):
        record = create_daily_schedule_record(
            "d", "D", {"time": "23:00:00", "time_zone": "Asia/Shanghai"},
            _ms("2026-09-16T14:59:59.999Z"), "D")
        self.assertEqual(resolve_daily_occurrence(record, _ms("2026-09-16T15:00:00Z")), {
            "occurrenceAt": "2026-09-16T15:00:00.000Z",
            "nextScheduledAt": "2026-09-17T15:00:00.000Z",
        })

    def test_dst_gap_skips_date(self):
        record = create_daily_schedule_record(
            "d", "D", {"time": "02:30:00", "time_zone": "America/New_York"},
            _ms("2026-03-07T00:00:00Z"), "D")
        self.assertEqual(record["scheduledAt"], "2026-03-07T07:30:00.000Z")
        self.assertEqual(resolve_daily_occurrence(record, _ms(record["scheduledAt"])), {
            "occurrenceAt": "2026-03-07T07:30:00.000Z",
            "nextScheduledAt": "2026-03-09T06:30:00.000Z",
        })

    def test_dst_overlap_uses_earlier_once(self):
        record = create_daily_schedule_record(
            "d", "D", {"time": "01:30:00", "time_zone": "America/New_York"},
            _ms("2026-11-01T04:00:00Z"), "D")
        self.assertEqual(record["scheduledAt"], "2026-11-01T05:30:00.000Z")
        for now in ("2026-11-01T05:30:00Z", "2026-11-01T06:00:00Z", "2026-11-01T06:30:00Z"):
            self.assertEqual(resolve_daily_occurrence(record, _ms(now)), {
                "occurrenceAt": "2026-11-01T05:30:00.000Z",
                "nextScheduledAt": "2026-11-02T06:30:00.000Z",
            })

    def test_latest_only_after_downtime(self):
        record = create_daily_schedule_record(
            "d", "D", {"time": "23:00:00", "time_zone": "Asia/Shanghai"},
            _ms("1800-01-01T00:00:00Z"), "D")
        self.assertEqual(resolve_daily_occurrence(record, _ms("2026-09-16T14:59:59Z")), {
            "occurrenceAt": "2026-09-15T15:00:00.000Z",
            "nextScheduledAt": "2026-09-16T15:00:00.000Z",
        })


class WeeklyRuleTest(unittest.TestCase):
    def test_normalize_weekdays(self):
        self.assertEqual(normalize_weekdays([7, 2, 5, 1]), [1, 2, 5, 7])
        for bad in ([], [0], [8], [1.5], ["1"], [1, 1]):
            with self.assertRaises(ScheduleInputError):
                normalize_weekdays(bad)

    def test_creation_and_earliest_weekday(self):
        record = create_weekly_schedule_record(
            "w", "Weekly", {"time": "09:00:00", "time_zone": "Asia/Shanghai",
                            "weekdays": [3]},
            _ms("2026-09-14T00:00:00Z"), "Weekly")
        self.assertEqual((record["weekdays"], record["scheduledAt"]),
                         ([3], "2026-09-16T01:00:00.000Z"))
        earlier = create_weekly_schedule_record(
            "w", "W", {"time": "09:00:00", "time_zone": "Europe/Paris",
                       "weekdays": [5, 2]},
            _ms("2026-09-14T00:00:00Z"), "W")
        self.assertEqual(earlier["scheduledAt"], "2026-09-15T07:00:00.000Z")

    def test_dst_gap_and_overlap(self):
        gap = create_weekly_schedule_record(
            "w", "W", {"time": "02:30:00", "time_zone": "America/New_York",
                       "weekdays": [7]},
            _ms("2026-03-07T00:00:00Z"), "W")
        self.assertEqual(gap["scheduledAt"], "2026-03-15T06:30:00.000Z")
        overlap = create_weekly_schedule_record(
            "w", "W", {"time": "01:30:00", "time_zone": "America/New_York",
                       "weekdays": [7]},
            _ms("2026-10-26T00:00:00Z"), "W")
        self.assertEqual(overlap["scheduledAt"], "2026-11-01T05:30:00.000Z")
        self.assertEqual(resolve_weekly_occurrence(overlap, _ms("2026-11-01T06:00:00Z")), {
            "occurrenceAt": "2026-11-01T05:30:00.000Z",
            "nextScheduledAt": "2026-11-08T06:30:00.000Z",
        })


class CronRuleTest(unittest.TestCase):
    CANONICAL = [
        ("0 0 * * *", "0 0 * * *"),
        ("*/15 9-17 * * 1-5", "*/15 9-17 * * 1-5"),
        ("30,10,20 * * * *", "10-30/10 * * * *"),
        ("*/1 * * * *", "* * * * *"),
        ("0-59 0-23 1-31 1-12 0-7", "0-59 0-23 1-31 1-12 0-6"),
        ("0 0 * * 7", "0 0 * * 0"),
        ("0 0 * * 1-2,3-4", "0 0 * * 1-4"),
        ("0 0 * * 5-7", "0 0 * * 0,5-6"),
        ("00 09 * * 5,4,3,2,1", "0 9 * * 1-5"),
        ("0 9 1-31 * */7", "0 9 1-31 * */7"),
        ("0 9 */2 * 1", "0 9 */2 * 1"),
        ("*,5 0 * * *", "* 0 * * *"),
        ("5,* 0 * * *", "0-59 0 * * *"),
    ]

    def test_canonicalization(self):
        for raw, canonical in self.CANONICAL:
            self.assertEqual(canonicalize_cron_expression(raw), canonical)
            self.assertEqual(canonicalize_cron_expression(canonical), canonical)

    def test_star_flag_preserved(self):
        for raw in ("0 9 1-31 * */7", "0 9 */2 * 1", "0 9 * * 1", "*/1 * * * *"):
            canonical = canonicalize_cron_expression(raw)
            for index, field in enumerate(raw.split()):
                self.assertEqual(canonical.split()[index].startswith("*"),
                                 field.startswith("*"))

    def test_rejections(self):
        for bad in ("0 0 * * * *", "@daily", "0 0 L * *", "60 0 * * *",
                    "0 24 * * *", "0 0 0 * *", "0 0 * 13 *", "0 0 * * 8",
                    "0 0 20-10 * *", "*/0 * * * *", "0,,1 * * * *",
                    "0 0 * * 1,", "5/2 * * * *", " 0 0 * * *"):
            with self.assertRaises(ScheduleInputError):
                canonicalize_cron_expression(bad)

    def test_target_selection(self):
        cases = [
            ("2026-09-16T00:00:00Z", "30 9 * * *", "Asia/Shanghai", "2026-09-16T01:30:00.000Z"),
            ("2026-04-01T00:00:00Z", "0 0 31 * *", "UTC", "2026-05-31T00:00:00.000Z"),
            ("2026-01-01T00:00:00Z", "0 0 29 2 *", "UTC", "2028-02-29T00:00:00.000Z"),
            ("2026-09-01T00:00:00Z", "0 0 13 * 5", "UTC", "2026-09-04T00:00:00.000Z"),
            ("2026-09-12T00:00:00Z", "0 0 13 * 5", "UTC", "2026-09-13T00:00:00.000Z"),
            ("2026-09-01T00:00:00Z", "0 9 */2 * 1", "UTC", "2026-09-07T09:00:00.000Z"),
            ("2026-09-13T00:00:00Z", "0 9 */2 * 1", "UTC", "2026-09-21T09:00:00.000Z"),
            ("2026-09-02T00:00:00Z", "0 9 1-31 * */2", "UTC", "2026-09-03T09:00:00.000Z"),
        ]
        for now, expression, zone, want in cases:
            record = create_cron_schedule_record(
                "c", "C", {"expression": expression, "time_zone": zone}, _ms(now), "C")
            self.assertEqual(record["scheduledAt"], want, expression)

    def test_dst_gap_and_overlap(self):
        gap = create_cron_schedule_record(
            "c", "C", {"expression": "30 2 * 3 0", "time_zone": "America/New_York"},
            _ms("2026-03-01T00:00:00Z"), "C")
        self.assertEqual(gap["scheduledAt"], "2026-03-01T07:30:00.000Z")
        self.assertEqual(resolve_cron_occurrence(gap, _ms("2026-03-08T12:00:00Z")), {
            "occurrenceAt": "2026-03-01T07:30:00.000Z",
            "nextScheduledAt": "2026-03-15T06:30:00.000Z",
        })
        overlap = create_cron_schedule_record(
            "c", "C", {"expression": "30 1 * 11 0", "time_zone": "America/New_York"},
            _ms("2026-10-25T00:00:00Z"), "C")
        self.assertEqual(overlap["scheduledAt"], "2026-11-01T05:30:00.000Z")
        self.assertEqual(resolve_cron_occurrence(overlap, _ms("2026-11-01T06:30:00Z")), {
            "occurrenceAt": "2026-11-01T05:30:00.000Z",
            "nextScheduledAt": "2026-11-08T06:30:00.000Z",
        })

    def test_unsatisfiable_returns_saved_target_and_creation_exhausts(self):
        impossible = {
            "id": "i", "kind": "cron", "title": "I", "prompt": "I",
            "expression": "0 0 30 2 *", "timeZone": "UTC",
            "scheduledAt": "2026-09-15T00:00:00.000Z",
        }
        self.assertEqual(resolve_cron_occurrence(impossible, _ms("2026-09-16T00:00:00Z")),
                         {"occurrenceAt": "2026-09-15T00:00:00.000Z"})
        with self.assertRaises(ScheduleInputError) as ctx:
            create_cron_schedule_record(
                "c", "C", {"expression": "0 0 30 2 *", "time_zone": "UTC"},
                _ms("2026-01-01T00:00:00Z"), "C")
        self.assertEqual(ctx.exception.code, "time_out_of_range")

    def test_recurring_dispatch_shared(self):
        record = create_cron_schedule_record(
            "c", "C", {"expression": "0 9 * * 1-5", "time_zone": "UTC"},
            _ms("2026-09-14T00:00:00Z"), "C")
        self.assertTrue(is_recurring_schedule_record(record))
        self.assertFalse(is_recurring_schedule_record(
            create_at_schedule_record("a", "A", "2026-09-16T01:00:00Z",
                                      _ms("2026-09-15T00:00:00Z"), "A")))
        self.assertEqual(resolve_recurring_occurrence(record, _ms(record["scheduledAt"])),
                         resolve_cron_occurrence(record, _ms(record["scheduledAt"])))


def record_scheduled(record):
    return _ms(record["scheduledAt"])


# ===== title =====

class TitleTest(unittest.TestCase):
    def test_required_and_trimmed(self):
        self.assertEqual(schedule_title("  hi  "), "hi")
        for bad in ("", "   ", "x" * (MAX_TITLE_LENGTH + 1)):
            with self.assertRaises(ScheduleInputError) as ctx:
                schedule_title(bad)
            self.assertEqual(ctx.exception.code, "invalid_prompt")

    def test_create_requires_title(self):
        with self.assertRaises(ScheduleInputError):
            create_after_schedule_record("x", "p", 5, _ms("2026-09-15T00:00:00Z"), "")

    def test_legacy_events_may_lack_title(self):
        legacy = {"id": "schedule-1", "kind": "after", "prompt": "p",
                  "afterSeconds": 5, "scheduledAt": "2099-01-01T00:00:00.000Z"}
        decoded = decode_schedule_change(
            {"version": 1, "operation": "create", "schedule": legacy})
        self.assertEqual(decoded["schedule"]["id"], "schedule-1")
        with self.assertRaises(ScheduleLogError):
            decode_schedule_record(legacy)


# ===== legacy fold =====

class LegacyFoldTest(unittest.TestCase):
    def _fold(self, changes):
        return fold_schedule_events([{"type": "schedule/change", "data": change}
                                     for change in changes])

    def test_create_delete_dispatch(self):
        record = create_every_schedule_record(
            "schedule-1", "E", 300, _ms("2026-09-15T00:00:00Z"), "E")
        folded = self._fold([
            {"version": 1, "operation": "create", "schedule": record},
            {"version": 1, "operation": "dispatch", "id": "schedule-1",
             "acceptedAt": "2026-09-15T00:05:00.000Z"},
        ])
        self.assertEqual(len(folded["active"]), 1)
        self.assertGreater(_ms(folded["active"][0]["scheduledAt"]), _ms(record["scheduledAt"]))

    def test_delete_and_unknown(self):
        record = create_after_schedule_record(
            "schedule-1", "A", 5, _ms("2026-09-15T00:00:00Z"), "A")
        folded = self._fold([
            {"version": 1, "operation": "create", "schedule": record},
            {"version": 1, "operation": "delete", "id": "schedule-1"},
        ])
        self.assertEqual(folded["active"], ())
        with self.assertRaises(ScheduleLogError):
            self._fold([{"version": 1, "operation": "delete", "id": "nope"}])

    def test_daily_and_cron_rejected_by_legacy_decoder(self):
        daily = create_daily_schedule_record(
            "d", "D", {"time": "09:00:00", "time_zone": "UTC"},
            _ms("2026-09-16T00:00:00Z"), "D")
        with self.assertRaises(ScheduleLogError):
            decode_schedule_change(
                {"version": 1, "operation": "create", "schedule": daily})


# ===== compare-and-update =====

class UpdateTest(unittest.TestCase):
    NOW = _ms("2026-09-16T00:00:00.125Z")

    def _daily(self):
        return create_daily_schedule_record(
            "t", "Keep prompt", {"time": "09:00:00.125", "time_zone": "US/Eastern"},
            self.NOW, "Kept name")

    def test_stale_expected_conflicts(self):
        daily = self._daily()
        self.assertEqual(
            resolve_schedule_update(daily, {**daily, "prompt": "other"},
                                    {"kind": "every", "every_seconds": 600}, self.NOW),
            {"id": "t", "updated": False, "code": "schedule_conflict"})

    def test_invalid_expected_returns_fixed_error(self):
        daily = self._daily()
        del daily["title"]
        self.assertEqual(
            resolve_schedule_update(self._daily(), daily, None, self.NOW),
            {"code": "invalid_rule",
             "message": "expected must be a complete valid Schedule record."})

    def test_equivalent_normalized_timing_keeps_target(self):
        daily = {**self._daily(), "time": "09:00:00.100",
                 "timeZone": "America/New_York"}
        result = resolve_schedule_update(
            daily, daily, {"kind": "daily",
                           "daily": {"time": "09:00:00.1", "time_zone": "America/New_York"}},
            self.NOW + 86_400_000)
        self.assertIs(result["record"], daily)
        self.assertFalse(result["updated"])

    def test_kind_change_recomputes(self):
        daily = self._daily()
        result = resolve_schedule_update(
            daily, daily, {"kind": "every", "every_seconds": 600}, self.NOW)
        self.assertTrue(result["updated"])
        self.assertEqual(result["record"]["kind"], "every")
        self.assertEqual(result["record"]["title"], "Kept name")

    def test_content_only_keeps_target(self):
        daily = self._daily()
        result = resolve_schedule_update(daily, daily, None, self.NOW,
                                         {"title": "Renamed", "prompt": "New"})
        self.assertTrue(result["updated"])
        self.assertEqual(result["record"]["scheduledAt"], daily["scheduledAt"])
        self.assertEqual(result["record"]["kind"], "daily")

    def test_invalid_replacement(self):
        daily = self._daily()
        self.assertEqual(resolve_schedule_update(daily, daily, None, self.NOW,
                                                 {"title": "   "}),
                         {"code": "invalid_prompt",
                          "message": "title is required and must be non-empty after trimming."})


# ===== delivery history =====

class DeliveryHistoryTest(unittest.TestCase):
    def _task(self, records):
        latest = records[-1] if records else None
        task = {
            "sessionId": "s1", "status": "inactive",
            "record": create_after_schedule_record(
                "t", "Current", 1, _ms("2026-09-15T00:00:00Z"), "Current"),
            "deliveryHistory": {"records": records, "earlierRecordsUnavailable": False},
        }
        if latest is not None:
            task["lastDelivery"] = {
                "scheduledAt": latest["scheduledAt"], "deliveredAt": latest["deliveredAt"],
                "messageId": latest["messageId"]}
        return task

    def _receipt(self, index):
        return {"messageId": f"message-{index}", "prompt": f"Sent {index}",
                "scheduledAt": "2026-09-15T00:00:00.000Z",
                "deliveredAt": ("2026-09-15T00:00:01.000Z" if index % 2 == 0
                                else "2026-09-14T00:00:01.000Z")}

    def test_append_prunes_by_cap_and_marks_flags(self):
        task = self._task([self._receipt(1), self._receipt(2)])
        appended = append_delivery(task, self._receipt(3), {"days": 30, "records": 2})
        self.assertEqual([r["messageId"] for r in appended["deliveryHistory"]["records"]],
                         ["message-2", "message-3"])
        self.assertTrue(appended["deliveryHistory"]["earlierRecordsUnavailable"])
        self.assertTrue(appended["deliveryHistory"]["earlierRecordsPruned"])
        self.assertEqual(appended["lastDelivery"], self._receipt(3))

    def test_append_window_and_legacy_unavailable_not_pruned(self):
        task = self._task([self._receipt(1), self._receipt(2)])
        fresh = {**self._receipt(9), "deliveredAt": "2026-09-16T00:00:00.000Z"}
        appended = append_delivery(task, fresh, {"days": 1, "records": 200})
        self.assertEqual([r["messageId"] for r in appended["deliveryHistory"]["records"]],
                         ["message-2", "message-9"])
        legacy = self._task([self._receipt(1)])
        legacy["deliveryHistory"] = {"records": [self._receipt(1)],
                                     "earlierRecordsUnavailable": True}
        appended = append_delivery(legacy, self._receipt(2), {"days": 30, "records": 200})
        self.assertTrue(appended["deliveryHistory"]["earlierRecordsUnavailable"])
        self.assertFalse(appended["deliveryHistory"]["earlierRecordsPruned"])

    def test_page_identity_cursor(self):
        records = [self._receipt(index) for index in range(103)]
        task = self._task(records)
        meta = {"earlierRecordsUnavailable": False,
                "earlierRecordsPruned": False, "retention": {"days": 30, "records": 200}}
        first = delivery_history_page(task, {"id": "t", "limit": 100}, {"days": 30, "records": 200})
        self.assertEqual(len(first["records"]), 100)
        self.assertEqual(first["records"][0]["messageId"], "message-102")
        self.assertEqual(first["nextBefore"], "message-3")
        self.assertFalse(first["earlierRecordsPruned"])
        second = delivery_history_page(
            task, {"id": "t", "limit": 100, "before": "message-3"},
            {"days": 30, "records": 200})
        self.assertEqual([r["messageId"] for r in second["records"]],
                         ["message-2", "message-1", "message-0"])
        self.assertNotIn("nextBefore", second)
        unknown = delivery_history_page(
            task, {"id": "t", "limit": 10, "before": "missing"},
            {"days": 30, "records": 200})
        self.assertEqual(unknown, {"id": "t", "code": "delivery_cursor_not_found"})
        self.assertIn("retention", first)


# ===== service =====

class ServiceHarness(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Context(name="schedule-service")
        self.addCleanup(self.ctx.dispose)
        install_sessions(self.ctx)
        install_agents(self.ctx)
        install_storage(self.ctx, os.path.join(self.tmp.name, "storage"))
        self.ctx.on("session/flush", lambda payload: None)
        self.reg = ToolRegistry(self.ctx)
        self.agent = AgentLoop(
            Session("root"), FakeLlmAdapter(final_text="ok"), self.reg, self.ctx,
            system_prompt="root")
        self.agent.publish()
        self.followups = []
        original = self.agent.followup

        def recording(message, source="user"):
            self.followups.append(message)
            return original(message, source)

        self.agent.followup = recording
        self.service = install_schedule(self.ctx)
        self.changed = []
        self.ctx.on("schedule/changed", lambda *_args: self.changed.append(1))


class ServiceTest(ServiceHarness):
    def test_create_writes_table_not_session_events(self):
        async def run():
            record = await self.service.create(
                "root", {"prompt": "喝水", "title": "喝水", "after_seconds": 60})
            self.assertEqual(record["kind"], "after")
            self.assertTrue(record["title"])
            self.assertIsNotNone(self.service._table.get(record["id"]))
            self.assertTrue(self.changed)
            self.assertFalse(any(event["type"] == "schedule/change"
                                 for event in self.agent.session.snapshot_events()))
        asyncio.run(run())

    def test_list_catalog_history_delete_update(self):
        async def run():
            record = await self.service.create(
                "root", {"prompt": "喝水", "title": "喝水", "after_seconds": 60})
            self.assertEqual([r["id"] for r in await self.service.list({"sessionId": "root"})],
                             [record["id"]])
            catalog = await self.service.catalog()
            self.assertEqual(catalog[0]["status"], "active")
            self.assertEqual(catalog[0]["sessionId"], "root")
            history = await self.service.history(
                {"sessionId": "root", "id": record["id"], "limit": 10})
            self.assertEqual(history["records"], [])
            self.assertEqual(history["retention"], {"days": 30, "records": 200})
            updated = await self.service.update(
                {"sessionId": "root", "id": record["id"], "expected": record,
                 "prompt": "新"})
            self.assertTrue(updated["updated"])
            self.assertEqual(updated["record"]["scheduledAt"], record["scheduledAt"])
            conflict = await self.service.update(
                {"sessionId": "root", "id": record["id"], "expected": record,
                 "prompt": "x"})
            self.assertEqual(conflict["code"], "schedule_conflict")
            self.assertEqual(await self.service.delete(
                {"sessionId": "root", "id": record["id"]}), {"id": record["id"], "deleted": True})
            self.assertEqual(await self.service.delete(
                {"sessionId": "root", "id": record["id"]}),
                {"id": record["id"], "deleted": False, "code": "schedule_not_found"})
        asyncio.run(run())

    def test_history_lookup_failures_and_limit(self):
        async def run():
            record = await self.service.create(
                "root", {"prompt": "p", "title": "p", "after_seconds": 60})
            self.assertEqual(await self.service.history(
                {"sessionId": "wrong", "id": record["id"], "limit": 1}),
                {"id": record["id"], "code": "schedule_not_found"})
            self.assertEqual(await self.service.history(
                {"sessionId": "root", "id": record["id"], "limit": 1, "before": "unknown"}),
                {"id": record["id"], "code": "delivery_cursor_not_found"})
            for limit in (0, -1, 101, 1.5, None):
                with self.assertRaises(ScheduleInputError):
                    await self.service.history(
                        {"sessionId": "root", "id": record["id"], "limit": limit})
        asyncio.run(run())

    def test_update_ended(self):
        async def run():
            record = await self.service.create(
                "root", {"prompt": "p", "title": "p", "after_seconds": 60})
            await self.service._table.put(record["id"], {
                **self.service._table.get(record["id"]), "status": "inactive"})
            result = await self.service.update(
                {"sessionId": "root", "id": record["id"], "expected": record, "prompt": "x"})
            self.assertEqual(result, {"id": record["id"], "updated": False, "code": "schedule_ended"})
        asyncio.run(run())

    def test_config_bounds(self):
        from miniharness.schedule.service import ScheduleService
        for bad in ({"deliveryHistoryDays": 0}, {"deliveryHistoryDays": 3651},
                    {"deliveryHistoryRecords": 0}, {"deliveryHistoryRecords": 10001}):
            obj = ScheduleService.__new__(ScheduleService)
            with self.assertRaises(ValueError):
                ScheduleService.__init__(obj, self.ctx, bad)


# ===== tools =====

class ToolsTest(ServiceHarness):
    def _call(self, name, args):
        return self.agent.tools.resolve(name).execute(args, ToolExec(agent=self.agent))

    def test_create_list_delete(self):
        async def run():
            created = await self._call("schedule_create",
                                       {"prompt": "喝水", "title": "喝水", "after_seconds": 60})
            self.assertEqual(created["kind"], "after")
            self.assertEqual(created["deliveryMode"], "host")
            listed = await self._call("schedule_list", {})
            self.assertEqual([v["id"] for v in listed], [created["id"]])
            deleted = await self._call("schedule_delete", {"id": created["id"]})
            self.assertTrue(deleted["deleted"])
            missing = await self._call("schedule_delete", {"id": "schedule-none"})
            self.assertEqual(missing, {"id": "schedule-none", "deleted": False,
                                       "code": "schedule_not_found"})
        asyncio.run(run())

    def test_selector_and_title_validation(self):
        async def run():
            result = await self._call("schedule_create",
                                      {"prompt": "x", "title": "x", "after_seconds": 1,
                                       "every_seconds": 300})
            self.assertEqual(result["code"], "invalid_selector")
            result = await self._call("schedule_create", {"prompt": "x", "after_seconds": 1})
            self.assertEqual(result["code"], "invalid_prompt")
            result = await self._call("schedule_create",
                                      {"prompt": "x", "title": "x"})
            self.assertEqual(result["code"], "invalid_selector")
            result = await self._call("schedule_create",
                                      {"prompt": "x", "title": "x", "every_seconds": 1})
            self.assertEqual(result["code"], "frequency_too_high")
        asyncio.run(run())

    def test_update_compare_and_update(self):
        async def run():
            created = await self._call("schedule_create",
                                       {"prompt": "p", "title": "t", "every_seconds": 300})
            updated = await self._call("schedule_update",
                                       {"id": created["id"], "title": "renamed"})
            self.assertEqual(updated["title"], "renamed")
            self.assertEqual(updated["scheduledAt"], created["scheduledAt"])
            ended = await self._call("schedule_update",
                                     {"id": "schedule-nope", "title": "x"})
            self.assertEqual(ended["code"], "schedule_not_found")
        asyncio.run(run())

    def test_render_is_compact_json(self):
        async def run():
            created = await self._call("schedule_create",
                                       {"prompt": "hello world", "title": "t",
                                        "after_seconds": 60})
            tool = self.agent.tools.resolve("schedule_create")
            rendered = call_render(tool, {"prompt": "x", "title": "t",
                                          "after_seconds": 60}, created)
            self.assertIn('"prompt":"hello world"', rendered[0]["text"])
            self.assertNotIn(", ", rendered[0]["text"])
        asyncio.run(run())


# ===== runtime =====

class RuntimeHarness(ServiceHarness):
    def _past(self, record, seconds_ago=5):
        now = _ms_local()
        return {**record, "scheduledAt": _iso(now - seconds_ago * 1000)}


def _ms_local():
    import time
    return int(time.time() * 1000)


class RuntimeTest(RuntimeHarness):
    def test_one_shot_delivers_and_retires(self):
        async def run():
            await self.service._ensure_domain()
            record = self._past(create_after_schedule_record(
                "schedule-1", "提醒喝水", 60, _ms_local(), "喝水"))
            await self.service._table.put("schedule-1", {
                "sessionId": "root", "record": record, "status": "active",
                "deliveryHistory": {"records": [], "earlierRecordsUnavailable": False}})
            self.service._runtime.request_drive()
            await asyncio.sleep(0.1)
            self.assertEqual(len(self.followups), 1)
            text = self.followups[0]["content"][0]["text"]
            self.assertIn(SCHEDULED_MESSAGE_FRAMING, text)
            self.assertIn("提醒喝水", text)
            task = self.service._table.get("schedule-1")
            self.assertEqual(task["status"], "inactive")
            self.assertEqual(len(task["deliveryHistory"]["records"]), 1)
            self.assertEqual(task["deliveryHistory"]["records"][0]["messageId"],
                             self.followups[0]["id"])
            await asyncio.sleep(0.1)
            self.assertEqual(len(self.followups), 1)
        asyncio.run(run())

    def test_recurring_tasks_share_one_batch(self):
        async def run():
            await self.service._ensure_domain()
            for id_, prompt in (("schedule-1", "甲"), ("schedule-2", "乙")):
                record = self._past(create_every_schedule_record(
                    id_, prompt, 60, _ms_local(), id_))
                await self.service._table.put(id_, {
                    "sessionId": "root", "record": record, "status": "active",
                    "deliveryHistory": {"records": [], "earlierRecordsUnavailable": False}})
            self.service._runtime.request_drive()
            await asyncio.sleep(0.1)
            self.assertEqual(len(self.followups), 1)
            text = self.followups[0]["content"][0]["text"]
            self.assertIn("[SCHEDULE REMINDER BATCH]", text)
            self.assertIn("甲", text)
            self.assertIn("乙", text)
            for id_ in ("schedule-1", "schedule-2"):
                task = self.service._table.get(id_)
                self.assertEqual(task["status"], "active")
                self.assertEqual(len(task["deliveryHistory"]["records"]), 1)
                self.assertEqual(task["deliveryHistory"]["records"][0]["messageId"],
                                 self.followups[0]["id"])
        asyncio.run(run())

    def test_flush_failure_leaves_task_uncommitted(self):
        async def run():
            await self.service._ensure_domain()
            record = self._past(create_after_schedule_record(
                "schedule-1", "p", 60, _ms_local(), "t"))
            await self.service._table.put("schedule-1", {
                "sessionId": "root", "record": record, "status": "active",
                "deliveryHistory": {"records": [], "earlierRecordsUnavailable": False}})
            # Force the persistence barrier to report failure; nothing may commit.
            self.service._runtime._flush = _false_flush
            self.service._runtime.request_drive()
            await asyncio.sleep(0.1)
            task = self.service._table.get("schedule-1")
            self.assertEqual(task["status"], "active")
            self.assertEqual(len(task["deliveryHistory"]["records"]), 0)
        asyncio.run(run())

    def test_dispose_stops_timer(self):
        async def run():
            await self.service._ensure_domain()
            record = create_after_schedule_record(
                "schedule-1", "p", 60, _ms_local(), "t")
            record = {**record, "scheduledAt": _iso(_ms_local() + 60_000)}
            await self.service._table.put("schedule-1", {
                "sessionId": "root", "record": record, "status": "active"})
            self.service._runtime.request_drive()
            await asyncio.sleep(0.02)
            await self.service._runtime.dispose()
            await asyncio.sleep(0.05)
            self.assertEqual(self.followups, [])
        asyncio.run(run())


async def _false_flush(agent):
    return False


if __name__ == "__main__":
    unittest.main()
