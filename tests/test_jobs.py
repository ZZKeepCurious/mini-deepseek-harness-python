"""验收：miniharness.jobs 后台作业家族（对齐 packages/jobs/ 契约面，rc.1 重写）。

覆盖：注册表语义（owner 会话栅栏 / 结算 first-wins / 并发上限 / teardown）、
JobHandle 输出 ring + pull 泵、JobEvents 订阅与 scope 分层、三工具
（job_output/job_list/job_kill）、完成 notice 投递（wakeup/quiet 与预算）、
字节封顶、装配幂等。461+ 基线之外的独立测试文件。

rc.1 契约：owner 是 SessionId（注册表经 ctx.agents 解析为 live Agent）；
run(handle) 收 JobHandle（append/updateProgress）；结算以 cause+awaited 描述；
输出走 ring（read 消费游标 / readAt 非消耗）；事件经 events.subscribe。
"""
import asyncio
import threading
import time
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.agents import install_agents
from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.session_store import install_sessions
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
from miniharness.jobs.pump import start_pump
from miniharness.jobs.ring import OutputRing
from miniharness.jobs.tools import install_completion_delivery
from miniharness.llm import FakeLlmAdapter


class _TextAdapter(FakeLlmAdapter):
    def __init__(self, text: str):
        super().__init__(final_text=text)


def _make_owner(agents, session_id: str, scope_ctx: Context | None = None):
    """注册进真实 ctx.agents 的轻量 owner 替身（resolveOwner 需 live 实例）。

    暴露 id/session(session_id)/ctx/_carrier/status + followup/inject/delivered/wakes，
    足以为 jobs 的所有权栅栏、scope 路由与 notice 送达提供精确边界。
    """
    owner = type("Owner", (), {})()
    owner.id = session_id
    owner.session = type("SessionRef", (), {"session_id": session_id})()
    owner.ctx = scope_ctx if scope_ctx is not None else Context(name=f"own-{session_id}")
    owner._carrier = owner.ctx
    owner.status = "idle"
    owner.delivered: list[tuple[str, str]] = []
    owner.wakes = 0

    def followup(content, source=None):
        owner.wakes += 1
        owner.delivered.append(("wakeup", content))

    def inject(content, source=None):
        owner.delivered.append(("inject", content))

    owner.followup = followup
    owner.inject = inject
    agents.register(owner)
    return owner


def _start(registry, *, owner=None, kind="bash", label="s", output=None, limit=None):
    """启动一条作业；返回 (id, box, holder)。holder['handle'] 为 JobHandle。"""
    box = JobDoneBox()
    holder: dict = {"cancel": lambda r=None: None}

    def run(job):
        holder["handle"] = job
        return {"done": box, "cancel": lambda r=None: holder["cancel"](r)}

    spec: dict = {"kind": kind, "label": label, "run": run}
    if owner is not None:
        spec["owner"] = owner
    if output is not None:
        spec["output"] = output
    if limit is not None:
        spec["outputLimitBytes"] = limit
    return registry.start(spec), box, holder


def _finish_async(box: JobDoneBox, delay: float, outcome: dict) -> threading.Thread:
    """后台线程在 delay 秒后结算（producer 完成线程模拟）。"""
    t = threading.Thread(target=lambda: (time.sleep(delay), box.settle(outcome)),
                         daemon=True)
    t.start()
    return t


def _collect(registry, ctx, filter_):
    """订阅事件并返回收集列表（含 disposer 不保留）。"""
    seen: list[dict] = []
    registry.events_for(ctx).subscribe(filter_, seen.append)
    return seen


def _text(chunks) -> str:
    return "".join(c["text"] for c in chunks)


class RegistryBasicsTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-registry")
        self.agents = install_agents(self.ctx)
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
        owner = _make_owner(self.agents, "o1")
        with self.assertRaises(RuntimeError):
            _start(self.registry, owner=owner)

    def test_attach_controller_enables_start(self):
        self.registry.attach_controller("test")
        owner = _make_owner(self.agents, "o1")
        tid, _box, _h = _start(self.registry, owner=owner, label="sleep")
        self.assertTrue(tid.startswith("bash-"))
        snap = self.registry.get(tid, owner)
        self.assertEqual(snap["status"], "running")
        self.assertEqual(snap["owner"], "o1")

    def test_id_counter_per_kind(self):
        self.registry.attach_controller("test")
        owner = _make_owner(self.agents, "o1")
        a, _b1, _h1 = _start(self.registry, owner=owner, label="1")
        b, _b2, _h2 = _start(self.registry, owner=owner, label="2")
        c, _b3, _h3 = _start(self.registry, owner=owner, kind="subagent", label="3")
        self.assertEqual((a, b, c), ("bash-1", "bash-2", "subagent-1"))

    def test_invalid_spec_rejected(self):
        self.registry.attach_controller("test")
        owner = _make_owner(self.agents, "o1")
        with self.assertRaises(ValueError):
            _start(self.registry, owner=owner, kind="", label="x")
        with self.assertRaises(ValueError):
            _start(self.registry, owner=owner, kind="bash", label="")
        with self.assertRaises(ValueError):
            _start(self.registry, owner=owner, label="x", limit=-1)


class AccessFenceTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-fence")
        self.agents = install_agents(self.ctx)
        self.registry = LocalJobRegistry(self.ctx)
        self.registry.attach_controller("test")
        self.alice = _make_owner(self.agents, "alice")
        self.bob = _make_owner(self.agents, "bob")

    def test_owned_job_hidden_from_other_session(self):
        tid, _box, _h = _start(self.registry, owner=self.alice, label="mine")
        self.assertEqual([j["id"] for j in self.registry.list(self.bob)], [])
        self.assertEqual([j["id"] for j in self.registry.list(self.alice)], [tid])
        with self.assertRaises(RuntimeError):
            self.registry.get(tid, self.bob)
        with self.assertRaises(RuntimeError):
            self.registry.kill(tid, self.bob)
        with self.assertRaises(RuntimeError):
            self.registry.read(tid, self.bob)

    def test_unowned_job_open_to_all(self):
        tid, _box, _h = _start(self.registry, label="shared")
        self.assertEqual([j["id"] for j in self.registry.list(self.alice)], [tid])
        self.assertEqual([j["id"] for j in self.registry.list(None)], [tid])
        self.assertIsNotNone(self.registry.get(tid, self.bob))

    def test_agentless_caller_never_matches_owned(self):
        tid, _box, _h = _start(self.registry, owner=self.alice, label="mine")
        with self.assertRaises(RuntimeError):
            self.registry.get(tid, None)

    def test_unknown_job_fails_loud(self):
        with self.assertRaises(RuntimeError):
            self.registry.get("nope-1", self.alice)

    def test_owner_must_be_live_agent(self):
        with self.assertRaises(RuntimeError) as cm:
            _start(self.registry, owner="ghost", label="x")
        self.assertIn("has no live agent", str(cm.exception))


class SettlementTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-settle")
        self.agents = install_agents(self.ctx)
        self.registry = LocalJobRegistry(self.ctx)
        self.registry.attach_controller("test")
        self.owner = _make_owner(self.agents, "o1")

    def test_completed_settlement(self):
        tid, box, _h = _start(self.registry, owner=self.owner)
        box.settle({"status": "completed"})
        snap = self.registry.get(tid, self.owner)
        self.assertEqual(snap["status"], "completed")
        self.assertIsNotNone(snap["finishedAt"])
        self.assertLessEqual(snap["startedAt"], snap["finishedAt"])
        self.assertNotIn("progress", snap)

    def test_first_wins_ignores_late_settle(self):
        tid, box, _h = _start(self.registry, owner=self.owner)
        box.settle({"status": "completed"})
        box.settle({"status": "killed"})
        self.assertEqual(self.registry.get(tid, self.owner)["status"], "completed")

    def test_reject_becomes_failed_with_detail(self):
        tid, box, _h = _start(self.registry, owner=self.owner)
        box.fail(ValueError("boom"))
        snap = self.registry.get(tid, self.owner)
        self.assertEqual(snap["status"], "failed")
        self.assertEqual(snap["detail"], "boom")

    def test_invalid_producer_outcome_is_failed(self):
        tid, box, _h = _start(self.registry, owner=self.owner)
        box.settle({"status": "running"})
        snap = self.registry.get(tid, self.owner)
        self.assertEqual(snap["status"], "failed")
        self.assertIn("invalid outcome", snap["detail"])

    def test_optional_fields_in_snapshot(self):
        tid, box, _h = _start(self.registry, owner=self.owner, limit=1024)
        box.settle({"status": "completed", "detail": "done", "result": "out"})
        snap = self.registry.get(tid, self.owner)
        self.assertEqual(snap["detail"], "done")
        self.assertEqual(snap["outputLimitBytes"], 1024)
        self.assertEqual(self.registry.read(tid, self.owner)["result"], "out")

    def test_kill_reason_merged_into_killed_detail(self):
        tid, box, _h = _start(self.registry, owner=self.owner)
        self.registry.kill(tid, self.owner, "user asked")
        box.settle({"status": "killed", "detail": "signal: SIGTERM"})
        snap = self.registry.get(tid, self.owner)
        self.assertEqual(snap["detail"], "signal: SIGTERM; user asked")

    def test_kill_reason_dropped_when_job_outran_kill(self):
        tid, box, _h = _start(self.registry, owner=self.owner)
        self.registry.kill(tid, self.owner, "late")
        box.settle({"status": "completed", "detail": "exit code: 0"})
        self.assertEqual(self.registry.get(tid, self.owner)["detail"], "exit code: 0")

    def test_producer_done_listener_fires_once(self):
        seen = _collect(self.registry, self.ctx, {"owners": "all"})
        tid, box, _h = _start(self.registry, owner=self.owner)
        box.settle({"status": "completed"})
        box.settle({"status": "killed"})
        settled = [e for e in seen if e["type"] == "settled"]
        self.assertEqual(len(settled), 1)
        self.assertEqual(settled[0]["cause"], "producer")
        self.assertFalse(settled[0]["awaited"])


class KillTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-kill")
        self.agents = install_agents(self.ctx)
        self.registry = LocalJobRegistry(self.ctx)
        self.registry.attach_controller("test")
        self.owner = _make_owner(self.agents, "o1")
        self.cancelled: list = []

    def test_kill_sets_stopping_and_calls_cancel(self):
        tid, _box, holder = _start(self.registry, owner=self.owner)
        holder["cancel"] = lambda r: self.cancelled.append(r)
        self.assertEqual(self.registry.kill(tid, self.owner, "no longer needed"),
                         "requested")
        snap = self.registry.get(tid, self.owner)
        self.assertEqual(snap["status"], "stopping")
        self.assertEqual(self.cancelled, ["no longer needed"])

    def test_kill_terminal_returns_already_finished(self):
        tid, box, _h = _start(self.registry, owner=self.owner)
        box.settle({"status": "completed"})
        self.assertEqual(self.registry.kill(tid, self.owner), "already-finished")

    def test_throwing_cancel_propagates_without_state_change(self):
        tid, _box, holder = _start(self.registry, owner=self.owner)

        def boom(_r=None):
            raise RuntimeError("cancel denied")

        holder["cancel"] = boom
        with self.assertRaises(RuntimeError):
            self.registry.kill(tid, self.owner)
        self.assertEqual(self.registry.get(tid, self.owner)["status"], "running")

    def test_kill_emits_stopping_event(self):
        seen = _collect(self.registry, self.ctx, {"owners": "all"})
        tid, _box, _h = _start(self.registry, owner=self.owner)
        self.registry.kill(tid, self.owner)
        stopping = [e for e in seen if e["type"] == "stopping"]
        self.assertEqual(len(stopping), 1)
        self.assertEqual(stopping[0]["job"]["status"], "stopping")


class ReadWaitTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-read")
        self.agents = install_agents(self.ctx)
        self.registry = LocalJobRegistry(self.ctx)
        self.registry.attach_controller("test")
        self.owner = _make_owner(self.agents, "o1")

    def test_stream_job_reads_delta(self):
        tid, _box, holder = _start(self.registry, owner=self.owner)
        handle = holder["handle"]
        handle.append("alpha")
        self.assertEqual(_text(self.registry.read(tid, self.owner)["chunks"]), "alpha")
        handle.append("beta")
        self.assertEqual(_text(self.registry.read(tid, self.owner)["chunks"]), "beta")
        self.assertEqual(_text(self.registry.read(tid, self.owner)["chunks"]), "")

    def test_final_output_job_reads_result_after_settlement(self):
        tid, box, _h = _start(self.registry, owner=self.owner, kind="subagent")
        self.assertEqual(self.registry.read(tid, self.owner).get("result"), None)
        box.settle({"status": "completed", "result": "the answer"})
        first = self.registry.read(tid, self.owner)
        self.assertEqual(first["result"], "the answer")
        second = self.registry.read(tid, self.owner)
        self.assertNotIn("result", second)

    def test_read_channels_render_split(self):
        tid, _box, holder = _start(self.registry, owner=self.owner)
        handle = holder["handle"]
        handle.append("out", {"channel": "stdout"})
        handle.append("err", {"channel": "stderr"})
        handle.append("narr", {"channel": "log"})
        read = self.registry.read(tid, self.owner)
        self.assertEqual(set(c.get("channel") for c in read["chunks"]),
                         {"stdout", "stderr", "log"})
        self.assertEqual(read["job"]["output"]["total"], 10)

    def test_read_at_non_consuming(self):
        tid, _box, holder = _start(self.registry, owner=self.owner)
        holder["handle"].append("alpha")
        read_at = self.registry.read_at(tid, 0, self.owner)
        self.assertEqual(_text(read_at["chunks"]), "alpha")
        self.assertEqual(read_at["next"], 5)
        # 非消耗：模型游标未动，read 仍返回同一 delta
        self.assertEqual(_text(self.registry.read(tid, self.owner)["chunks"]), "alpha")

    def test_read_at_invalid_offset(self):
        tid, _box, _h = _start(self.registry, owner=self.owner)
        with self.assertRaises(ValueError):
            self.registry.read_at(tid, -1, self.owner)

    def test_wait_returns_after_settlement(self):
        seen = _collect(self.registry, self.ctx, {"owners": "all"})
        tid, box, _h = _start(self.registry, owner=self.owner)
        _finish_async(box, 0.05, {"status": "completed"})
        snap = self.registry.wait(tid, 2000, self.owner)
        self.assertEqual(snap["status"], "completed")
        settled = [e for e in seen if e["type"] == "settled"][0]
        self.assertTrue(settled["awaited"])

    def test_wait_timeout_returns_running_without_cancelling(self):
        tid, _box, _h = _start(self.registry, owner=self.owner)
        snap = self.registry.wait(tid, 30, self.owner)
        self.assertEqual(snap["status"], "running")
        self.assertEqual(self.registry.get(tid, self.owner)["status"], "running")

    def test_wait_aborted_by_signal_raises(self):
        tid, _box, _h = _start(self.registry, owner=self.owner)
        signal = threading.Event()
        threading.Thread(target=lambda: (time.sleep(0.05), signal.set()),
                         daemon=True).start()
        with self.assertRaises(RuntimeError):
            self.registry.wait(tid, 2000, self.owner, signal)

    def test_wait_invalid_timeout(self):
        tid, _box, _h = _start(self.registry, owner=self.owner)
        with self.assertRaises(ValueError):
            self.registry.wait(tid, 0, self.owner)

    def test_remove_settled_job(self):
        tid, box, _h = _start(self.registry, owner=self.owner)
        box.settle({"status": "completed"})
        self.registry.remove(tid, self.owner)
        with self.assertRaises(RuntimeError):
            self.registry.get(tid, self.owner)

    def test_remove_live_job_fails_loud(self):
        tid, _box, _h = _start(self.registry, owner=self.owner)
        with self.assertRaises(RuntimeError):
            self.registry.remove(tid, self.owner)


class ConcurrencyCapTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-cap")
        self.agents = install_agents(self.ctx)
        self.registry = LocalJobRegistry(self.ctx, {"maxConcurrentJobsPerOwner": 2})
        self.registry.attach_controller("test")
        self.owner = _make_owner(self.agents, "o1")

    def test_default_limit_value(self):
        self.assertEqual(DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER, 10)

    def test_cap_enforced_per_exact_owner(self):
        _start(self.registry, owner=self.owner, label="1")
        _start(self.registry, owner=self.owner, label="2")
        with self.assertRaises(RuntimeError):
            _start(self.registry, owner=self.owner, label="3")

    def test_settlement_frees_capacity(self):
        _tid, box, _h = _start(self.registry, owner=self.owner, label="1")
        _start(self.registry, owner=self.owner, label="2")
        box.settle({"status": "completed"})
        _start(self.registry, owner=self.owner, label="3")

    def test_other_owner_has_own_cap(self):
        other = _make_owner(self.agents, "o2")
        _start(self.registry, owner=self.owner, label="1")
        _start(self.registry, owner=self.owner, label="2")
        _start(self.registry, owner=other, label="1")
        _start(self.registry, owner=other, label="2")
        with self.assertRaises(RuntimeError):
            _start(self.registry, owner=other, label="3")

    def test_unowned_share_one_bucket(self):
        _start(self.registry, label="1")
        _start(self.registry, label="2")
        with self.assertRaises(RuntimeError):
            _start(self.registry, label="3")


class TeardownTest(unittest.TestCase):
    def test_owner_dispose_cancels_and_removes(self):
        ctx = Context(name="jobs-td")
        agents = install_agents(ctx)
        registry = LocalJobRegistry(ctx)
        registry.attach_controller("test")
        owner = _make_owner(agents, "o1")
        tid, box, holder = _start(registry, owner=owner)
        holder["cancel"] = lambda r: box.settle({"status": "killed"})
        owner.ctx.dispose()
        self.assertEqual(registry.list(owner), [])
        with self.assertRaises(RuntimeError):
            registry.get(tid, owner)

    def test_teardown_cancel_throw_force_fails(self):
        ctx = Context(name="jobs-td2")
        agents = install_agents(ctx)
        registry = LocalJobRegistry(ctx)
        registry.attach_controller("test")
        owner = _make_owner(agents, "o1")
        seen = _collect(registry, ctx, {"owners": "all"})
        tid, _box, holder = _start(registry, owner=owner)

        def boom(_r=None):
            raise RuntimeError("no")

        holder["cancel"] = boom
        owner.ctx.dispose()
        settled = [e for e in seen if e["type"] == "settled"]
        self.assertEqual([e["job"]["status"] for e in settled], ["failed"])
        self.assertEqual(settled[0]["cause"], "teardown")
        self.assertIn("orphaned", settled[0]["job"]["detail"])
        with self.assertRaises(RuntimeError):
            registry.get(tid, owner)

    def test_owner_cleanup_registered_once(self):
        ctx = Context(name="jobs-td3")
        agents = install_agents(ctx)
        registry = LocalJobRegistry(ctx)
        registry.attach_controller("test")
        owner = _make_owner(agents, "o1")
        _tid1, box1, holder1 = _start(registry, owner=owner, label="1")
        _tid2, box2, holder2 = _start(registry, owner=owner, label="2")
        holder1["cancel"] = lambda r: box1.settle({"status": "killed"})
        holder2["cancel"] = lambda r: box2.settle({"status": "killed"})
        # 第二枚 owner cleanup 不重复挂载；dispose 只触发一次且全部结算
        self.assertEqual(len(registry._owner_cleanups), 1)
        owner.ctx.dispose()
        self.assertEqual(registry.list(owner), [])


class RingTest(unittest.TestCase):
    def test_absolute_offsets_and_head_eviction(self):
        ring = OutputRing()
        ring.append("ab", None, 10)
        ring.append("cd", None, 10)
        self.assertEqual((ring.total, ring.earliest), (4, 0))
        ring.append("efghij", None, 4)
        # 头部驱逐到 cap=4；偏移保持绝对
        self.assertEqual(ring.total, 10)
        self.assertEqual(ring.earliest, 6)
        read = ring.read_from(0)
        self.assertTrue(read["lossy"])
        self.assertEqual(_text(read["chunks"]), "ghij")

    def test_oversized_single_chunk_keeps_utf8_tail(self):
        ring = OutputRing()
        ring.append("中" * 10, None, 4)  # 每字 3 字节，总 30
        self.assertEqual(ring.total, 30)
        self.assertEqual(ring.retained_bytes, 3)
        # 4 字节切点落在码点中途，向后越过续字节 → 幸存 1 字，起点绝对偏移 27
        self.assertEqual(ring.earliest, 27)
        chunk = ring.read_from(0)["chunks"][0]
        self.assertTrue(chunk["gapBefore"])
        self.assertEqual(chunk["text"], "中")

    def test_empty_chunk_is_noop(self):
        ring = OutputRing()
        self.assertFalse(ring.append("", None, 10))
        self.assertEqual((ring.total, ring.earliest), (0, 0))

    def test_channel_and_gap_preserved(self):
        ring = OutputRing()
        ring.append("x", {"channel": "stderr", "gapBefore": True}, 10)
        chunk = ring.read_from(0)["chunks"][0]
        self.assertEqual(chunk["channel"], "stderr")
        self.assertTrue(chunk["gapBefore"])


class PumpTest(unittest.TestCase):
    def test_pump_drains_source_once_and_reports_spill(self):
        state = {"sent": False}
        appended: list = []
        spills: list = []

        def read(from_byte):
            if state["sent"]:
                return {"text": "", "nextOffset": from_byte, "lossy": False}
            state["sent"] = True
            return {"text": "hello", "nextOffset": from_byte + 5, "lossy": False,
                    "spillPath": "/spill/a"}

        until = threading.Event()
        until.set()
        handle = start_pump(
            [{"read": read}],
            {"append": lambda text, options=None: appended.append((text, options)),
             "spill": lambda i, p: spills.append((i, p))},
            10, until)
        handle.wait()
        self.assertEqual([a[0] for a in appended], ["hello"])
        # 末次读未再报 spill → sink 收到 None（源撤回落盘文件，对齐 pump.ts:68）
        self.assertEqual(spills, [(0, "/spill/a"), (0, None)])

    def test_lossy_read_lands_gap_before(self):
        appended: list = []
        until = threading.Event()
        until.set()
        handle = start_pump(
            [{"channel": "stderr",
              "read": lambda frm: {"text": "tail", "nextOffset": 9, "lossy": True}}],
            {"append": lambda text, options=None: appended.append((text, options)),
             "spill": lambda i, p: None},
            10, until)
        handle.wait()
        self.assertEqual(appended[0], ("tail", {"channel": "stderr", "gapBefore": True}))

    def test_invalid_poll_rejected(self):
        with self.assertRaises(ValueError):
            start_pump([], {"append": lambda *a: None, "spill": lambda *a: None}, 0,
                       threading.Event())


class PullIntegrationTest(unittest.TestCase):
    def test_pull_source_pumped_into_ring(self):
        ctx = Context(name="jobs-pull")
        agents = install_agents(ctx)
        registry = LocalJobRegistry(ctx)
        registry.attach_controller("test")
        owner = _make_owner(agents, "o1")
        state = {"sent": False}

        def read(from_byte):
            # 源始终持有同一落盘文件（每次读都上报，故自元数据存活）
            if state["sent"]:
                return {"text": "", "nextOffset": from_byte, "lossy": False,
                        "spillPath": "/spill/pull"}
            state["sent"] = True
            return {"text": "from-source", "nextOffset": from_byte + 11,
                    "lossy": False, "spillPath": "/spill/pull"}

        tid, box, _h = _start(registry, owner=owner, output=[{"read": read}], label="pump")
        box.settle({"status": "completed"})
        read_result = registry.read(tid, owner)
        self.assertEqual(_text(read_result["chunks"]), "from-source")
        self.assertEqual(read_result["job"]["output"]["spillPaths"], ["/spill/pull"])


class EventsTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-events")
        self.agents = install_agents(self.ctx)
        self.registry = LocalJobRegistry(self.ctx)
        self.registry.attach_controller("test")
        self.owner = _make_owner(self.agents, "o1")

    def test_lifecycle_event_sequence(self):
        seen = _collect(self.registry, self.ctx, {"owners": "all"})
        tid, box, holder = _start(self.registry, owner=self.owner)
        holder["handle"].append("x")
        holder["handle"].update_progress("3/10")
        self.registry.kill(tid, self.owner)
        box.settle({"status": "killed"})
        self.registry.remove(tid, self.owner)
        types = [e["type"] for e in seen]
        self.assertEqual(types[0], "registered")
        for expected in ("progress", "stopping", "settled", "removed"):
            self.assertIn(expected, types)
        # settled 后跟一条收尾 output
        settled_index = types.index("settled")
        self.assertEqual(types[settled_index + 1], "output")

    def test_output_event_carries_total(self):
        seen = _collect(self.registry, self.ctx, {"owners": "all"})
        _tid, _box, holder = _start(self.registry, owner=self.owner)
        holder["handle"].append("abcd")
        outputs = [e for e in seen if e["type"] == "output"]
        self.assertEqual(outputs[-1]["total"], 4)

    def test_owner_filter_only_delivers_that_owner(self):
        seen = _collect(self.registry, self.ctx, {"owner": "o1"})
        other = _make_owner(self.agents, "o2")
        _start(self.registry, owner=other, label="other")
        self.assertEqual(seen, [])
        _start(self.registry, owner=self.owner, label="mine")
        self.assertTrue(seen)

    def test_listener_throw_is_contained(self):
        seen: list = []
        self.registry.events_for(self.ctx).subscribe(
            {"owners": "all"},
            lambda e: (_ for _ in ()).throw(RuntimeError("listener boom")))
        self.registry.events_for(self.ctx).subscribe({"owners": "all"}, seen.append)
        _tid, _box, _h = _start(self.registry, owner=self.owner)
        self.assertTrue(any(e["type"] == "registered" for e in seen))


class NoticeDeliveryTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-notice")
        install_agents(self.ctx)
        self.registry = install_jobs(self.ctx)
        self.owner = _make_owner(self.ctx.get("agents"), "o1")

    def _start_and_settle(self, outcome: dict) -> str:
        tid, box, _h = _start(self.registry, owner=self.owner, label="sleep")
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

    def test_default_delivery_is_unbounded_wakeup(self):
        self.owner.status = "idle"
        for i in range(5):
            self._start_and_settle({"status": "completed"})
        self.assertEqual(self.owner.wakes, 5)
        self.assertTrue(all(k == "wakeup" for k, _ in self.owner.delivered))

    def test_quiet_delivery_injects_even_idle(self):
        ctx = Context(name="jobs-quiet")
        install_agents(ctx)
        registry = install_jobs(ctx, {"completionDelivery": "quiet"})
        owner = _make_owner(ctx.get("agents"), "o1")
        owner.status = "idle"
        tid, box, _h = _start(registry, owner=owner, label="sleep")
        box.settle({"status": "completed"})
        self.assertEqual(owner.delivered[0][0], "inject")
        self.assertEqual(owner.wakes, 0)

    def test_awaited_settlement_no_notice(self):
        tid, box, _h = _start(self.registry, owner=self.owner)
        _finish_async(box, 0.05, {"status": "completed"})
        self.registry.wait(tid, 2000, self.owner)
        self.assertEqual(self.owner.delivered, [])

    def test_unowned_job_no_notice(self):
        _tid, box, _h = _start(self.registry, label="sleep")
        box.settle({"status": "completed"})
        self.assertEqual(self.owner.delivered, [])

    def test_consecutive_wake_budget_capped(self):
        ctx = Context(name="jobs-budget")
        install_agents(ctx)
        registry = install_jobs(ctx, {"maxConsecutiveWakes": 3})
        owner = _make_owner(ctx.get("agents"), "o1")
        for i in range(4):
            tid, box, _h = _start(registry, owner=owner, label=f"j{i}")
            box.settle({"status": "completed"})
        self.assertEqual(owner.wakes, 3)
        self.assertEqual(len(owner.delivered), 4)
        self.assertEqual(owner.delivered[3][0], "inject")

    def test_user_claim_resets_budget(self):
        ctx = Context(name="jobs-claim")
        install_agents(ctx)
        registry = install_jobs(ctx, {"maxConsecutiveWakes": 3})
        owner = _make_owner(ctx.get("agents"), "o1")
        for i in range(3):
            tid, box, _h = _start(registry, owner=owner, label=f"j{i}")
            box.settle({"status": "completed"})
        self.assertEqual(owner.wakes, 3)
        ctx.emit("agent/inbox/claimed",
                 {"agent": owner, "message": {"source": {"kind": "user"}}, "turn": 1})
        for i in range(3):
            tid, box, _h = _start(registry, owner=owner, label=f"k{i}")
            box.settle({"status": "completed"})
        self.assertEqual(owner.wakes, 6)

    def test_plugin_claim_does_not_reset_budget(self):
        ctx = Context(name="jobs-plugin-claim")
        install_agents(ctx)
        registry = install_jobs(ctx, {"maxConsecutiveWakes": 3})
        owner = _make_owner(ctx.get("agents"), "o1")
        for i in range(3):
            tid, box, _h = _start(registry, owner=owner, label=f"j{i}")
            box.settle({"status": "completed"})
        ctx.emit("agent/inbox/claimed",
                 {"agent": owner,
                  "message": {"source": {"kind": "plugin", "plugin": "tool-jobs"}},
                  "turn": 2})
        tid, box, _h = _start(registry, owner=owner, label="j3")
        box.settle({"status": "completed"})
        self.assertEqual(owner.wakes, 3)
        self.assertEqual(owner.delivered[3][0], "inject")

    def test_disposed_event_clears_budget(self):
        ctx = Context(name="jobs-disposed")
        install_agents(ctx)
        registry = install_jobs(ctx, {"maxConsecutiveWakes": 3})
        owner = _make_owner(ctx.get("agents"), "o1")
        for i in range(4):
            tid, box, _h = _start(registry, owner=owner, label=f"j{i}")
            box.settle({"status": "completed"})
        self.assertEqual(owner.wakes, 3)
        self.assertEqual(owner.delivered[3][0], "inject")
        ctx.emit("agent/disposed", {"agent": owner})
        tid, box, _h = _start(registry, owner=owner, label="j4")
        box.settle({"status": "completed"})
        self.assertEqual(owner.wakes, 4)


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

    def test_public_job_drops_owner_and_output(self):
        job = {"id": "bash-1", "kind": "bash", "label": "s", "status": "running",
               "startedAt": 1, "owner": "o1",
               "output": {"total": 4, "earliest": 0}}
        pub = public_job(job)
        self.assertNotIn("owner", pub)
        self.assertNotIn("output", pub)

    def test_validate_job_id(self):
        self.assertEqual(validate_job_id("bash-1"), "bash-1")
        with self.assertRaises(ValueError):
            validate_job_id("")
        with self.assertRaises(ValueError):
            validate_job_id(None)

    def test_status_line_with_detail(self):
        self.assertEqual(status_line({"status": "failed", "detail": "boom"}),
                         "[status: failed, boom]")


class JobsToolsTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="jobs-tools")
        self.agents = install_agents(self.ctx)
        self.registry = LocalJobRegistry(self.ctx)
        self.registry.attach_controller("tool-jobs")
        self.owner = _make_owner(self.agents, "o1")
        self.exec = type("Exec", (), {"agent": self.owner,
                                      "signal": threading.Event()})()
        self.tools = ToolRegistry(self.ctx)
        register_job_tools(self.tools, self.registry)

    def _call(self, name, args, exec_=None):
        return asyncio.run(self.tools.resolve(name).execute(args, exec_ or self.exec))

    def _render(self, name, value):
        return self.tools.resolve(name).render(value)

    def test_job_list_format(self):
        _tid, _box, _h = _start(self.registry, owner=self.owner, label="sleep")
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
        tid, _box, holder = _start(self.registry, owner=self.owner, label="sleep")
        holder["handle"].append("progress")
        canonical = self._call("job_output", {"job_id": tid})
        self.assertEqual(canonical["text"], "progress")
        self.assertEqual(canonical["job"]["status"], "running")
        rendered = self._render("job_output", canonical)
        body = rendered[0]["text"]
        self.assertTrue(body.startswith("progress"))
        self.assertTrue(body.endswith("[status: running]"))

    def test_job_output_renders_result_once_after_settlement(self):
        tid, box, holder = _start(self.registry, owner=self.owner, label="sleep")
        holder["handle"].append("streamed")
        box.settle({"status": "completed", "result": "final"})
        first = self._call("job_output", {"job_id": tid})
        self.assertEqual(first["text"], "streamed\nfinal")
        second = self._call("job_output", {"job_id": tid})
        self.assertEqual(second["text"], "")

    def test_job_output_unknown_job(self):
        with self.assertRaises(RuntimeError):
            self._call("job_output", {"job_id": "nope-1"})

    def test_job_output_other_session_fenced(self):
        tid, _box, _h = _start(self.registry, owner=self.owner, label="sleep")
        other = type("Exec", (), {"agent": _make_owner(self.agents, "bob"),
                                  "signal": threading.Event()})()
        with self.assertRaises(RuntimeError):
            self._call("job_output", {"job_id": tid}, other)

    def test_job_kill_flows(self):
        tid, box, _h = _start(self.registry, owner=self.owner, label="sleep")
        canonical = self._call("job_kill", {"job_id": tid})
        self.assertEqual(canonical["outcome"], "cancellation-requested")
        self.assertEqual(canonical["job"]["id"], tid)
        rendered = self._render("job_kill", canonical)
        self.assertEqual(rendered,
                         [{"type": "text", "text": f"requested cancellation of job {tid}"}])
        box.settle({"status": "killed"})
        canonical2 = self._call("job_kill", {"job_id": tid})
        self.assertEqual(canonical2["outcome"], "already-finished")
        rendered2 = self._render("job_kill", canonical2)
        self.assertIn("already finished", rendered2[0]["text"])

    def test_job_kill_with_reason(self):
        tid, _box, _h = _start(self.registry, owner=self.owner, label="sleep")
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
        return asyncio.run(run_pipeline_async(
            self.ctx, self.tools.resolve(name), args, ToolExec(agent=self.owner)))

    def _block_text(self, content):
        if isinstance(content, (tuple, list)) and len(content) == 1:
            text = content[0].get("text") if hasattr(content[0], "get") else None
            return text if isinstance(text, str) else None
        return None

    def test_finalize_producer_limit_bounds_body_and_status(self):
        _tid, _box, holder = _start(self.registry, owner=self.owner, label="sleep",
                                    limit=48)
        holder["handle"].append("界" * 100)
        result = self._pipeline("job_output", {"job_id": "bash-1"})
        self.assertTrue(result.ok)
        text = self._block_text(result.content)
        self.assertIsNotNone(text)
        self.assertLessEqual(len(text.encode("utf-8")), 48)
        self.assertIn("[output truncated]", text)
        self.assertIn("[status: running]", text)

    def test_finalize_producer_limit_preserves_empty_and_newline_terminated(self):
        _tid, _box, holder = _start(self.registry, owner=self.owner, label="sleep",
                                    limit=64)
        r1 = self._pipeline("job_output", {"job_id": "bash-1"})
        self.assertEqual(self._block_text(r1.content), "(no new output)\n[status: running]")
        holder["handle"].append("line\n")
        r2 = self._pipeline("job_output", {"job_id": "bash-1"})
        self.assertEqual(self._block_text(r2.content), "line\n[status: running]")

    def test_finalize_deny_flow_through_pipeline(self):
        self.ctx.on("tools/pre-execute",
                    lambda p, nxt: {"kind": "deny"} if p["tool"] == "job_output" else nxt(p))
        _tid, _box, _h = _start(self.registry, owner=self.owner, label="sleep", limit=64)
        denied = self._pipeline("job_output", {"job_id": "bash-1"})
        self.assertTrue(denied.is_error)
        self.assertEqual(denied.error, "denied by tools/pre-execute")

    def test_finalize_wired_on_job_controls_only(self):
        self.assertIsNotNone(self.tools.resolve("job_output").finalize_content)
        self.assertIsNotNone(self.tools.resolve("job_kill").finalize_content)
        self.assertIsNone(self.tools.resolve("job_list").finalize_content)

    def test_finalize_bounds_long_error_text(self):
        _tid, _box, _h = _start(self.registry, owner=self.owner, label="sleep", limit=64)
        hook = self.tools.resolve("job_output").finalize_content
        exec_ = ToolExec(agent=self.owner, name="job_output", arguments={"job_id": "bash-1"})
        bounded = hook(exec_, {"content": [{"type": "text", "text": "denied: " + "d" * 1000}],
                               "value": None, "is_error": True})
        self.assertLessEqual(len(bounded[0]["text"].encode("utf-8")), 64)
        self.assertIn("[result truncated]", bounded[0]["text"])

    def test_finalize_policy_replaced_content_bounded_without_status(self):
        _tid, _box, _h = _start(self.registry, owner=self.owner, label="sleep", limit=64)
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
        _tid, _box, holder = _start(self.registry, owner=self.owner, label="sleep")
        holder["handle"].append("hello")
        result = self._pipeline("job_output", {"job_id": "bash-1"})
        self.assertTrue(result.ok)
        self.assertEqual(self._block_text(result.content), "hello\n[status: running]")


class RealLoopIntegrationTest(unittest.TestCase):
    def _loop_ctx(self, name, session_id, adapter, jobs_config=None):
        ctx = Context(name=name)
        install_sessions(ctx)
        install_agents(ctx)
        loop = AgentLoop(Session(session_id), adapter, ToolRegistry(ctx), ctx)
        loop.publish()
        install_jobs(ctx, jobs_config)
        return ctx, loop

    def test_wakeup_opens_turn_on_real_loop(self):
        ctx, loop = self._loop_ctx("jobs-real", "jobs-real-1", _TextAdapter("done"))
        registry = ctx.get("jobs")
        tid, box, _h = _start(registry, owner=loop, label="long")
        self.assertEqual(loop.status, "idle")
        box.settle({"status": "completed"})
        self.assertEqual(loop.status, "idle")
        notices = [ev for ev in loop.session.events
                   if ev["type"] == "user/message" and "background job" in str(ev["data"])]
        self.assertTrue(notices, "notice 应作为 plugin user/message 落日志")
        self.assertEqual(len(loop.inbox), 0)
        self.assertIn(tid, [j["id"] for j in registry.list(loop)])

    def test_real_loop_user_claim_resets_budget(self):
        ctx, loop = self._loop_ctx("jobs-real-2", "jobs-real-2", _TextAdapter("done"),
                                   {"maxConsecutiveWakes": 3})
        registry = ctx.get("jobs")
        wakes = {"n": 0}
        orig = loop.followup

        def _count(content, source="user"):
            if source == "tool-jobs":
                wakes["n"] += 1
            return orig(content, source=source)

        loop.followup = _count

        def _settle(label):
            _tid, box, _h = _start(registry, owner=loop, label=label)
            box.settle({"status": "completed"})

        for i in range(4):
            _settle(f"j{i}")
        # 预算 3：前三次 wakeup，第四次 inject
        self.assertEqual(wakes["n"], 3)
        # 真实 loop 认领 user 输入（同步 pump 开 turn）→ agent/inbox/claimed
        # user 源 → 安装 scope 的订阅复位预算
        loop.followup("hi", source="user")
        self.assertEqual(loop.status, "idle")
        _settle("j4")
        self.assertEqual(wakes["n"], 4)

    def test_job_tools_registered_through_default_tools(self):
        from miniharness.cli.default_tools import default_tools
        ctx = Context(name="jobs-def")
        install_agents(ctx)
        install_jobs(ctx)
        reg = default_tools(ctx)
        for name in ("job_output", "job_list", "job_kill"):
            self.assertIsNotNone(reg.resolve(name))


class ScopeLayeringTest(unittest.TestCase):
    """controller/事件订阅按注册 scope 分层（对齐 jobs-local ScopedLayers 语义）。"""

    def setUp(self):
        self.ctx = Context(name="jobs-scope")
        self.agents = install_agents(self.ctx)
        self.registry = LocalJobRegistry(self.ctx)
        self.scope_a = self.ctx.create_scope("preset-a")
        self.scope_b = self.ctx.create_scope("preset-b")

    def _owner(self, owner_id: str, scope):
        return _make_owner(self.agents, owner_id, scope.ctx)

    def _spec(self, owner=None):
        return {"kind": "bash", "label": "sleep 60",
                **({"owner": owner} if owner is not None else {}),
                "run": lambda job: {"done": JobDoneBox(), "cancel": lambda r=None: None}}

    def test_no_controller_rejected_verbatim(self):
        owner = self._owner("a1", self.scope_a)
        with self.assertRaises(RuntimeError) as cm:
            self.registry.start(self._spec(owner))
        self.assertEqual(
            str(cm.exception),
            "background jobs unavailable: no job controller serves this agent "
            "(load @deepseek-ai/dsh-tool-jobs in its composition)")

    def test_scoped_controller_serves_only_its_subtree(self):
        self.registry.attach_controller("tool-jobs", self.scope_a.ctx)
        served = self._owner("served", self.scope_a)
        unserved = self._owner("unserved", self.scope_b)
        job_id = self.registry.start(self._spec(served))
        self.assertTrue(job_id.startswith("bash-"))
        with self.assertRaises(RuntimeError):
            self.registry.start(self._spec(unserved))

    def test_scoped_controller_does_not_serve_unowned(self):
        self.registry.attach_controller("tool-jobs", self.scope_a.ctx)
        with self.assertRaises(RuntimeError):
            self.registry.start(self._spec(None))

    def test_global_controller_serves_everyone(self):
        self.registry.attach_controller("host", self.ctx)
        self.assertTrue(self.registry.start(self._spec(None)))
        self.assertTrue(self.registry.start(self._spec(self._owner("a1", self.scope_a))))
        self.assertTrue(self.registry.start(self._spec(self._owner("b1", self.scope_b))))

    def test_descendant_scope_owner_served_by_ancestor_controller(self):
        inner = self.scope_a.ctx.create_scope("inner-agent")
        self.registry.attach_controller("tool-jobs", self.scope_a.ctx)
        job_id = self.registry.start(self._spec(self._owner("deep", inner)))
        self.assertTrue(job_id.startswith("bash-"))

    def test_event_scope_relative_delivery(self):
        self.registry.attach_controller("tool-jobs", self.scope_a.ctx)
        self.registry.attach_controller("tool-jobs-b", self.scope_b.ctx)
        seen_a: list = []
        seen_b: list = []
        self.registry.events_for(self.scope_a.ctx).subscribe({"owners": "scope"},
                                                             seen_a.append)
        self.registry.events_for(self.scope_b.ctx).subscribe({"owners": "scope"},
                                                             seen_b.append)
        box = JobDoneBox()
        owner_a = self._owner("a1", self.scope_a)
        self.registry.start({
            "kind": "bash", "label": "x", "owner": owner_a,
            "run": lambda job: {"done": box, "cancel": lambda r=None: None}})
        box.settle({"status": "completed"})
        self.assertTrue(any(e["type"] == "settled" for e in seen_a))
        self.assertEqual(seen_b, [])

    def test_unowned_settlement_skips_scoped_listener(self):
        scoped_seen: list = []
        global_seen: list = []
        self.registry.attach_controller("host", self.ctx)
        self.registry.events_for(self.scope_a.ctx).subscribe({"owners": "scope"},
                                                             scoped_seen.append)
        self.registry.events_for(self.ctx).subscribe({"owners": "scope"},
                                                     global_seen.append)
        box = JobDoneBox()
        self.registry.start({
            "kind": "bash", "label": "bg",
            "run": lambda job: {"done": box, "cancel": lambda r=None: None}})
        box.settle({"status": "completed"})
        self.assertEqual(scoped_seen, [])
        self.assertTrue(any(e["type"] == "settled" for e in global_seen))

    def test_controller_detaches_on_scope_dispose(self):
        self.registry.attach_controller("tool-jobs", self.scope_a.ctx)
        owner = self._owner("a1", self.scope_a)
        self.scope_a.dispose()
        with self.assertRaises(RuntimeError):
            self.registry.start(self._spec(owner))

    def test_duplicate_names_independent_detach(self):
        d1 = self.registry.attach_controller("a")
        d2 = self.registry.attach_controller("a")
        d1()
        self.assertTrue(self.registry.start(self._spec(None)))
        d2()
        with self.assertRaises(RuntimeError):
            self.registry.start(self._spec(None))


class ArchiveAdmissionTest(unittest.TestCase):
    """job 家族并入 workspace 归档准入（对齐 archive-admission.ts）。"""

    def setUp(self):
        self.ctx = Context(name="jobs-archive")
        self.agents = install_agents(self.ctx)
        self.registry = LocalJobRegistry(self.ctx)
        self.registry.attach_controller("test", self.ctx)

    def _activity(self, session_id):
        return self.ctx.waterfall(
            "workspace/session-activity", {"sessionId": session_id},
            base=lambda _p: [])

    def test_activity_reports_running_owned_jobs(self):
        _make_owner(self.agents, "s1")
        tid, box, _ = _start(self.registry, owner="s1", label="Build")
        activity = self._activity("s1")
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity[0].kind, "job")
        self.assertEqual([item.id for item in activity[0].items], [tid])
        self.assertEqual(activity[0].items[0].label, "Build")
        # 别的会话与结算后的作业都不报
        self.assertEqual(self._activity("s2"), [])
        box.settle({"status": "completed"})
        self.assertEqual(self.registry.get(tid, "s1")["status"], "completed")
        self.assertEqual(self._activity("s1"), [])

    def test_stop_kills_running_owned_jobs(self):
        _make_owner(self.agents, "s1")
        tid, box, holder = _start(self.registry, owner="s1")
        reasons: list = []
        holder["cancel"] = lambda r=None: reasons.append(r)
        self.ctx.parallel("workspace/session-stop", {"sessionId": "s1"})
        self.assertEqual(reasons, ["session archived"])
        self.assertEqual(self.registry.get(tid, "s1")["status"], "stopping")
        box.settle({"status": "killed"})

    def test_stop_ignores_other_session(self):
        _make_owner(self.agents, "s1")
        tid, box, holder = _start(self.registry, owner="s1")
        reasons: list = []
        holder["cancel"] = lambda r=None: reasons.append(r)
        self.ctx.parallel("workspace/session-stop", {"sessionId": "s2"})
        self.assertEqual(reasons, [])
        self.assertEqual(self.registry.get(tid, "s1")["status"], "running")
        box.settle({"status": "completed"})


if __name__ == "__main__":
    unittest.main()
