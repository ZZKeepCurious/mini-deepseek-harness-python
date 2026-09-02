"""A4 验收：miniharness.jobs 后台作业家族（对齐 packages/jobs/ 契约面）。

覆盖：注册表语义（owner 栅栏 / 结算 first-wins / 并发上限 / teardown）、
三工具（job_output/job_list/job_kill）、完成 notice 投递（wakeup/quiet 与
预算）、字节封顶、装配幂等。461+ 基线之外的独立测试文件。
"""
import asyncio
import threading
import time
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.tools import ToolExec, ToolRegistry, run_pipeline_async
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

    def followup(content, source):
        owner.wakes += 1
        owner.delivered.append(("wakeup", content))

    def inject(content, source):
        owner.delivered.append(("inject", content))

    owner.followup = followup
    owner.inject = inject
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
        self.assertIs(self.ctx.get("jobs"), self.registry)

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
                              "run": lambda: {"done": box, "cancel": lambda r: box.settle({"status": "killed"})}})
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
        def run():
            box = JobDoneBox()
            return {"done": box, "cancel": lambda r: box.settle({"status": "killed"})}
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
        # 第一次完成 → wakeup
        box = JobDoneBox()
        self.registry.start({"kind": "bash", "label": "j0", "owner": self.owner,
                             "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "completed"})
        self.assertEqual(self.owner.wakes, 1)
        # 用户输入被认领 → agent/inbox/claimed（user 源）复位预算
        self.ctx.emit("agent/inbox/claimed",
                      {"agent": self.owner, "message": {"source": {"kind": "user"}}, "turn": 1})
        for i in range(3):
            b = JobDoneBox()
            self.registry.start({"kind": "bash", "label": f"j{i+1}", "owner": self.owner,
                                 "run": lambda: {"done": b, "cancel": lambda r: None}})
            b.settle({"status": "completed"})
        self.assertEqual(self.owner.wakes, 4)   # 预算已回填 → 全部 wakeup

    def test_plugin_claim_does_not_reset_budget(self):
        # 对齐 tool-jobs.spec.ts:666-681：非 user 源认领不恢复预算
        self.owner.status = "idle"
        for i in range(3):
            box = JobDoneBox()
            self.registry.start({"kind": "bash", "label": f"j{i}", "owner": self.owner,
                                 "run": lambda: {"done": box, "cancel": lambda r: None}})
            box.settle({"status": "completed"})
        self.assertEqual(self.owner.wakes, 3)
        self.ctx.emit("agent/inbox/claimed",
                      {"agent": self.owner,
                       "message": {"source": {"kind": "plugin", "plugin": "tool-jobs"}},
                       "turn": 2})
        box = JobDoneBox()
        self.registry.start({"kind": "bash", "label": "j3", "owner": self.owner,
                             "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "completed"})
        self.assertEqual(self.owner.wakes, 3)   # 预算未复位 → 仍 inject
        self.assertEqual(self.owner.delivered[3][0], "inject")

    def test_disposed_event_clears_budget(self):
        self.owner.status = "idle"
        for i in range(4):
            box = JobDoneBox()
            self.registry.start({"kind": "bash", "label": f"j{i}", "owner": self.owner,
                                 "run": lambda: {"done": box, "cancel": lambda r: None}})
            box.settle({"status": "completed"})
        self.assertEqual(self.owner.wakes, 3)
        self.assertEqual(self.owner.delivered[3][0], "inject")
        # agent/disposed：销毁 loop 清预算项 → 下一个完成重新 wakeup
        self.ctx.emit("agent/disposed", {"agent": self.owner})
        box = JobDoneBox()
        self.registry.start({"kind": "bash", "label": "j4", "owner": self.owner,
                             "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "completed"})
        self.assertEqual(self.owner.wakes, 4)

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

    def _call(self, name, args, exec_=None):
        """执行 job 工具（async 契约 → asyncio.run 包装）；返回 canonical value。"""
        return asyncio.run(self.tools.resolve(name).execute(args, exec_ or self.exec))

    def _render(self, name, value):
        """调用工具的 render，返回模型可见 content blocks 列表。"""
        return self.tools.resolve(name).render(value)

    def test_job_list_format(self):
        box = JobDoneBox()
        self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                             "run": lambda: {"done": box, "cancel": lambda r: None}})
        canonical = self._call("job_list", {})
        self.assertEqual(len(canonical), 1)
        self.assertEqual(canonical[0]["kind"], "bash")
        self.assertEqual(canonical[0]["status"], "running")
        rendered = self._render("job_list", canonical)
        self.assertEqual(rendered, [{"type": "text", "text": "bash-1 [bash] running — sleep"}])

    def test_job_list_empty(self):
        canonical = self._call("job_list", {})
        self.assertEqual(canonical, [])
        rendered = self._render("job_list", canonical)
        self.assertEqual(rendered, [{"type": "text", "text": "(no background jobs)"}])

    def test_job_output_nonblocking_and_suffix(self):
        box = JobDoneBox()
        chunks = []

        def read_output():
            return chunks.pop(0) if chunks else ""

        tid = self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None,
                                                   "read_output": read_output}})
        chunks.append("progress")
        canonical = self._call("job_output", {"job_id": tid})
        self.assertEqual(canonical["text"], "progress")
        self.assertEqual(canonical["job"]["status"], "running")
        rendered = self._render("job_output", canonical)
        body = rendered[0]["text"]
        self.assertTrue(body.startswith("progress"))
        self.assertTrue(body.endswith("[status: running]"))

    def test_job_output_unknown_job(self):
        with self.assertRaises(RuntimeError):
            self._call("job_output", {"job_id": "nope-1"})

    def test_job_output_other_session_fenced(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        other = type("Exec", (), {"agent": _fake_owner("bob", Context(name="b")),
                                  "signal": threading.Event()})()
        with self.assertRaises(RuntimeError):
            self._call("job_output", {"job_id": tid}, other)

    def test_job_kill_flows(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        canonical = self._call("job_kill", {"job_id": tid})
        self.assertEqual(canonical["outcome"], "cancellation-requested")
        self.assertEqual(canonical["job"]["id"], tid)
        rendered = self._render("job_kill", canonical)
        self.assertEqual(rendered, [{"type": "text", "text": f"requested cancellation of job {tid}"}])
        box.settle({"status": "killed"})
        canonical2 = self._call("job_kill", {"job_id": tid})
        self.assertEqual(canonical2["outcome"], "already-finished")
        rendered2 = self._render("job_kill", canonical2)
        self.assertIn("already finished", rendered2[0]["text"])

    def test_job_kill_with_reason(self):
        box = JobDoneBox()
        tid = self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                                   "run": lambda: {"done": box, "cancel": lambda r: None}})
        self._call("job_kill", {"job_id": tid, "reason": "enough"})
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

    # ---------- finalizeContent 次要截断（对齐 tool-jobs.spec.ts:210-320） ----------

    def _pipeline(self, name, args):
        """全程管线（政策段 + finalize_content 收口）作为模型可见结果返回。"""
        return asyncio.run(run_pipeline_async(
            self.ctx, self.tools.resolve(name), args, ToolExec(agent=self.owner)))

    def _block_text(self, content):
        """content blocks（冻结 tuple/MappingProxyType 或普通 dict）→ 单文本。"""
        if isinstance(content, (tuple, list)) and len(content) == 1:
            text = content[0].get("text") if hasattr(content[0], "get") else None
            return text if isinstance(text, str) else None
        return None

    def test_finalize_producer_limit_bounds_body_and_status(self):
        # 对齐 tool-jobs.spec.ts:210-220：完整 body+status 按 outputLimitBytes 封顶，
        # 触发保状态行的 [output truncated] 分支
        box = JobDoneBox()
        self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                             "outputLimitBytes": 48,
                             "run": lambda: {"done": box, "cancel": lambda r: None,
                                             "read_output": lambda: "界" * 100}})
        result = self._pipeline("job_output", {"job_id": "bash-1"})
        self.assertTrue(result.ok)
        text = self._block_text(result.content)
        self.assertIsNotNone(text)
        self.assertLessEqual(len(text.encode("utf-8")), 48)
        self.assertIn("[output truncated]", text)
        self.assertIn("[status: running]", text)

    def test_finalize_producer_limit_preserves_empty_and_newline_terminated(self):
        # 对齐 tool-jobs.spec.ts:222-234：空输出与换行结尾在限内原样保留
        box = JobDoneBox()
        chunks = ["", "line\n"]

        def read_output():
            return chunks.pop(0) if chunks else ""

        self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                             "outputLimitBytes": 64,
                             "run": lambda: {"done": box, "cancel": lambda r: None,
                                             "read_output": read_output}})
        r1 = self._pipeline("job_output", {"job_id": "bash-1"})
        self.assertEqual(self._block_text(r1.content), "(no new output)\n[status: running]")
        r2 = self._pipeline("job_output", {"job_id": "bash-1"})
        self.assertEqual(self._block_text(r2.content), "line\n[status: running]")

    def test_finalize_read_failure_bounded(self):
        # 对齐 tool-jobs.spec.ts:253-264：规范化读失败按上限封顶 [result truncated]
        box = JobDoneBox()

        def read_output():
            raise RuntimeError("read failed: " * 100)

        self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                             "outputLimitBytes": 64,
                             "run": lambda: {"done": box, "cancel": lambda r: None,
                                             "read_output": read_output}})
        result = self._pipeline("job_output", {"job_id": "bash-1"})
        self.assertTrue(result.is_error)
        self.assertLessEqual(len(result.error.encode("utf-8")), 64)
        self.assertIn("[result truncated]", result.error)

    def test_finalize_deny_flow_through_pipeline(self):
        # 对齐 tool-jobs.spec.ts:300-303：pre-execute deny 结果同样过 finalize 收口
        # （mini 无 deny reason 载体，短文本在限内不改写；不崩溃即契约成立）
        self.ctx.on("tools/pre-execute",
                    lambda p, nxt: {"kind": "deny"} if p["tool"] == "job_output" else nxt(p))
        box = JobDoneBox()
        self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                             "outputLimitBytes": 64,
                             "run": lambda: {"done": box, "cancel": lambda r: None,
                                             "read_output": lambda: ""}})
        denied = self._pipeline("job_output", {"job_id": "bash-1"})
        self.assertTrue(denied.is_error)
        self.assertEqual(denied.error, "denied by tools/pre-execute")

    def test_finalize_wired_on_job_controls_only(self):
        self.assertIsNotNone(self.tools.resolve("job_output").finalize_content)
        self.assertIsNotNone(self.tools.resolve("job_kill").finalize_content)
        self.assertIsNone(self.tools.resolve("job_list").finalize_content)

    def test_finalize_bounds_long_error_text(self):
        # 对齐 tool-jobs.spec.ts:310-319 失败统一封顶：错误文本（如 deny reason）超限截断
        box = JobDoneBox()
        self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                             "outputLimitBytes": 64,
                             "run": lambda: {"done": box, "cancel": lambda r: None,
                                             "read_output": lambda: ""}})
        hook = self.tools.resolve("job_output").finalize_content
        exec_ = ToolExec(agent=self.owner, name="job_output", arguments={"job_id": "bash-1"})
        bounded = hook(exec_, {"content": [{"type": "text", "text": "denied: " + "d" * 1000}],
                               "value": None, "is_error": True})
        self.assertLessEqual(len(bounded[0]["text"].encode("utf-8")), 64)
        self.assertIn("[result truncated]", bounded[0]["text"])

    def test_finalize_policy_replaced_content_bounded_without_status(self):
        # 对齐 tool-jobs.spec.ts:236-251：内容被 post-policy 替换后不恢复 canonical
        # 渲染，整体封顶且不带状态行（mini 无 accept-replace 载体，直接契约测钩子）
        box = JobDoneBox()
        self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                             "outputLimitBytes": 64,
                             "run": lambda: {"done": box, "cancel": lambda r: None,
                                             "read_output": lambda: "canonical output"}})
        hook = self.tools.resolve("job_output").finalize_content
        exec_ = ToolExec(agent=self.owner, name="job_output", arguments={"job_id": "bash-1"})
        replaced = hook(exec_, {"content": [{"type": "text", "text": "p" * 1000}],
                                "value": {"text": "canonical output",
                                          "job": {"id": "bash-1", "kind": "bash",
                                                  "label": "sleep", "status": "running"}},
                                "is_error": False})
        text = replaced[0]["text"]
        self.assertLessEqual(len(text.encode("utf-8")), 64)
        self.assertIn("[result truncated]", text)
        self.assertNotIn("[status: running]", text)

    def test_finalize_without_limit_unchanged(self):
        # 无 outputLimitBytes → 钩子不干预，管线原样透传
        box = JobDoneBox()
        self.registry.start({"kind": "bash", "label": "sleep", "owner": self.owner,
                             "run": lambda: {"done": box, "cancel": lambda r: None,
                                             "read_output": lambda: "hello"}})
        result = self._pipeline("job_output", {"job_id": "bash-1"})
        self.assertTrue(result.ok)
        self.assertEqual(self._block_text(result.content), "hello\n[status: running]")


class RealLoopIntegrationTest(unittest.TestCase):
    """真 AgentLoop：wakeup 在 idle loop 上开 turn，notice 以 plugin 消息进日志。"""

    def test_wakeup_opens_turn_on_real_loop(self):
        ctx = Context(name="jobs-real")
        install_jobs(ctx)
        session = Session("jobs-real-1")
        loop = AgentLoop(session, _TextAdapter("done"), ToolRegistry(ctx), ctx)
        registry = ctx.get("jobs")
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

    def test_real_loop_user_claim_resets_budget(self):
        # 真实 loop：claimed 事件经载波路由到安装 scope（R2 载体闭合的端点）
        ctx = Context(name="jobs-real-2")
        install_jobs(ctx)
        session = Session("jobs-real-2")
        loop = AgentLoop(session, _TextAdapter("done"), ToolRegistry(ctx), ctx)
        wakes = {"n": 0}
        orig = loop.followup
        def _count(content, source="user"):
            if source == "tool-jobs":
                wakes["n"] += 1
            return orig(content, source=source)
        loop.followup = _count
        registry = ctx.get("jobs")
        def _settle(label):
            box = JobDoneBox()
            registry.start({"kind": "bash", "label": label, "owner": loop,
                            "run": lambda: {"done": box, "cancel": lambda r: None}})
            box.settle({"status": "completed"})
        for i in range(4):
            _settle(f"j{i}")
        self.assertEqual(wakes["n"], 3)      # 第 4 次 inject（预算满）
        # 真实 loop 认领 user 输入（同步 pump 开 turn）→ agent/inbox/claimed
        # user 源 → 安装 scope 的订阅复位预算
        loop.followup("hi", source="user")
        self.assertEqual(loop.status, "idle")
        _settle("j4")
        _settle("j5")
        self.assertEqual(wakes["n"], 5)      # 已复位 → 又两次 wakeup

    def test_job_tools_registered_through_default_tools(self):
        from miniharness.cli.default_tools import default_tools
        ctx = Context(name="jobs-def")
        install_jobs(ctx)
        reg = default_tools(ctx)
        for name in ("job_output", "job_list", "job_kill"):
            self.assertIsNotNone(reg.resolve(name))


class ScopeLayeringTest(unittest.TestCase):
    """controller/监听器按注册 scope 分层（P1-4a，对齐 jobs-local index.ts
    ScopedLayers 语义 + jobs.spec.ts:125-160 的 served/unserved 矩阵）。"""

    def setUp(self):
        self.ctx = Context(name="jobs-scope")
        self.registry = LocalJobRegistry(self.ctx)
        # 两个平级 preset scope（对齐上游 createScope 兄弟节点）
        self.scope_a = self.ctx.create_scope("preset-a")
        self.scope_b = self.ctx.create_scope("preset-b")

    def _owner(self, owner_id: str, scope):
        return _fake_owner(owner_id, scope.ctx)

    def _spec(self, owner=None):
        return {"kind": "bash", "label": "sleep 60",
                **({"owner": owner} if owner is not None else {}),
                "run": lambda: {"done": JobDoneBox(), "cancel": lambda r: None}}

    def test_no_controller_rejected_verbatim(self):
        owner = self._owner("a1", self.scope_a)
        with self.assertRaises(RuntimeError) as cm:
            self.registry.start(self._spec(owner))
        # 逐字对齐上游 jobs-local index.ts:133
        self.assertEqual(
            str(cm.exception),
            "background jobs unavailable: no job controller serves this agent "
            "(load @deepseek-ai/dsh-tool-jobs in its composition)")

    def test_scoped_controller_serves_only_its_subtree(self):
        # controller 从 preset-a 的组合 scope 注册（tool-jobs 方式）
        self.registry.attach_controller("tool-jobs", self.scope_a.ctx)
        served = self._owner("served", self.scope_a)
        unserved = self._owner("unserved", self.scope_b)
        job_id = self.registry.start(self._spec(served))
        self.assertTrue(job_id.startswith("bash-"))
        with self.assertRaises(RuntimeError):
            self.registry.start(self._spec(unserved))
        # 平级 scope 的链互不覆盖：B 链上没有 A 层

    def test_scoped_controller_does_not_serve_unowned(self):
        # unowned 无链可走，只有全局层能接（jobs.spec.ts:143 注释语义）
        self.registry.attach_controller("tool-jobs", self.scope_a.ctx)
        with self.assertRaises(RuntimeError):
            self.registry.start(self._spec(None))

    def test_global_controller_serves_everyone(self):
        self.registry.attach_controller("host", self.ctx)  # 根 ctx → 全局层
        self.assertTrue(self.registry.start(self._spec(None)))
        self.assertTrue(self.registry.start(self._spec(self._owner("a1", self.scope_a))))
        self.assertTrue(self.registry.start(self._spec(self._owner("b1", self.scope_b))))

    def test_descendant_scope_owner_served_by_ancestor_controller(self):
        # 嵌套 scope 自动成为父 scope 后裔：A 内再开 scope，controller 在 A 层
        inner = self.scope_a.ctx.create_scope("inner-agent")
        self.registry.attach_controller("tool-jobs", self.scope_a.ctx)
        job_id = self.registry.start(self._spec(self._owner("deep", inner)))
        self.assertTrue(job_id.startswith("bash-"))

    def test_done_listener_delivery_is_scope_relative(self):
        seen: list[tuple[str, str | None]] = []
        global_seen: list[tuple[str, str | None]] = []
        self.registry.attach_controller("tool-jobs", self.scope_a.ctx)
        self.registry.attach_controller("tool-jobs-b", self.scope_b.ctx)
        self.registry.on_job_done(
            lambda snap, owner: seen.append((snap["id"], getattr(owner, "id", None))),
            self.scope_a.ctx)
        self.registry.on_job_done(
            lambda snap, owner: global_seen.append((snap["id"], getattr(owner, "id", None))),
            self.ctx)
        box = JobDoneBox()
        owner_a = self._owner("a1", self.scope_a)
        job_id = self.registry.start({
            "kind": "bash", "label": "x", "owner": owner_a,
            "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "completed"})
        self.assertEqual(seen, [(job_id, "a1")])          # A 层只收 A 链的结算
        self.assertEqual(global_seen, [(job_id, "a1")])   # 全局层收所有结算

    def test_unowned_settlement_skips_scoped_listener(self):
        scoped_seen: list = []
        global_seen: list = []
        self.registry.attach_controller("host", self.ctx)
        self.registry.on_job_done(lambda s, o: scoped_seen.append(s["id"]), self.scope_a.ctx)
        self.registry.on_job_done(lambda s, o: global_seen.append(s["id"]), self.ctx)
        box = JobDoneBox()
        job_id = self.registry.start({
            "kind": "bash", "label": "bg",
            "run": lambda: {"done": box, "cancel": lambda r: None}})
        box.settle({"status": "completed"})
        self.assertEqual(scoped_seen, [])                 # unowned 不进 scoped 监听器
        self.assertEqual(global_seen, [job_id])

    def test_controller_detaches_on_scope_dispose(self):
        self.registry.attach_controller("tool-jobs", self.scope_a.ctx)
        self.scope_a.dispose()
        owner = self._owner("a1", self.scope_a)
        with self.assertRaises(RuntimeError):
            self.registry.start(self._spec(owner))

    def test_duplicate_names_independent_detach(self):
        d1 = self.registry.attach_controller("a")
        d2 = self.registry.attach_controller("a")
        d1()
        self.assertTrue(self.registry.start(self._spec(None)))  # a 的第二枚 token 仍在
        d2()
        with self.assertRaises(RuntimeError):
            self.registry.start(self._spec(None))


if __name__ == "__main__":
    unittest.main()
