"""进程内后台作业注册表（对齐 packages/jobs/jobs-local/src/{index,view}.ts）。

语义（与上游逐条一致）：
  * 一条注册 = `ctx.jobs` 服务（每 context 单实现，重复提供 fail loud）
  * registrations 存续于 producer/controller fiber 之外；owner 或服务销毁取消并
    等待在飞作业，throw 的 teardown cancel 只 force-fail 记录
  * owned-job 访问按 owner **会话 id** 栅栏（`!== undefined` 语义：unowned 开放、
    无会话调用方永不匹配 owned）；owner 在 start 时解析为 live Agent（供 scope
    路由与 owner 清理），访问面只看会话 id
  * 结算 first-wins：一条终态记录 + 一次 `settled{cause,awaited}` 通知 + 尾随
    `output`（对迟到的 producer 结算免疫）
  * start 在无已挂 controller 服务该 owner 时拒绝；controller/订阅者按**注册
    scope 分层**——全局层服务所有 owner，scoped 层只服务 owner 链上的成员；
    unowned 只有全局层能接
  * 每条作业一个输出 ring：spec 的 pull 源由注册表按 cadence 泵入，结算前排干
    一次；模型消费游标与观察者绝对偏移读同一批字节且互不打扰；保留有界

载体说明（登录 verified-diffs）：
  * owner 经 `ctx.get('agents')` 解析为 live Agent；未安装 agents 的裸装配拒绝
    owned 注册（逐字对齐 jobs-local index.ts:359-366 的补救指引）
  * done 用 JobDoneBox/Future 承载 Promise 语义；teardown 排干逐任务等 settled
    （对齐 `await Promise.all(settled)`：无时间上限，producer 契约要求响应
    cancel 并最终结算）
  * pump 以线程承载上游 async 循环；首次排干同步完成以保持"registered 先于泵"
"""
from __future__ import annotations

import threading
import time
from typing import Any

from ..core.session import now_ms
from ..core.scope import Context
from ..core.dsh_scope import ScopedLayers, scope_of
from .archive_admission import install_job_archive_admission
from .events import JobEventHub, JobLayer
from .pump import start_pump
from .ring import OutputRing
from .types import (
    DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER,
    DEFAULT_PUMP_POLL_MS,
    DEFAULT_RETAIN_BYTES,
    DEFAULT_SETTLED_RETAIN_BYTES,
    JobHandle,
    TERMINAL_STATUSES,
    job_id,
)
from .view import build_view

__all__ = ["LocalJobRegistry", "TASK_WAIT_TIMEOUT"]

#: 区分"等待超时"与"调用方取消"的 scoped deadline 码（jobs-local index.ts:30）。
TASK_WAIT_TIMEOUT = "TASK_WAIT_TIMEOUT"


def _signal_aborted(signal: Any) -> bool:
    """两种 signal 形状的统一判读：_AbortProxy.aborted 或 threading.Event.is_set。"""
    if signal is None:
        return False
    aborted = getattr(signal, "aborted", None)
    if aborted is not None:
        return bool(aborted)
    is_set = getattr(signal, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    return False


def _is_positive_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 1


def _is_positive_number(value: Any) -> bool:
    return (isinstance(value, (int, float)) and not isinstance(value, bool)
            and value > 0 and _finite(value))


def _finite(value: Any) -> bool:
    try:
        import math
        return math.isfinite(value)
    except TypeError:
        return False


class _BoundEvents:
    """绑定到某个注册上下文的 `events` 视图（对齐上游 get events 的 scope 语义）。"""

    __slots__ = ("_registry", "_ctx")

    def __init__(self, registry: "LocalJobRegistry", ctx) -> None:
        self._registry = registry
        self._ctx = ctx

    def subscribe(self, filter_: dict, listener) -> object:
        """注册一个 effect-scoped 监听器；返回注销 disposer。"""
        return self._registry._hub.subscribe(self._ctx, filter_, listener)


class LocalJobRegistry:
    """内存 `jobs` 服务：每条记录只在内部可变，对外只给新鲜投影。"""

    def __init__(self, ctx: Context, config: dict | None = None):
        self.ctx = ctx
        cfg = config or {}
        unknown = set(cfg) - {"maxConcurrentJobsPerOwner", "retainBytes",
                              "settledRetainBytes", "pumpPollMs"}
        if unknown:
            raise ValueError(f"unknown jobs registry config keys: {sorted(unknown)!r}")
        self.max_concurrent_jobs_per_owner = cfg.get(
            "maxConcurrentJobsPerOwner", DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER)
        self.retain_bytes = cfg.get("retainBytes", DEFAULT_RETAIN_BYTES)
        self.settled_retain_bytes = cfg.get("settledRetainBytes", DEFAULT_SETTLED_RETAIN_BYTES)
        self.pump_poll_ms = cfg.get("pumpPollMs", DEFAULT_PUMP_POLL_MS)
        for name, value in (("maxConcurrentJobsPerOwner", self.max_concurrent_jobs_per_owner),
                            ("retainBytes", self.retain_bytes),
                            ("settledRetainBytes", self.settled_retain_bytes),
                            ("pumpPollMs", self.pump_poll_ms)):
            if not _is_positive_int(value):
                raise ValueError(f"invalid {name}: expected a positive integer, got {value!r}")
        self._store: dict[str, dict] = {}
        self._counters: dict[str, int] = {}
        # controller/订阅者按注册 scope 分层（对齐 ScopedLayers<JobLayer>）。
        self._layers = ScopedLayers(lambda _scope: JobLayer(), lambda: None)
        self._hub = JobEventHub(self._layers, self._warn)
        # owner agent（按 id()）→ 挂在 owner scope 上的 cleanup disposer。
        self._owner_cleanups: dict[int, Any] = {}
        ctx.provide("jobs", self)
        ctx.effect(lambda: self._dispose_all, "jobs registry teardown")
        install_job_archive_admission(ctx, self)

    # ---------- 事件面 ----------

    @property
    def events(self) -> _BoundEvents:
        """绑定注册上下文的事件视图（对齐上游 `get events`）。"""
        return _BoundEvents(self, self.ctx)

    def events_for(self, ctx) -> _BoundEvents:
        """绑定任意注册上下文的事件视图（mini 显式 scope 载体）。"""
        return _BoundEvents(self, ctx)

    def attach_controller(self, name: str, ctx: Any = None) -> Any:
        """挂一个可读/停作业的 controller；返回注销 disposer。

        `ctx` 是注册方上下文：其 scope 层持有此贡献，scope 销毁时随 fiber 自动
        卸载。缺省=注册表自身 ctx（通常为组合根 → 全局层，服务所有 owner）。
        同名可独立卸。
        """
        token = object()
        return self._layers.effect(
            ctx if ctx is not None else self.ctx,
            lambda layer: layer.controllers.append(token),
            label="jobs.attachController()",
        )

    # ---------- 服务面 ----------

    def start(self, spec: dict) -> str:
        """启动前完整 preflight，之后原子注册，注册后不可失败。返回 `<kind>-N`。"""
        owner_session = self._caller_session(spec.get("owner"))
        owner_agent = self._resolve_owner(owner_session)
        if not self._serves_owner(owner_agent):
            # 逐字对齐上游（jobs-local index.ts:209；括注指向上游补救插件）
            raise RuntimeError(
                "background jobs unavailable: no job controller serves this agent "
                "(load @deepseek-ai/dsh-tool-jobs in its composition)"
            )
        kind = spec["kind"]
        label = spec["label"]
        if not kind:
            raise ValueError("invalid job kind: expected a non-empty string")
        if not label:
            raise ValueError("invalid job label: expected a non-empty string")
        limit = spec.get("outputLimitBytes")
        if limit is not None and not _is_positive_int(limit):
            raise ValueError(
                f"invalid outputLimitBytes: expected a positive safe integer, got {limit!r}")
        if owner_agent is not None:
            self._ensure_owner_cleanup(owner_agent)
        if self._active_task_count(owner_agent) >= self.max_concurrent_jobs_per_owner:
            raise RuntimeError(
                f"background job limit reached for this owner "
                f"(limit: {self.max_concurrent_jobs_per_owner}); "
                "use job_kill to stop an unneeded job, wait for it to finish, then retry"
            )

        count = self._counters.get(kind, 0) + 1
        self._counters[kind] = count
        task_id = job_id(kind, count)
        ring = OutputRing()
        state: dict = {"progress": None, "job": None}
        handle = JobHandle(
            task_id,
            lambda text, options=None: self._append_ring(state, ring, text, options, "producer"),
            lambda line: self._update_progress(state, line),
        )
        # starter 同步执行；throw 则不留任何注册（消耗的序号被跳过）。
        hooks = spec["run"](handle)
        done = hooks["done"]
        cancel = hooks["cancel"]
        if not (hasattr(done, "add_done_callback") and hasattr(done, "result")):
            raise TypeError("hooks['done'] must expose add_done_callback()/result() (JobDoneBox or Future)")
        job: dict = {
            "id": task_id,
            "kind": kind,
            "label": label,
            "outputLimitBytes": limit,
            "owner": owner_session,
            "owner_agent": owner_agent,
            "cancel": cancel,
            "status": "running",
            "ring": ring,
            "modelCursor": 0,
            "resultDelivered": False,
            "state": state,
            "detail": None,
            "result": None,
            "startedAt": now_ms(),
            "finishedAt": None,
            "killReason": None,
            "settleCause": None,
            "settled": threading.Event(),
            "waiters": 0,
            "pump": None,
            "pumpUntil": threading.Event(),
            "spillPaths": [],
        }
        state["job"] = job
        self._store[task_id] = job
        # 注册提交即发布；pump 随后创建（其同步首排干可能 announce output，
        # 故一条作业的首个事件恒为 registered）。
        self._emit({"type": "registered", "job": self._view(job)}, owner_agent)
        if spec.get("output"):
            job["pump"] = start_pump(
                [self._guard_source(job, source) for source in spec["output"]],
                {
                    "append": lambda text, options=None: self._append_ring(
                        state, ring, text, options, "pump"),
                    "spill": lambda index, path: self._spill(job, index, path),
                },
                self.pump_poll_ms,
                job["pumpUntil"],
            )

        def on_producer_settle() -> None:
            outcome = self._extract_outcome(job, done)
            if job["pump"] is not None:
                job["pumpUntil"].set()
                job["pump"].wait()
            self._settle(job, outcome, job["settleCause"] or "producer")

        # 等价 hooks.done.then(settle, reject → failed)（producer 契约违例也要收场）。
        done.add_done_callback(lambda _box: on_producer_settle())
        return task_id

    def list(self, caller: Any = None) -> list[dict]:
        """按注册序列出 caller 可见的作业（owned 只给同会话，unowned 全开放）。"""
        session = self._caller_session(caller)
        return [
            self._view(j) for j in self._store.values()
            if j["owner"] is None or j["owner"] == session
        ]

    def get(self, task_id: str, caller: Any = None) -> dict:
        """非消耗性投影；未知/外会话作业 fail loud。"""
        return self._view(self._expect(task_id, caller))

    def read(self, task_id: str, caller: Any = None) -> dict:
        """从模型游标消费 ring 并推进游标；终态后的首次读还携带 result。"""
        job = self._expect(task_id, caller)
        read = job["ring"].read_from(job["modelCursor"])
        job["modelCursor"] = job["ring"].total
        result = job["result"] if (
            job["status"] in TERMINAL_STATUSES and not job["resultDelivered"]) else None
        if result is not None:
            job["resultDelivered"] = True
        # 终态读即 settled 流降到 settled cap 的时点（settlement 为它保下未消费字节）。
        if job["status"] in TERMINAL_STATUSES:
            job["ring"].trim(self.settled_retain_bytes)
        out: dict = {"chunks": read["chunks"], "lossy": read["lossy"], "job": self._view(job)}
        if result is not None:
            out["result"] = result
        return out

    def read_at(self, task_id: str, from_byte: int, caller: Any = None) -> dict:
        """不移动模型游标地读保留输出；`from` 为负或非整数 fail loud。"""
        job = self._expect(task_id, caller)
        if isinstance(from_byte, bool) or not isinstance(from_byte, int) or from_byte < 0:
            raise ValueError(
                f"invalid output read offset: expected a non-negative safe integer, "
                f"got {from_byte!r}")
        return job["ring"].read_from(from_byte)

    def kill(self, task_id: str, caller: Any = None, reason: str | None = None) -> str:
        """请求取消并置 stopping；producer 抛错不改状态即传播。reason 并入 killed detail。"""
        return self._kill_job(self._expect(task_id, caller), reason)

    def wait(self, task_id: str, timeout_ms: float, caller: Any = None,
             signal: Any = None) -> dict:
        """等结算或超时，不取消作业；超时返回运行态投影，abort 只在在飞时抛。"""
        job = self._expect(task_id, caller)
        if not _is_positive_number(timeout_ms):
            raise ValueError(
                f"invalid wait timeout: expected a positive number of milliseconds, "
                f"got {timeout_ms!r}")
        if job["status"] not in TERMINAL_STATUSES:
            if _signal_aborted(signal):
                raise RuntimeError("wait aborted")
            # waiter 计数让 settle 判定 awaited（释放了在场等待），抑制完成 notice。
            job["waiters"] += 1
            try:
                deadline = time.monotonic() + timeout_ms / 1000
                while job["status"] not in TERMINAL_STATUSES:
                    if _signal_aborted(signal):
                        raise RuntimeError("wait aborted")
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.02, remaining))
            finally:
                job["waiters"] -= 1
        return self._view(job)

    def remove(self, task_id: str, caller: Any = None) -> None:
        """丢弃一条已结算作业的记录并 announce removed；仍在运行则 fail loud。"""
        job = self._expect(task_id, caller)
        if job["status"] not in TERMINAL_STATUSES:
            raise RuntimeError(
                f"job {task_id} is still {job['status']}; "
                "kill it and wait for settlement before removing it")
        self._drop([job])

    # ---------- 内部：解析与访问 ----------

    @staticmethod
    def _caller_session(caller: Any):
        """调用方 → SessionId（mini 为裸字符串；容忍 Agent 对象取 .id）。"""
        if caller is None or isinstance(caller, str):
            return caller
        return getattr(caller, "id", None)

    def _resolve_owner(self, session: Any):
        """把 spec 的 owner 会话解析为其 live Agent（ownership 要求 live 实例）。"""
        if session is None:
            return None
        agents = self.ctx.get("agents")
        if agents is None:
            raise RuntimeError(
                "background job ownership requires the agent registry "
                "(load @deepseek-ai/dsh-agent)")
        owner = agents.get(session)
        if owner is None:
            raise RuntimeError(
                f'session "{session}" has no live agent (background job owner must be live)')
        return owner

    def _serves_owner(self, owner_agent) -> bool:
        """是否有已挂 controller 能收停 `owner` 的作业：全局层服务所有 owner；
        否则沿 owner 链找任一有 controller 的 scoped 层。unowned 只有全局层能接。"""
        if not self._layers.global_layer.controllers.isEmpty():
            return True
        scope = scope_of(owner_agent.ctx) if owner_agent is not None else None
        return any(
            not layer.controllers.isEmpty()
            for layer in self._layers.chain_layers(scope)
        )

    def _expect(self, task_id: str, caller: Any) -> dict:
        job = self._store.get(task_id)
        if job is None:
            raise RuntimeError(f"unknown job {task_id}")
        self._assert_access(job, caller)
        return job

    def _assert_access(self, job: dict, caller: Any) -> None:
        session = self._caller_session(caller)
        if job["owner"] is not None and job["owner"] != session:
            raise RuntimeError(f"job {job['id']} belongs to another session")

    def _active_task_count(self, owner_agent) -> int:
        return sum(
            1 for j in self._store.values()
            if j["owner_agent"] is owner_agent and j["status"] in ("running", "stopping")
        )

    # ---------- 内部：投影与事件 ----------

    def _view(self, job: dict) -> dict:
        spill = list(dict.fromkeys(
            path for path in job["spillPaths"] if path is not None))
        return build_view(
            id=job["id"], kind=job["kind"], label=job["label"],
            owner=job["owner"], output_limit_bytes=job["outputLimitBytes"],
            status=job["status"], progress=job["state"]["progress"],
            detail=job["detail"], started_at=job["startedAt"],
            finished_at=job["finishedAt"],
            total=job["ring"].total, earliest=job["ring"].earliest,
            spill_paths=spill,
        )

    def _emit(self, event: dict, owner_agent) -> None:
        self._hub.emit(event, owner_agent)

    def _emit_output(self, job: dict) -> None:
        event: dict = {"type": "output", "id": job["id"], "total": job["ring"].total}
        if job["owner"] is not None:
            event["owner"] = job["owner"]
        self._emit(event, job["owner_agent"])

    # ---------- 内部：producer 面 ----------

    def _append_ring(self, state: dict, ring: OutputRing, text: str,
                     options: dict | None, writer: str) -> None:
        """追加一块到 ring；已结算作业的 producer 写记日志并丢弃。"""
        job = state["job"]
        if job is not None and job["status"] in TERMINAL_STATUSES:
            if writer == "producer":
                self._warn(f"jobs: append to settled job {job['id']} dropped")
            return
        if not ring.append(text, options, self.retain_bytes):
            return
        if job is not None:
            self._emit_output(job)

    def _update_progress(self, state: dict, line: str) -> None:
        """替换 live 进度行；已结算作业的写记日志并丢弃。"""
        job = state["job"]
        if job is not None and job["status"] in TERMINAL_STATUSES:
            self._warn(f"jobs: progress update on settled job {job['id']} dropped")
            return
        state["progress"] = line
        if job is not None:
            self._emit({"type": "progress", "job": self._view(job)}, job["owner_agent"])

    def _guard_source(self, job: dict, source: dict) -> dict:
        """包含失败的 pull 源：首次抛错记日志，此后该源视为耗尽。"""
        failed = {"value": False}

        def read(from_byte):
            if failed["value"]:
                return {"text": "", "nextOffset": from_byte, "lossy": False}
            try:
                return source["read"](from_byte)
            except Exception as error:  # noqa: BLE001 - 源失败被包含，作业继续自行结算
                failed["value"] = True
                self._warn(
                    f"jobs: output source for {job['id']} failed; "
                    f"its stream stops here: {error}")
                return {"text": "", "nextOffset": from_byte, "lossy": False}

        guarded: dict = {"read": read}
        if source.get("channel") is not None:
            guarded["channel"] = source["channel"]
        return guarded

    def _spill(self, job: dict, index: int, path) -> None:
        while len(job["spillPaths"]) <= index:
            job["spillPaths"].append(None)
        job["spillPaths"][index] = path

    # ---------- 内部：结算与清理 ----------

    def _extract_outcome(self, job: dict, done: Any) -> dict:
        try:
            value = done.result()
        except BaseException as error:  # noqa: BLE001 - producer 契约违例被收编
            self._warn(
                f"jobs: job {job['id']} producer done promise rejected "
                f"(producer contract violation): {error}")
            return {"status": "failed", "detail": str(error)}
        if not isinstance(value, dict) or value.get("status") not in TERMINAL_STATUSES:
            return {"status": "failed",
                    "detail": f"producer done settled with invalid outcome: {value!r}"}
        outcome: dict = {"status": value["status"]}
        if value.get("detail") is not None:
            outcome["detail"] = value["detail"]
        if value.get("result") is not None:
            outcome["result"] = value["result"]
        return outcome

    def _kill_job(self, job: dict, reason: str | None) -> str:
        if job["status"] in TERMINAL_STATUSES:
            return "already-finished"
        # 先 cancel：throw 让生命周期状态保持原样
        job["cancel"](reason)
        job["status"] = "stopping"
        # 后写者胜（对齐上游）：detail 报最新一次 kill 意图
        if reason is not None:
            job["killReason"] = reason
        job["settleCause"] = "kill"
        self._emit({"type": "stopping", "job": self._view(job)}, job["owner_agent"])
        return "requested"

    def _settle(self, job: dict, outcome: dict, cause: str) -> None:
        if job["status"] in TERMINAL_STATUSES:
            return  # first-wins：对迟到的 producer 结算免疫
        job["status"] = outcome["status"]
        # killed 结算把记录的 kill reason 并入 detail（producer 事实在前）；一个跑赢了
        # kill 请求的作业（completed/failed）只保留 producer detail。
        if outcome["status"] == "killed" and job["killReason"] is not None:
            detail = outcome.get("detail")
            job["detail"] = f"{detail}; {job['killReason']}" if detail else job["killReason"]
        elif outcome.get("detail") is not None:
            job["detail"] = outcome["detail"]
        job["state"]["progress"] = None
        job["result"] = outcome.get("result")
        job["finishedAt"] = now_ms()
        # 结算终结流：在观察者读终态投影前裁到 settled cap，但绝不裁到模型游标
        # 之下（作业在首次模型读前完成时保留活 cap 下的全部字节）。
        job["ring"].trim(max(self.settled_retain_bytes,
                             job["ring"].total - job["modelCursor"]))
        # 放行任何在飞等待：awaited 表示这次结算释放了一个 live wait。
        awaited = job["waiters"] > 0
        job["pumpUntil"].set()
        job["settled"].set()
        self._emit({"type": "settled", "job": self._view(job),
                    "cause": cause, "awaited": awaited}, job["owner_agent"])
        # ring 的流随结算结束；信号跟在已提交结算之后，醒来的观察者读到终态。
        self._emit_output(job)

    def _ensure_owner_cleanup(self, owner_agent) -> None:
        """经精确 owner 的 scope 挂一个 awaited cleanup；service teardown 可 detach。"""
        key = id(owner_agent)
        if key in self._owner_cleanups:
            return

        def detach() -> None:
            self._owner_cleanups.pop(key, None)
            self._dispose_owned(owner_agent)

        # attach 成功后才记录（正在拆解的 scope 拒绝新 effect）。
        disposer = owner_agent.ctx.effect(
            lambda: detach, f"jobs.ownerCleanup({getattr(owner_agent, 'id', '?')})")
        self._owner_cleanups[key] = disposer

    def _dispose_owned(self, owner_agent) -> None:
        owned = [j for j in self._store.values() if j["owner_agent"] is owner_agent]
        self._cancel_for_teardown(owned, "owner disposed")
        self._drain(owned)
        self._drop(owned)

    def _dispose_all(self) -> None:
        all_jobs = list(self._store.values())
        self._cancel_for_teardown(all_jobs, "jobs service disposed")
        self._drain(all_jobs)
        self._drop(all_jobs)
        # 共享 store 静止后再 detach 跨 fiber 的 owner effect。
        cleanups = list(self._owner_cleanups.values())
        self._owner_cleanups.clear()
        for cleanup in cleanups:
            cleanup()

    def _drain(self, jobs: list[dict]) -> None:
        """等 producer 到达静止（对齐上游 await Promise.all(settled)：调用方已先
        cancel-all，此处逐任务等 settled 事件、无时间上限——producer 永不结算会
        挂起 teardown，上游同款限制；producer 契约要求响应 cancel 并最终结算）。"""
        for job in jobs:
            job["settled"].wait()

    def _cancel_for_teardown(self, jobs: list[dict], reason: str) -> None:
        for job in jobs:
            if job["status"] in TERMINAL_STATUSES:
                continue
            # 从此无论谁结算它，owner/服务都在销毁：cause='teardown' 让完成报告者
            # 不去打扰已不存在的读者。
            job["settleCause"] = "teardown"
            try:
                job["cancel"](reason)
                job["status"] = "stopping"
                self._emit({"type": "stopping", "job": self._view(job)}, job["owner_agent"])
            except Exception as error:  # noqa: BLE001 - 单任务 cancel 抛错被包含
                detail = f"cancel threw during teardown; work may be orphaned: {error}"
                self._warn(
                    f"jobs: cancel of {job['id']} threw during teardown; "
                    f"job record forced failed and work may be orphaned: {error}")
                self._settle(job, {"status": "failed", "detail": detail}, "teardown")

    def _drop(self, jobs: list[dict]) -> None:
        """丢弃已结算记录并逐条 announce removed（可见集唯一的变更）。"""
        for job in jobs:
            self._store.pop(job["id"], None)
            self._emit({"type": "removed", "job": self._view(job)}, job["owner_agent"])

    def _warn(self, message: str) -> None:
        logger = getattr(self.ctx, "logger", None)
        if logger is not None and hasattr(logger, "warn"):
            logger.warn(message)
        else:
            print(f"[jobs] {message}")
