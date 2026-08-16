"""A4 验收：miniharness.jobs 后台作业家族（对齐 packages/jobs/ 契约面）。

覆盖：注册表语义（owner 栅栏 / 结算 first-wins / 并发上限 / teardown）、
三工具（job_output/job_list/job_kill）、完成 notice 投递（wakeup/quiet 与
预算）、字节封顶、装配幂等。461+ 基线之外的独立测试文件。
"""
import threading
import time
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.tools import ToolRegistry
from miniharness.jobs import (
    DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER,
    JobDoneBox,
    LocalJobRegistry,
    fit_completion_notice,
    fit_with_suffix,
    install_jobs,
    public_job,
    register_job_tools,
    status_line,
    validate_job_id,
)
from miniharness.llm import FakeLlmAdapter


class _TextAdapter(FakeLlmAdapter):
    def __init__(self, text: str):
        super().__init__(final_text=text)


def _fake_owner(owner_id: str, ctx: Context):
    """注册表只按 id + ctx duck-type 栅栏的 owner 替身（记录送达路径）。"""
    owner = type("Owner", (), {})()
    owner.id = owner_id
    owner.ctx = ctx
    owner.status = "idle"
    owner.delivered: list[tuple[str, str]] = []      # (kind, notice)
    owner.wakes = 0
    owner.claimed_hooks = []
    owner.armed = False

    def followup(content, source):
        owner.wakes += 1
        owner.delivered.append(("wakeup", content))

    def inject(content, source):
        owner.delivered.append(("inject", content))

    def on_inbox_claimed(hook):
        owner.claimed_hooks.append(hook)
        return lambda: owner.claimed_hooks.remove(hook) if hook in owner.claimed_hooks else None

    owner.followup = followup
    owner.inject = inject
    owner.on_inbox_claimed = on_inbox_claimed
    return owner


def _finish_async(box: JobDoneBox, delay: float, outcome: dict) -> threading.Thread:
    """后台线程在 delay 秒后结算（producer 完成线程模拟）。"""
    t = threading.Thread(target=lambda: (time.sleep(delay), box.settle(outcome)), daemon=True)
    t.start()
    return t


class RegistryBasicsTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-registry")
        self.registry = LocalJobRegistry(self.ctx)

    def test_installs_ctx_jobs_service(self):
        self.assertIs(self.ctx.inject("jobs"), self.registry)

    def test_duplicate_provide_fails(self):
        with self.assertRaises(RuntimeError):
            self.ctx.provide("jobs", object())

    def test_install_jobs_idempotent(self):
        self.assertIs(install_jobs(self.ctx), self.registry)
        self.assertIs(install_jobs(self.ctx), self.registry)

    def test_invalid_config_rejected(self):
        with self.assertRaises(ValueError):
            LocalJobRegistry(Context(name="bad"), {"maxConcurrentJobsPerOwner": 0})
        with self.assertRaises(ValueError):
            LocalJobRegistry(Context(name="bad2"), {"maxConcurrentJobsPerOwner": "10"})

    def test_start_requires_controller(self):
        owner = _fake_owner("o1", Context(name="own"))
        with self.assertRaises(RuntimeError):
            self.registry.start({"kind": "bash", "label": "x", "owner": owner,
                                 "run": lambda: {"done": JobDoneBox(), "cancel": lambda r: None}})

    def test_attach_controller_enables_start(self):
        self.registry.attach_controller("test")
        owner = _fake_owner("o1", Context(name="own"))
        tid = self.registry.start({"kind": "bash", "label": "sleep", "owner": owner,
                                   "run": lambda: {"done": JobDoneBox(), "cancel": lambda r: None}})
        self.assertTrue(tid.startswith("bash-"))
        snap = self.registry.get(tid, owner)
        self.assertEqual(snap["status"], "running")
        self.assertEqual(snap["ownerSession"], "o1")
        self.assertNotIn("reported", {})  # reported 恒在快照中

    def test_id_counter_per_kind(self):
        self.registry.attach_controller("test")
        owner = _fake_owner("o1", Context(name="own"))
        run = lambda: {"done": JobDoneBox(), "cancel": lambda r: None}  # noqa: E731
        a = self.registry.start({"kind": "bash", "label": "1", "owner": owner, "run": run})
        b = self.registry.start({"kind": "bash", "label": "2", "owner": owner, "run": run})
        c = self.registry.start({"kind": "subagent", "label": "3", "owner": owner, "run": run})
        self.assertEqual((a, b, c), ("bash-1", "bash-2", "subagent-1"))

    def test_invalid_spec_rejected(self):
        self.registry.attach_controller("test")
        owner = _fake_owner("o1", Context(name="own"))
        run = lambda: {"done": JobDoneBox(), "cancel": lambda r: None}  # noqa: E731
        with self.assertRaises(ValueError):
            self.registry.start({"kind": "", "label": "x", "owner": owner, "run": run})
        with self.assertRaises(ValueError):
            self.registry.start({"kind": "bash", "label": "", "owner": owner, "run": run})
        with self.assertRaises(ValueError):
            self.registry.start({"kind": "bash", "label": "x", "outputLimitBytes": -1,
                                 "owner": owner, "run": run})


class AccessFenceTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-fence")
        self.registry = LocalJobRegistry(self.ctx)
        self.registry.attach_controller("test")
        self.alice = _fake_owner("alice", Context(name="a"))
        self.bob = _fake_owner("bob", Context(name="b"))
        self.run = lambda: {"done": JobDoneBox(), "cancel": lambda r: None}  # noqa: E731

    def test_owned_job_hidden_from_other_session(self):
        tid = self.registry.start({"kind": "bash", "label": "mine", "owner": self.alice,
                                   "run": self.run})
        self.assertEqual([j["id"] for j in self.registry.list(self.bob)], [])
        self.assertEqual([j["id"] for j in self.registry.list(self.alice)], [tid])
        with self.assertRaises(RuntimeError):
            self.registry.get(tid, self.bob)
        with self.assertRaises(RuntimeError):
            self.registry.kill(tid, self.bob)
        with self.assertRaises(RuntimeError):
            self.registry.read(tid, self.bob)

    def test_unowned_job_open_to_all(self):
        tid = self.registry.start({"kind": "bash", "label": "shared", "run": self.run})
        self.assertEqual([j["id"] for j in self.registry.list(self.alice)], [tid])
        self.assertEqual([j["id"] for j in self.registry.list(None)], [tid])
        self.assertIsNotNone(self.registry.get(tid, self.bob))

    def test_agentless_caller_never_matches_owned(self):
        tid = self.registry.start({"kind": "bash", "label": "mine", "owner": self.alice,
                                   "run": self.run})
        with self.assertRaises(RuntimeError):
            self.registry.get(tid, None)

    def test_unknown_job_fails_loud(self):
        with self.assertRaises(RuntimeError):
            self.registry.get("nope-1", self.alice)

    def test_owner_requires_id_and_ctx(self):
        with self.assertRaises(RuntimeError):
            self.registry.start({"kind": "bash", "label": "x", "owner": object(),
                                 "run": self.run})


class SettlementTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-settle")
        self.registry = LocalJobRegistry(self.ctx)
        self.registry.attach_controller("test")
        self.owner = _fake_owner("o1", Context(name="own"))

    def test_completed_settlement(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "s", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "completed"})
        snap = self.registry.get(tid, self.owner)
        self.assertEqual(snap["status"], "completed")
        self.assertIsNotNone(snap["finishedAt"])
        self.assertLessEqual(snap["startedAt"], snap["finishedAt"])

    def test_first_wins_ignores_late_settle(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "s", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "completed"})
        box.settle({"status": "killed"})   # 迟到结算被忽略
        self.assertEqual(self.registry.get(tid, self.owner)["status"], "completed")

    def test_reject_becomes_failed_with_detail(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "s", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.fail(ValueError("boom"))
        snap = self.registry.get(tid, self.owner)
        self.assertEqual(snap["status"], "failed")
        self.assertEqual(snap["detail"], "boom")

    def test_invalid_producer_outcome_is_failed(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "s", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "running"})  # 非终态 outcome → 归一化 failed
        snap = self.registry.get(tid, self.owner)
        self.assertEqual(snap["status"], "failed")
        self.assertIn("invalid outcome", snap["detail"])

    def test_optional_fields_in_snapshot(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "s", "outputLimitBytes": 1024,
                                   "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "completed", "detail": "done", "output": "out"})
        snap = self.registry.get(tid, self.owner)
        self.assertEqual(snap["detail"], "done")
        self.assertEqual(snap["outputLimitBytes"], 1024)

    def test_producer_done_listener_fires_once(self):
        box = JobDoneBox()
        seen = []
        self.registry.on_job_done(lambda snap, owner: seen.append(snap["id"]))
        tid = self.registry.start({"kind": "bash", "label": "s", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "completed"})
        box.settle({"status": "killed"})
        self.assertEqual(seen, [tid])


class KillTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-kill")
        self.registry = LocalJobRegistry(self.ctx)
        self.registry.attach_controller("test")
        self.owner = _fake_owner("o1", Context(name="own"))
        self.cancelled: list[str] = []

    def test_kill_sets_stopping_and_calls_cancel(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "s", "owner": self.owner,
                                   "run": lambda: {"done": box,
                                                   "cancel": lambda r: self.cancelled.append(r)}})
        self.assertEqual(self.registry.kill(tid, self.owner, "no longer needed"), "requested")
        snap = self.registry.get(tid, self.owner)
        self.assertEqual(snap["status"], "stopping")
        self.assertTrue(snap["reported"])
        self.assertEqual(self.cancelled, ["no longer needed"])

    def test_kill_terminal_returns_already_finished_and_marks_reported(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "s", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "completed"})
        self.assertEqual(self.registry.kill(tid, self.owner), "already-finished")
        self.assertTrue(self.registry.get(tid, self.owner)["reported"])

    def test_throwing_cancel_propagates_without_state_change(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "s", "owner": self.owner,
                                   "run": lambda: {"done": box,
                                                   "cancel": lambda r: (_ for _ in ()).throw(
                                                       RuntimeError("cancel denied"))}})
        with self.assertRaises(RuntimeError):
            self.registry.kill(tid, self.owner)
        snap = self.registry.get(tid, self.owner)
        self.assertEqual(snap["status"], "running")  # 状态未被触碰
        self.assertFalse(snap["reported"])


class ReadWaitTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-read")
        self.registry = LocalJobRegistry(self.ctx)
        self.registry.attach_controller("test")
        self.owner = _fake_owner("o1", Context(name="own"))

    def test_stream_job_reads_delta(self):
        chunks: list[str] = []

        def read_output():
            return (chunks.pop(0) if chunks else "")

        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "s", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None,
                                                   "read_output": read_output}})
        chunks.append("alpha")
        self.assertEqual(self.registry.read(tid, self.owner)["text"], "alpha")
        chunks.append("beta")
        self.assertEqual(self.registry.read(tid, self.owner)["text"], "beta")
        self.assertEqual(self.registry.read(tid, self.owner)["text"], "")

    def test_final_output_job_reads_after_settlement(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "subagent", "label": "s", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        self.assertEqual(self.registry.read(tid, self.owner)["text"], "")
        box.settle({"status": "completed", "output": "the answer"})
        self.assertEqual(self.registry.read(tid, self.owner)["text"], "the answer")

    def test_terminal_read_marks_reported(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "s", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "completed", "output": "out"})
        self.registry.read(tid, self.owner)
        self.assertTrue(self.registry.get(tid, self.owner)["reported"])

    def test_wait_returns_after_settlement(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "s", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        _finish_async(box, 0.05, {"status": "completed"})
        snap = self.registry.wait(tid, 2000, self.owner)
        self.assertEqual(snap["status"], "completed")
        self.assertTrue(snap["reported"])  # waiter 消费，抑制 notice

    def test_wait_timeout_returns_running_without_cancelling(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "s", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        snap = self.registry.wait(tid, 50, self.owner)
        self.assertEqual(snap["status"], "running")
        self.assertEqual(self.registry.get(tid, self.owner)["status"], "running")

    def test_wait_aborted_by_signal_raises(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "s", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "completed"})
        self.assertEqual(self.registry.wait(tid, 1000, self.owner).get("status"), "completed")

    def test_wait_invalid_timeout(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "s", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        with self.assertRaises(ValueError):
            self.registry.wait(tid, 0, self.owner)


class ConcurrencyCapTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-cap")
        self.registry = LocalJobRegistry(self.ctx, {"maxConcurrentJobsPerOwner": 2})
        self.registry.attach_controller("test")
        self.owner = _fake_owner("o1", Context(name="own"))

    def test_cap_enforced_per_exact_owner(self):
        run = lambda: {"done": JobDoneBox(), "cancel": lambda r: None}  # noqa: E731
        self.registry.start({"kind": "bash", "label": "1", "owner": self.owner, "run": run})
        self.registry.start({"kind": "bash", "label": "2", "owner": self.owner, "run": run})
        with self.assertRaises(RuntimeError):
            self.registry.start({"kind": "bash", "label": "3", "owner": self.owner, "run": run})

    def test_settlement_frees_capacity(self):
        run = lambda: {"done": JobDoneBox(), "cancel": lambda r: None}  # noqa: E731
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "1", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        self.registry.start({"kind": "bash", "label": "2", "owner": self.owner, "run": run})
        box.settle({"status": "completed"})
        self.registry.start({"kind": "bash", "label": "3", "owner": self.owner, "run": run})

    def test_other_owner_has_own_cap(self):
        run = lambda: {"done": JobDoneBox(), "cancel": lambda r: None}  # noqa: E731
        other = _fake_owner("o2", Context(name="own2"))
        self.registry.start({"kind": "bash", "label": "1", "owner": self.owner, "run": run})
        self.registry.start({"kind": "bash", "label": "2", "owner": self.owner, "run": run})
        self.registry.start({"kind": "bash", "label": "1", "owner": other, "run": run})
        self.registry.start({"kind": "bash", "label": "2", "owner": other, "run": run})
        with self.assertRaises(RuntimeError):
            self.registry.start({"kind": "bash", "label": "3", "owner": other, "run": run})

    def test_unowned_share_one_bucket(self):
        run = lambda: {"done": JobDoneBox(), "cancel": lambda r: None}  # noqa: E731
        self.registry.start({"kind": "bash", "label": "1", "run": run})
        self.registry.start({"kind": "bash", "label": "2", "run": run})
        with self.assertRaises(RuntimeError):
            self.registry.start({"kind": "bash", "label": "3", "run": run})


class TeardownTest(unittest.TestCase):
    def test_owner_dispose_cancels_and_removes(self):
        ctx = Context(name="jobs-td")
        registry = LocalJobRegistry(ctx)
        registry.attach_controller("test")
        owner = _fake_owner("o1", Context(name="own"))
        box = JobDoneBox()
        tid = registry.start({"kind": "bash", "label": "s", "owner": owner,
                              "run": lambda: {"done": box, "cancel": lambda r: None}})
        owner.ctx.dispose()
        self.assertEqual(registry.list(owner), [])
        with self.assertRaises(RuntimeError):
            registry.get(tid, owner)

    def test_teardown_cancel_throw_force_fails(self):
        ctx = Context(name="jobs-td2")
        registry = LocalJobRegistry(ctx)
        registry.attach_controller("test")
        owner = _fake_owner("o1", Context(name="own"))
        box = JobDoneBox()
        seen: list[dict] = []
        registry.on_job_done(lambda snap, o: seen.append(snap))
        tid = registry.start({"kind": "bash", "label": "s", "owner": owner,
                              "run": lambda: {"done": box,
                                              "cancel": lambda r: (_ for _ in ()).throw(
                                                  RuntimeError("no"))}})
        owner.ctx.dispose()
        # teardown cancel 抛错 → force-fail（经 done 监听器可见）→ 记录随后删除
        self.assertEqual([s["status"] for s in seen], ["failed"])
        self.assertIn("orphaned", seen[0]["detail"])
        with self.assertRaises(RuntimeError):
            registry.get(tid, owner)

    def test_owner_cleanup_registered_once(self):
        ctx = Context(name="jobs-td3")
        registry = LocalJobRegistry(ctx)
        registry.attach_controller("test")
        owner = _fake_owner("o1", Context(name="own"))
        run = lambda: {"done": JobDoneBox(), "cancel": lambda r: None}  # noqa: E731
        registry.start({"kind": "bash", "label": "1", "owner": owner, "run": run})
        registry.start({"kind": "bash", "label": "2", "owner": owner, "run": run})
        owner.ctx.dispose()   # 只注册一次 cleanup，不抛
        registry.list(owner)


class FitHelpersTest(unittest.TestCase):
    def test_fit_with_suffix_under_limit(self):
        self.assertEqual(fit_with_suffix("abc", "\n[status: completed]", None, "\n[x]"),
                         "abc\n[status: completed]")

    def test_fit_with_suffix_truncates_head_keeps_tail_and_marker(self):
        body = "x" * 500
        out = fit_with_suffix(body, "\n[status: completed]", 60, "\n[output truncated]")
        self.assertEqual(len(out.encode("utf-8")), 60)
        self.assertTrue(out.endswith("\n[output truncated]\n[status: completed]"))
        self.assertIn("output truncated", out)

    def test_fit_with_suffix_no_utf8_split(self):
        body = "中" * 300
        out = fit_with_suffix(body, "\n[status: completed]", 30, "\n[t]")
        # 裁剪后仍为合法 UTF-8，且字节数不超限
        out.encode("utf-8").decode("utf-8")
        self.assertLessEqual(len(out.encode("utf-8")), 30)

    def test_fit_completion_notice_under_limit(self):
        snap = {"id": "bash-1", "kind": "bash", "label": "sleep", "status": "completed",
                "startedAt": 1, "finishedAt": 2}
        notice = fit_completion_notice(snap)
        self.assertIn("background job bash-1", notice)
        self.assertIn("job_output", notice)

    def test_fit_completion_notice_keeps_id_and_action(self):
        snap = {"id": "bash-1", "kind": "bash", "label": "sleep", "status": "completed",
                "startedAt": 1, "finishedAt": 2, "outputLimitBytes": 80}
        notice = fit_completion_notice(snap)
        self.assertLessEqual(len(notice.encode("utf-8")), 80)
        self.assertIn("background job bash-1", notice)
        self.assertTrue(notice.endswith("\nDone; job_output."))

    def test_public_job_drops_owner_and_reported(self):
        snap = {"id": "bash-1", "kind": "bash", "label": "s", "status": "running",
                "startedAt": 1, "reported": False, "ownerSession": "o1"}
        pub = public_job(snap)
        self.assertNotIn("ownerSession", pub)
        self.assertNotIn("reported", pub)

    def test_validate_job_id(self):
        self.assertEqual(validate_job_id("bash-1"), "bash-1")
        with self.assertRaises(ValueError):
            validate_job_id("")
        with self.assertRaises(ValueError):
            validate_job_id(None)

    def test_status_line_with_detail(self):
        self.assertEqual(status_line({"status": "failed", "detail": "boom"}),
                         "[status: failed, boom]")


class NoticeDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-notice")
        self.registry = install_jobs(self.ctx)
        self.owner = _fake_owner("o1", Context(name="own"))

    def _start_and_settle(self, outcome: dict) -> str:
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle(outcome)
        return tid

    def test_wakeup_delivery_when_idle(self):
        self.owner.status = "idle"
        self._start_and_settle({"status": "completed"})
        self.assertEqual(len(self.owner.delivered), 1)
        kind, notice = self.owner.delivered[0]
        self.assertEqual(kind, "wakeup")
        self.assertIn("background job bash-1", notice)
        self.assertEqual(self.owner.wakes, 1)

    def test_inject_delivery_when_busy(self):
        self.owner.status = "running"
        self._start_and_settle({"status": "killed"})
        self.assertEqual(self.owner.delivered[0][0], "inject")
        self.assertEqual(self.owner.wakes, 0)
        self.assertIn("[status: killed]", self.owner.delivered[0][1])

    def test_quiet_delivery_injects_even_idle(self):
        ctx = Context(name="jobs-quiet")
        registry = LocalJobRegistry(ctx, {"maxConcurrentJobsPerOwner": 10})
        registry.attach_controller("test")
        owner = _fake_owner("o1", Context(name="own"))
        owner.status = "idle"
        from miniharness.jobs.tools import install_completion_delivery
        install_completion_delivery(registry, {"completionDelivery": "quiet"})
        box = JobDoneBox()
        registry.start({"kind": "bash", "label": "sleep", "owner": owner,
                        "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "completed"})
        self.assertEqual(owner.delivered[0][0], "inject")
        self.assertEqual(owner.wakes, 0)

    def test_reported_completion_no_notice(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        # kill 已消费该作业（reported=True + stopping），随后才结算 → notice 抑制
        self.registry.kill(tid, self.owner)
        box.settle({"status": "completed"})
        self.assertEqual(self.owner.delivered, [])

    def test_unowned_job_no_notice(self):
        box = JobDoneBox()
        self.registry.start({"kind": "bash", "label": "sleep",
                             "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "completed"})
        self.assertEqual(self.owner.delivered, [])

    def test_consecutive_wake_budget_capped(self):
        self.owner.status = "idle"
        for i in range(4):
            box = JobDoneBox()
            self.registry.start({"kind": "bash", "label": f"j{i}", "owner": self.owner,
                                 "run": lambda: {"done": box, "cancel": lambda r: None}})
            box.settle({"status": "completed"})
        # 默认预算 3：前三次 wakeup，第四次起 inject
        self.assertEqual(self.owner.wakes, 3)
        self.assertEqual(len(self.owner.delivered), 4)
        self.assertEqual(self.owner.delivered[3][0], "inject")

    def test_user_claim_resets_budget(self):
        self.owner.status = "idle"
        # 第一次完成 → wakeup，并注册 claimed 钩子
        box = JobDoneBox()
        self.registry.start({"kind": "bash", "label": "j0", "owner": self.owner,
                             "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "completed"})
        self.assertEqual(self.owner.wakes, 1)
        self.assertEqual(len(self.owner.claimed_hooks), 1)
        # 用户输入被认领 → 钩子复位预算
        self.owner.claimed_hooks[0](self.owner)
        for i in range(3):
            b = JobDoneBox()
            self.registry.start({"kind": "bash", "label": f"j{i+1}", "owner": self.owner,
                                 "run": lambda: {"done": b, "cancel": lambda r: None}})
            b.settle({"status": "completed"})
        self.assertEqual(self.owner.wakes, 4)   # 预算已回填 → 全部 wakeup

    def test_waiters_suppress_notice(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        _finish_async(box, 0.05, {"status": "completed"})
        snap = self.registry.wait(tid, 2000, self.owner)
        self.assertEqual(snap["status"], "completed")
        self.assertEqual(self.owner.delivered, [])  # waiters>0 → reported 先行，notice 抑制


class JobsToolsTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-tools")
        self.registry = LocalJobRegistry(self.ctx)
        self.registry.attach_controller("tool-jobs")
        self.owner = _fake_owner("o1", Context(name="own"))
        self.exec = type("Exec", (), {"agent": self.owner, "signal": threading.Event()})()
        self.tools = ToolRegistry(self.ctx)
        register_job_tools(self.tools, self.registry)

    def test_job_list_format(self):
        box = JobDoneBox()
        self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                             "run": lambda: {"done": box, "cancel": lambda r: None}})
        out = self.tools.resolve("job_list").execute({}, self.exec)
        self.assertEqual(out, "bash-1 [bash] running — sleep")

    def test_job_list_empty(self):
        out = self.tools.resolve("job_list").execute({}, self.exec)
        self.assertEqual(out, "(no background jobs)")

    def test_job_output_nonblocking_and_suffix(self):
        box = JobDoneBox()
        chunks = []

        def read_output():
            return chunks.pop(0) if chunks else ""

        tid = self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None,
                                                   "read_output": read_output}})
        chunks.append("progress")
        out = self.tools.resolve("job_output").execute(
            {"job_id": tid}, self.exec)
        self.assertTrue(out.startswith("progress"))
        self.assertTrue(out.endswith("[status: running]"))

    def test_job_output_unknown_job(self):
        with self.assertRaises(RuntimeError):
            self.tools.resolve("job_output").execute({"job_id": "nope-1"}, self.exec)

    def test_job_output_other_session_fenced(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        other = type("Exec", (), {"agent": _fake_owner("bob", Context(name="b")),
                                  "signal": threading.Event()})()
        with self.assertRaises(RuntimeError):
            self.tools.resolve("job_output").execute({"job_id": tid}, other)

    def test_job_kill_flows(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        out = self.tools.resolve("job_kill").execute({"job_id": tid}, self.exec)
        self.assertEqual(out, "requested cancellation of job " + tid)
        box.settle({"status": "killed"})
        out2 = self.tools.resolve("job_kill").execute({"job_id": tid}, self.exec)
        self.assertIn("already finished", out2)

    def test_job_kill_with_reason(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        self.tools.resolve("job_kill").execute({"job_id": tid, "reason": "enough"}, self.exec)
        self.assertEqual(self.registry.get(tid, self.owner)["status"], "stopping")

    def test_config_bounds_enforced(self):
        with self.assertRaises(ValueError):
            register_job_tools(ToolRegistry(Context(name="t1")), self.registry,
                               {"waitTimeoutMs": 700_000, "maxWaitTimeoutMs": 600_000})
        with self.assertRaises(ValueError):
            register_job_tools(ToolRegistry(Context(name="t2")), self.registry,
                               {"completionDelivery": "loud"})
        with self.assertRaises(ValueError):
            register_job_tools(ToolRegistry(Context(name="t3")), self.registry,
                               {"maxConsecutiveWakes": 0})


class RealLoopIntegrationTest(unittest.TestCase):
    """真 AgentLoop：wakeup 在 idle loop 上开 turn，notice 以 plugin 消息进日志。"""

    def test_wakeup_opens_turn_on_real_loop(self):
        ctx = Context(name="jobs-real")
        install_jobs(ctx)
        session = Session("jobs-real-1")
        loop = AgentLoop(session, _TextAdapter("done"), ToolRegistry(ctx), ctx)
        registry = ctx.inject("jobs")
        real = JobDoneBox()
        tid = registry.start({"kind": "bash", "label": "long", "owner": loop,
                              "run": lambda: {"done": real, "cancel": lambda r: None}})
        self.assertEqual(loop.status, "idle")
        real.settle({"status": "completed"})
        # 结算在 producer 线程同步触发 followup → pump 完成一个 turn
        self.assertEqual(loop.status, "idle")
        notices = [ev for ev in session.events
                   if ev["type"] == "user/message" and "background job" in str(ev["data"])]
        self.assertTrue(notices, "notice 应作为 plugin user/message 落日志")
        self.assertEqual(len(loop.inbox), 0)
        self.assertIn(tid, [j["id"] for j in registry.list(loop)])

    def test_job_tools_registered_through_default_tools(self):
        from miniharness.cli.default_tools import default_tools
        ctx = Context(name="jobs-def")
        install_jobs(ctx)
        reg = default_tools(ctx)
        for name in ("job_output", "job_list", "job_kill"):
            self.assertIsNotNone(reg.resolve(name))


if __name__ == "__main__":
    unittest.main()
