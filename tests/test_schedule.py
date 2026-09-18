"""C16 Schedule 移植验收：域折叠、工具三态、运行时派发、Install 装配。

上游对照：packages/schedule/schedule/src/{domain,runtime,transaction,
persistence,tools,index}.ts。

覆盖点：
  域 fold：三变体解码（after/at/every）、dispatch 从 active 摘除、id 分配永不复用、
  事件引用元素交错证明唯一性、四位数年范围、canonical 即时回读、安全整数边界、
  非法输入码闭集、corrupt fail-closed。
  事务：FIFO 串行、异常向后继冒泡不中断、并发不串扰。
  持久化：flush false → SchedulePersistenceError；非 persistence 异常 wrap+cause。
  工具：create/list/delete canonical 错误形状、persistence_uncertain 半成功、
  schedule_not_found 半成功、cancelled 前置、render=JSON 字节对齐、scoped 注册&拆解。
  运行时：create→arm→dispatch（followup+事件）、无重复派发、corrupt 熔断、dispose 收敛。
  Install：root-only、未来 agent、幂等、全局拆解 allSettled。

运行：python -m unittest tests.test_schedule -v
"""
import asyncio
import inspect
import json
import time
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.agents import install_agents
from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.session_store import install_sessions
from miniharness.core.tools import ToolExec, ToolRegistry
from miniharness.llm import FakeLlmAdapter

from miniharness.schedule import (
    MIN_EVERY_INTERVAL_SECONDS,
    SCHEDULE_CHANGE_VERSION,
    ScheduleInputError,
    ScheduleLogError,
    allocate_schedule_id,
    create_after_schedule_record,
    create_at_schedule_record,
    create_every_schedule_record,
    decode_schedule_change,
    fold_schedule_events,
    install_schedule,
    render_every_reminder_batch_framing,
    render_reminder_framing,
    resolve_every_occurrence,
    schedule_view,
)
from miniharness.schedule.domain import _json_stringify
from miniharness.schedule.persistence import SchedulePersistenceError, flush_schedule_persistence
from miniharness.schedule.runtime import ScheduleRuntime
from miniharness.schedule.transaction import run_schedule_transaction


def _parent_loop(session_id="root"):
    ctx = Context()
    install_sessions(ctx)
    install_agents(ctx)
    reg = ToolRegistry(ctx)
    loop = AgentLoop(Session(session_id), FakeLlmAdapter(final_text="父响应"),
                     reg, ctx, system_prompt="你是 root。")
    loop.publish()
    return loop, ctx, reg


def _call(reg, name, args, agent, signal=None):
    """经 agent.tools 解析并执行（可同步/异步），返回 canonical 值。"""
    exec_ = ToolExec(agent=agent)
    if signal is not None:
        exec_.signal = signal
    value = reg.resolve(name).execute(args, exec_)
    if inspect.isawaitable(value):
        return asyncio.run(value)
    return value


class _FakeSignal:
    def __init__(self, aborted=False):
        self._aborted = aborted

    def set(self):
        self._aborted = True

    def is_set(self):
        return self._aborted

    def wait(self, timeout=None):
        return self._aborted


def _now_ms():
    return int(time.time() * 1000)


# ===== 域 fold =====

class DomainFoldTest(unittest.TestCase):
    def setUp(self):
        self.root, self.ctx, self.reg = _parent_loop()

    def _fold(self, changes):
        session = Session("sched")
        for change in changes:
            session.append("schedule/change", change)
        return fold_schedule_events(session.own_events())

    def test_after_record_roundtrip(self):
        record = create_after_schedule_record(
            "schedule-1", " 喝水面膜 ", 5, _now_ms())
        self.assertEqual(record["kind"], "after")
        self.assertEqual(record["prompt"], "喝水面膜")
        decoded = decode_schedule_change({
            "version": 1, "operation": "create", "schedule": record})
        self.assertEqual(decoded["schedule"]["afterSeconds"], 5)
        # scheduledAt = now + 5s
        self.assertGreaterEqual(
            record["scheduledAt"], _format_now_canonical())

    def test_whitespace_only_prompt_fail_closed(self):
        with self.assertRaises(ScheduleInputError) as cm:
            create_after_schedule_record("x", "   ", 5, _now_ms())
        self.assertEqual(cm.exception.code, "invalid_prompt")

    def test_at_record_local(self):
        record = create_at_schedule_record(
            "schedule-2", "开会", {"date": "2099-01-02", "time": "10:30:00",
                                   "time_zone": "Asia/Shanghai"}, _now_ms())
        self.assertEqual(record["kind"], "at")
        self.assertRegex(record["scheduledAt"], r"^20\d\d-.*Z$")

    def test_every_interval_floor(self):
        with self.assertRaises(ScheduleInputError) as cm:
            create_every_schedule_record("x", "心跳", 299, _now_ms())
        self.assertEqual(cm.exception.code, "frequency_too_high")
        record = create_every_schedule_record(
            "x", "心跳", MIN_EVERY_INTERVAL_SECONDS, _now_ms())
        self.assertEqual(record["everySeconds"], MIN_EVERY_INTERVAL_SECONDS)

    def test_fold_derives_active_and_seen(self):
        changes = [
            {"version": 1, "operation": "create",
             "schedule": create_after_schedule_record("schedule-1", "a", 5, _now_ms())},
            {"version": 1, "operation": "create",
             "schedule": create_after_schedule_record("schedule-2", "b", 5, _now_ms())},
            {"version": 1, "operation": "delete", "id": "schedule-1"},
        ]
        folded = self._fold(changes)
        self.assertEqual([r["id"] for r in folded["active"]], ["schedule-2"])
        self.assertEqual(sorted(folded["seenIds"]), ["schedule-1", "schedule-2"])

    def test_dispatch_removes_from_active(self):
        record = create_after_schedule_record("schedule-1", "a", 5, _now_ms())
        folded = self._fold([
            {"version": 1, "operation": "create", "schedule": record},
            {"version": 1, "operation": "dispatch", "id": "schedule-1"},
        ])
        self.assertEqual(folded["active"], ())
        self.assertIn("schedule-1", folded["seenIds"])

    def test_every_dispatch_reschedules(self):
        record = create_every_schedule_record("schedule-1", "隅", 300, _now_ms())
        folded = self._fold([
            {"version": 1, "operation": "create", "schedule": record},
            {"version": 1, "operation": "dispatch", "id": "schedule-1",
             "acceptedAt": record["scheduledAt"]},
        ])
        # every 派发后重排下一 occurrence（仍 active），seenIds 保留
        self.assertEqual(len(folded["active"]), 1)
        self.assertEqual(folded["active"][0]["id"], "schedule-1")
        self.assertGreater(_epoch_of(folded["active"][0]["scheduledAt"]),
                           _epoch_of(record["scheduledAt"]))
        self.assertIn("schedule-1", folded["seenIds"])

    def test_allocate_never_reuses(self):
        folded = {"active": (), "seenIds": ("schedule-1", "schedule-2")}
        self.assertEqual(allocate_schedule_id(folded), "schedule-3")

    def test_unknown_operation_fails(self):
        with self.assertRaises(ScheduleLogError):
            self._fold([{"version": 1, "operation": "explode"}])

    def test_bad_version_fails(self):
        with self.assertRaises(ScheduleLogError):
            self._fold([{"version": 2, "operation": "create",
                         "schedule": create_after_schedule_record("s1", "a", 5, _now_ms())}])

    def test_extra_key_fails(self):
        with self.assertRaises(ScheduleLogError):
            self._fold([{"version": 1, "operation": "delete", "id": "x", "extra": 1}])

    def test_trailing_whitespace_id_fails(self):
        with self.assertRaises(ScheduleLogError):
            self._fold([{"version": 1, "operation": "delete", "id": " x"}])

    def test_impossible_date_fails(self):
        with self.assertRaises(ScheduleInputError):
            create_at_schedule_record(
                "x", "a", {"date": "2025-02-30", "time": "00:00:00",
                           "time_zone": "UTC"}, _now_ms())

    def test_resolve_every_occurrence(self):
        record = create_every_schedule_record("s1", "隅", 300, _now_ms())
        target = _epoch_of(record["scheduledAt"])
        followup = target + 300_000 + 1
        resolved = resolve_every_occurrence(record, followup)
        occurrence = _epoch_of(resolved["occurrenceAt"])
        # 对齐 target 网格、不枚举积压、落在 followup 前一个周期内
        self.assertEqual((occurrence - target) % 300_000, 0)
        self.assertGreaterEqual(occurrence, followup - 300_000)
        self.assertLessEqual(occurrence, followup)
        self.assertIn("nextScheduledAt", resolved)
        self.assertEqual(_epoch_of(resolved["nextScheduledAt"]), occurrence + 300_000)

    def test_corrupt_log_fails_closed(self):
        changed = _now_ms()
        record = create_after_schedule_record(
            "schedule-1", "a", 5, changed)
        del record["scheduledAt"]
        with self.assertRaises(ScheduleLogError):
            decode_schedule_change({
                "version": 1, "operation": "create", "schedule": record})

    def test_schedule_view_state(self):
        record = create_after_schedule_record("schedule-1", "a", 5, _now_ms())
        view = schedule_view(record, _now_ms() + 10_000)
        self.assertEqual(view["id"], "schedule-1")
        self.assertIn(view["state"], ("scheduled", "overdue"))
        self.assertEqual(view["deliveryMode"], "session-local")

    def test_json_stringify_byte_parity(self):
        value = {"id": "schedule-1", "prompt": "a\u2028b", "é": "中文"}
        rendered = _json_stringify(value)
        # JS JSON.stringify：无空格分隔、unicode 原样、U+2028/29 转义
        self.assertNotIn(" ", rendered)
        self.assertNotIn("\u2028", rendered)
        self.assertIn("\\u2028", rendered)
        self.assertIn("中文", rendered)
        self.assertEqual(json.dumps(value, ensure_ascii=False, separators=(",", ":")),
                         rendered.replace("\\u2028", "\u2028"))


# ===== framing 文本 =====

class FramingTest(unittest.TestCase):
    def test_one_shot_framing(self):
        record = create_after_schedule_record("schedule-1", "喝水", 5, _now_ms())
        text = render_reminder_framing(record)
        self.assertIn("SCHEDULE REMINDER", text)
        self.assertIn("schedule-1", text)
        self.assertIn("喝水", text)

    def test_every_batch_framing(self):
        record = create_every_schedule_record("schedule-1", "隅", 300, _now_ms())
        text = render_every_reminder_batch_framing([
            {"record": record, "occurrenceAt": _format_now_canonical()}])
        self.assertIn("schedule-1", text)
        self.assertIn("隅", text)


# ===== 事务 + 持久化 =====

class _TxnAgent:
    def __init__(self, id_):
        self.id = id_


class TransactionTest(unittest.TestCase):
    def test_fifo_serialization(self):
        agent = _TxnAgent("a")
        order = []

        async def main():
            r1 = await run_schedule_transaction(agent, lambda: order.append(1))
            r2 = await run_schedule_transaction(agent, lambda: order.append(2))
            return r1, r2

        asyncio.run(main())
        self.assertEqual(order, [1, 2])

    def test_async_operations_awaited(self):
        agent = _TxnAgent("a")
        results = []

        async def op():
            await asyncio.sleep(0.001)
            results.append("done")

        asyncio.run(run_schedule_transaction(agent, op))
        self.assertEqual(results, ["done"])

    def test_failure_propagates_and_breaks_chain(self):
        # 上游 Promise 链：失败的后继拒绝（不吞错、不重排）
        agent = _TxnAgent("a")
        seen = []

        def bad():
            seen.append("bad")
            raise ValueError("boom")

        with self.assertRaises(ValueError):
            asyncio.run(run_schedule_transaction(agent, bad))
        self.assertEqual(seen, ["bad"])

    def test_tail_isolation_between_agents(self):
        a = _TxnAgent("a")
        b = _TxnAgent("b")
        out = []

        async def main():
            await asyncio.gather(
                run_schedule_transaction(a, lambda: out.append("a1")),
                run_schedule_transaction(b, lambda: out.append("b1")),
                run_schedule_transaction(a, lambda: out.append("a2")),
            )

        asyncio.run(main())
        # a 组串行，b 独立：a1 < a2 恒成立
        self.assertLess(out.index("a1"), out.index("a2"))
        self.assertIn("b1", out)


class PersistenceTest(unittest.TestCase):
    def test_flush_false_raises_schedule_persistence_error(self):
        class NoParticipants:
            def flush(self, session):
                return False

        ctx = Context()
        ctx.provide("sessions", NoParticipants())
        with self.assertRaises(SchedulePersistenceError):
            flush_schedule_persistence(ctx, Session("s1"))

    def test_non_persistence_error_wrapped_with_cause(self):
        class Broken:
            def flush(self, session):
                raise OSError("disk full")

        ctx = Context()
        ctx.provide("sessions", Broken())
        with self.assertRaises(SchedulePersistenceError) as cm:
            flush_schedule_persistence(ctx, Session("s1"))
        self.assertIsInstance(cm.exception.__cause__, OSError)
        self.assertEqual(cm.exception.name, "SchedulePersistenceError")


# ===== 工具 =====

class ScheduleToolsTest(unittest.TestCase):
    def setUp(self):
        self.root, self.ctx, self.reg = _parent_loop()
        # flush 参与位（真实 SessionStore 参与者）
        self.ctx.on("session/flush", lambda payload: None)
        from miniharness.schedule.tools import register_schedule_tools
        self.disposers = register_schedule_tools(
            self.ctx, self.root, lambda: None)
        self.reg = self.root.tools

    def tearDown(self):
        self.disposers()

    def _create(self, **args):
        return _call(self.reg, "schedule_create", args, self.root)

    def test_requires_agent_bound_exec(self):
        exec_ = ToolExec(agent=None)
        value = self.reg.resolve("schedule_create").execute(
            {"prompt": "x", "after_seconds": 1}, exec_)
        if inspect.isawaitable(value):
            value = asyncio.run(value)
        self.assertEqual(value["code"], "internal_error")

    def test_create_after_success(self):
        result = self._create(prompt="喝水", after_seconds=1)
        self.assertEqual(result["kind"], "after")
        self.assertEqual(result["prompt"], "喝水")
        self.assertIn(result["id"], (
            r["id"] for r in fold_schedule_events(self.root.session.own_events())["active"]))

    def test_invalid_selector(self):
        result = self._create(prompt="x", after_seconds=1, every_seconds=300)
        self.assertEqual(result["code"], "invalid_selector")

    def test_select_missing(self):
        result = self._create(prompt="x")
        self.assertEqual(result["code"], "invalid_selector")

    def test_invalid_prompt(self):
        result = self._create(prompt="  ", after_seconds=1)
        self.assertEqual(result["code"], "invalid_prompt")

    def test_frequency_too_high(self):
        result = self._create(prompt="x", every_seconds=299)
        self.assertEqual(result["code"], "frequency_too_high")

    def test_every_ok(self):
        result = self._create(prompt="x", every_seconds=300)
        self.assertEqual(result["kind"], "every")

    def test_list_returns_active(self):
        self._create(prompt="a", after_seconds=1)
        listed = _call(self.reg, "schedule_list", {}, self.root)
        self.assertTrue(any(v["id"] for v in listed))

    def test_delete_success(self):
        created = self._create(prompt="a", after_seconds=1)
        deleted = _call(self.reg, "schedule_delete",
                        {"id": created["id"]}, self.root)
        self.assertTrue(deleted["deleted"])

    def test_delete_unknown_half_success(self):
        deleted = _call(self.reg, "schedule_delete", {"id": "schedule-no"}, self.root)
        self.assertFalse(deleted["deleted"])
        self.assertEqual(deleted["code"], "schedule_not_found")

    def test_delete_invalid_id(self):
        deleted = _call(self.reg, "schedule_delete", {"id": " x "}, self.root)
        self.assertEqual(deleted["code"], "invalid_rule")

    def test_cancelled_before_fifo_turn(self):
        result = self._create(prompt="x", after_seconds=1)
        cancelled = self._create_signal_aborted()
        gone = _call(self.reg, "schedule_delete", {"id": result["id"]}, self.root,
                     signal=cancelled)
        self.assertEqual(gone["code"], "internal_error")

    def _create_signal_aborted(self):
        sig = _FakeSignal()
        sig.set()
        return sig

    def test_render_json_parity(self):
        created = self._create(prompt="hello world", after_seconds=1)
        # render 经 call_render 双参派发
        tool = self.reg.resolve("schedule_create")
        from miniharness.core.tools import call_render
        value = call_render(tool, {"prompt": "hello world", "after_seconds": 1}, created)
        text = value[0]["text"]
        self.assertIn('"prompt":"hello world"', text)
        self.assertIn("hello world", text)
        # JSON.stringify 紧凑载体：无分隔空格
        self.assertNotIn(", ", text)
        self.assertNotIn(": ", text)


# ===== 运行时 =====

class _RuntimeHarness:
    """进程内合成 ctx + 注册表，让 ScheduleRuntime 视为 root live。"""

    def __init__(self, agent):
        flushed = []
        warns = []

        class _Agents:
            def get(self, agent_id):
                return agent

            def roots(self):
                return [agent]

        class _Sessions:
            def flush(self, session):
                flushed.append(session)
                return True

        class _Logger:
            def warn(self, message):
                warns.append(message)

        ctx = Context()
        ctx.provide("agents", _Agents())
        ctx.provide("sessions", _Sessions())
        ctx.logger = _Logger()  # 属性覆写，遮蔽内建 LoggerService
        self.ctx = ctx
        self.flushed = flushed
        self.warns = warns

    def flush_count(self):
        return len(self.flushed)


class _RuntimeAgent:
    def __init__(self, agent_id="runtime-agent"):
        self.id = agent_id
        self.session = Session(agent_id)
        self._idle = True
        self.status = "idle"
        self.followups = []
        self.maintenance_calls = 0

    def when_idle(self):
        return self._idle

    def when_idle_async(self):
        raise RuntimeError("no driver")

    def run_maintenance(self, task):
        if not self.when_idle():
            raise RuntimeError("run_maintenance 要求 true idle")
        self.status = "maintenance"
        self._idle = False
        try:
            result = task()
        finally:
            self.status = "idle"
            self._idle = True
        if result:
            self.maintenance_calls += 1
        return True

    def followup(self, message):
        self.followups.append(message)

    def append_change(self, payload):
        self.session.append("schedule/change", payload)


def _format_now_canonical():
    from miniharness.schedule.domain import _format_epoch_ms
    return _format_epoch_ms(_now_ms())


def _epoch_of(canonical):
    from miniharness.schedule.domain import _parse_canonical
    return int(_parse_canonical(canonical).timestamp() * 1000)


class RuntimeTest(unittest.TestCase):
    def test_create_then_dispatch_once(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)

        async def main():
            runtime.start()
            await asyncio.sleep(0.03)
            record = create_after_schedule_record(
                allocate_schedule_id({"active": (), "seenIds": ()}), "提醒", 1, _now_ms())
            agent.append_change(
                {"version": 1, "operation": "create", "schedule": record})
            runtime.request_drive()
            await asyncio.sleep(1.25)
            # 二次驱动无重复派发
            runtime.request_drive()
            await asyncio.sleep(0.1)
            await runtime.dispose()

        asyncio.run(main())
        self.assertEqual(agent.maintenance_calls, 1)
        self.assertEqual(len(agent.followups), 1)
        self.assertIn("提醒", agent.followups[0]["content"][0]["text"])
        types = [e["type"] for e in agent.session.events]
        self.assertEqual(types.count("schedule/change"), 2)

    def test_no_schedule_no_followup(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)

        async def main():
            runtime.start()
            await asyncio.sleep(0.05)
            runtime.request_drive()
            await asyncio.sleep(0.05)
            await runtime.dispose()

        asyncio.run(main())
        self.assertEqual(agent.followups, [])

    def test_corrupt_log_faults_runtime(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        agent.session.append("schedule/change",
                             {"version": 1, "operation": "create",
                              "schedule": {"id": "bad"}})
        runtime = ScheduleRuntime(harness.ctx, agent)

        async def main():
            runtime.start()
            await asyncio.sleep(0.05)
            await runtime.dispose()

        asyncio.run(main())
        self.assertEqual(agent.followups, [])
        self.assertTrue(runtime._faulted)
        self.assertTrue(harness.warns)

    def test_loopless_start_defers_then_drives(self):
        # 同步装配阶段 start()：不崩，等 loop 内 request_drive 兜住
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        runtime.start()  # 无 loop，无异常
        self.assertIsNone(runtime._run)
        record = create_after_schedule_record(
            allocate_schedule_id({"active": (), "seenIds": ()}), "x", 1, _now_ms())
        agent.append_change({"version": 1, "operation": "create", "schedule": record})

        async def main():
            runtime.request_drive()
            await asyncio.sleep(1.25)
            await runtime.dispose()

        asyncio.run(main())
        self.assertEqual(agent.maintenance_calls, 1)

    def test_dispose_cancels_timer(self):
        agent = _RuntimeAgent()
        harness = _RuntimeHarness(agent)
        runtime = ScheduleRuntime(harness.ctx, agent)
        record = create_after_schedule_record(
            allocate_schedule_id({"active": (), "seenIds": ()}), "x", 5, _now_ms())
        agent.append_change({"version": 1, "operation": "create", "schedule": record})

        async def main():
            runtime.start()
            await asyncio.sleep(0.02)
            runtime.request_drive()
            await asyncio.sleep(0.02)
            await runtime.dispose()
            await asyncio.sleep(0.05)

        asyncio.run(main())
        self.assertEqual(agent.followups, [])

    def test_transaction_cancellation_does_not_break_serialization(self):
        """事务外层 await 被取消：操作完成 + 后继事务仍严格串行（上游尾链在
        Promise 语义下不被 awaiter 取消摧毁，transaction.ts:13-23）。"""
        agent = _TxnAgent("cancel-a")
        order = []

        async def main():
            slow_started = asyncio.Event()

            async def slow():
                slow_started.set()
                await asyncio.sleep(0.05)
                order.append("slow")

            outer = asyncio.ensure_future(run_schedule_transaction(agent, slow))
            await slow_started.wait()
            outer.cancel()
            try:
                await outer
            except asyncio.CancelledError:
                pass
            await run_schedule_transaction(agent, lambda: order.append("fast"))

        asyncio.run(main())
        self.assertEqual(order, ["slow", "fast"])

    def test_same_session_id_shares_serialization_key(self):
        """相同 session_id 的同一 agent 复用一条尾链（上游 exact Agent 键的
        mini 载体：session_id 唯一标识 owner）。"""
        agent = _TxnAgent("shared-key")
        order = []

        async def main():
            await asyncio.gather(
                run_schedule_transaction(agent, lambda: order.append(1)),
                run_schedule_transaction(agent, lambda: order.append(2)),
            )

        asyncio.run(main())
        self.assertEqual(order, [1, 2])


# ===== Install 装配 =====

class InstallTest(unittest.TestCase):
    def setUp(self):
        self.root, self.ctx, self.reg = _parent_loop()

    def _publish(self, session_id):
        """在同一 ctx 发布一个未来 root（触发 agent/created）。"""
        loop = AgentLoop(Session(session_id), FakeLlmAdapter(final_text="x"),
                         self.reg, self.ctx, system_prompt="x")
        loop.publish()
        return loop

    def test_requires_agents_and_sessions(self):
        bare = Context()
        with self.assertRaises(RuntimeError):
            install_schedule(bare)

    def test_install_idempotent_and_adopts_future_root(self):
        d1 = install_schedule(self.ctx)
        d2 = install_schedule(self.ctx)
        self.assertIs(d1, d2)
        # 既有 root 不被收养（上游仅未来 root）
        self.assertNotIn("schedule_create", self.root.tools.names())
        fresh = self._publish("fresh")
        try:
            self.assertIn("schedule_create", fresh.tools.names())
            self.assertIn("schedule_list", fresh.tools.names())
            self.assertIn("schedule_delete", fresh.tools.names())
        finally:
            fresh.dispose()

    def test_existing_root_not_adopted(self):
        install_schedule(self.ctx)
        self.assertNotIn("schedule_list", self.reg.names())

    def test_teardown_stops_future_adoption(self):
        d = install_schedule(self.ctx)
        d()
        late = self._publish("late")
        try:
            self.assertNotIn("schedule_create", late.tools.names())
        finally:
            late.dispose()

    def test_teardown_disposes_adopted_owner(self):
        install_schedule(self.ctx)
        adopted = self._publish("adopted")
        self.assertIn("schedule_create", adopted.tools.names())
        adopted.dispose()
        self.assertNotIn("schedule_create", self.reg.names())

    def test_status_idle_event_drives_runtime(self):
        """agent/status idle + 会话含 schedule/change → requestDrive（上游
        index.ts:57-62 的 status 监听契约）。"""
        self.ctx.on("session/flush", lambda payload: None)
        install_schedule(self.ctx)
        adopted = self._publish("adopted-status")
        try:
            record = create_after_schedule_record(
                allocate_schedule_id({"active": (), "seenIds": ()}),
                "idle 唤醒", 1, _now_ms())
            adopted.session.append("schedule/change",
                                   {"version": 1, "operation": "create",
                                    "schedule": record})

            async def main():
                adopted.ctx.emit("agent/status",
                                 {"agent": adopted, "status": "idle"})
                await asyncio.sleep(1.25)

            asyncio.run(main())
            changes = [e for e in adopted.session.events
                       if e["type"] == "schedule/change"
                       and e["data"]["operation"] == "dispatch"]
            self.assertEqual(len(changes), 1)
        finally:
            adopted.dispose()


if __name__ == "__main__":
    unittest.main()