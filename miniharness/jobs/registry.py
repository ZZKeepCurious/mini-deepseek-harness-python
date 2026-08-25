"""进程内后台作业注册表（对齐 packages/jobs/jobs-local/src/index.ts）。

语义（与上游逐条一致）：
  * 一条注册 = `ctx.jobs` 服务（每 context 单实现，重复提供 fail loud）
  * registrations 存续于 producer/controller fiber 之外；owner 或服务销毁
    取消并等待在飞作业，throw 的 teardown cancel 只 force-fail 记录
  * owned-job 访问按 owner 会话 id 栅栏（`!== undefined` 语义：unowned 开放、
    无 agent 调用方永不匹配 owned）
  * 结算 first-wins：一条终态记录 + 一次监听器通知（对迟到的 producer 结算免疫）
  * start 在无已挂 controller 服务该 owner 时拒绝（producer 无法启动 owner
    收不回/停不下的活）；controller/监听器/观察者按**注册 scope 分层**——
    全局层服务所有 owner，scoped 层只服务 owner 链上的成员；unowned 只有
    全局层能接（上游 ScopedLayers owner-relative，2026-08-22 P1-4a 对齐，
    拒绝文案逐字 index.ts:133）
  * maxConcurrentJobsPerOwner 默认 10，按精确 owner（或共享 unowned 桶）
    计 running+stopping；终态结算释放容量

mini 教学适配（有意保留，须在文档标注）：
  * 上游经 cordis `inject` 把服务方法绑定到注册方 ctx（jobs.spec.ts:87-100）；
    mini 无 inject 重绑，以显式 `ctx=` 参数承载"注册 scope"（缺省=注册表自身
    ctx → 全局层），语义等价、载体不同
  * 无 agent registry：owner 不校验"当前注册实例"，只要求 owner.id 与 owner.ctx
  * teardown 排干等每任务 settled 事件（对齐上游 await Promise.all(settled)：
    先 cancel-all 再逐任务等待，无时间上限——producer 永不结算会挂起 teardown，
    上游同款限制；producer 契约要求响应 cancel 并最终结算）
  * done 用 JobDoneBox/Future 承载 Promise 语义
"""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

from ..core.session import now_ms
from ..core.scope import Context
from ..core.dsh_scope import AnonymousEntries, ScopedLayers, scope_of
from .types import (
    DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER,
    TERMINAL_STATUSES,
    job_id,
)


def _signal_aborted(signal: Any) -> bool:
    """两种 signal 形状的统一判读：_AbortProxy.aborted 或 threading.Event.is_set。"""
    if signal is None:
        return False
    if getattr(signal, "aborted", None) is not None:
        return bool(signal.aborted)
    is_set = getattr(signal, "is_set", None)
    if callable(is_set):
        return bool(is_set())
    return False


class _JobLayer:
    """一个 scope 的贡献：controller + 完成监听器 + 变更观察者。

    三张表都是匿名条目（贡献由自己的 disposer 标识，同名可独立卸载；
    对齐上游 JobLayer，index.ts:76-84）。isEmpty 三表全空才算空。
    """

    __slots__ = ("controllers", "listeners", "changed")

    def __init__(self) -> None:
        self.controllers = AnonymousEntries()
        self.listeners = AnonymousEntries()
        self.changed = AnonymousEntries()

    def isEmpty(self) -> bool:
        return (
            self.controllers.isEmpty()
            and self.listeners.isEmpty()
            and self.changed.isEmpty()
        )


def _scope_of_owner(owner: Any) -> Any:
    """owner 的 scope 键（无 owner 或无 ctx 的 owner → None，即全局视角）。"""
    if owner is None:
        return None
    return scope_of(getattr(owner, "ctx", None))


class LocalJobRegistry:
    """内存 `jobs` 服务：每条记录只在内部可变，对外只给新鲜快照。"""

    def __init__(self, ctx: Context, config: dict | None = None):
        self.ctx = ctx
        cfg = config or {}
        max_conc = cfg.get("maxConcurrentJobsPerOwner", DEFAULT_MAX_CONCURRENT_JOBS_PER_OWNER)
        if not isinstance(max_conc, int) or isinstance(max_conc, bool) or max_conc < 1:
            raise ValueError(
                f"invalid maxConcurrentJobsPerOwner: expected a positive integer, got {max_conc!r}"
            )
        self.max_concurrent_jobs_per_owner = max_conc
        self._store: dict[str, dict] = {}
        self._counters: dict[str, int] = {}
        # controller/监听器/观察者按注册 scope 分层（上游 ScopedLayers<JobLayer>，
        # index.ts:104-116）：全局层服务所有 owner，scoped 层只服务 owner 链。
        self._layers = ScopedLayers(lambda _scope: _JobLayer(), lambda: None)
        self._listeners_closed = False
        self._owner_cleanups: dict[int, Callable] = {}
        # 注册为 ctx.jobs 服务（同 context 二次提供 fail loud）；teardown 清场
        ctx.provide("jobs", self)
        ctx.effect(lambda: self._dispose_all, "jobs registry teardown")

    # ---------- 生命周期与接入 ----------

    def attach_controller(self, name: str, ctx: Any = None) -> Callable[[], None]:
        """挂一个可读/停作业的 controller。

        `ctx` 是注册方上下文（上游经 inject 绑定到调用方；mini 显式传参）：
        其 scope 层持有此贡献，scope 销毁时随 fiber 自动卸载。缺省=注册表
        自身 ctx（通常为组合根 → 全局层，服务所有 owner）。同名可独立卸。
        """
        token = object()
        return self._layers.effect(
            ctx if ctx is not None else self.ctx,
            lambda layer: layer.controllers.append(token),
            label="jobs.attachController()",
        )

    def on_job_done(self, listener: Callable, ctx: Any = None) -> Callable[[], None]:
        """注册终态监听器（接收 snapshot 与精确 owner，或 unowned 的 None）。

        只接收注册 scope 覆盖的 owner 的结算：全局层先投递，再沿 owner 链
        逐层投递（index.ts:338-342）；链外组合的监听器不投递。
        """
        return self._layers.effect(
            ctx if ctx is not None else self.ctx,
            lambda layer: layer.listeners.append(listener),
            label="jobs.onJobDone()",
        )

    def on_jobs_changed(self, listener: Callable, ctx: Any = None) -> Callable[[], None]:
        """注册可见集变更观察者（接收 owner 或 unowned 的 None）。

        投递范围与 on_job_done 同规则解析（index.ts:388-392）；不携带
        notice 语义、不置 reported。
        """
        return self._layers.effect(
            ctx if ctx is not None else self.ctx,
            lambda layer: layer.changed.append(listener),
            label="jobs.onJobsChanged()",
        )

    # ---------- 服务面 ----------

    def start(self, spec: dict) -> str:
        """启动前完整 preflight，之后原子注册，注册后不可失败。返回 `<kind>-N`。"""
        owner = spec.get("owner")
        if not self._serves_owner(owner):
            # 逐字对齐上游（jobs-local index.ts:133；括注指向上游补救插件）
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
        if limit is not None and (not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0):
            raise ValueError(f"invalid outputLimitBytes: expected a positive safe integer, got {limit!r}")
        if owner is not None:
            self._ensure_owner_cleanup(owner)
        if self._active_task_count(owner) >= self.max_concurrent_jobs_per_owner:
            raise RuntimeError(
                f"background job limit reached for this owner (limit: {self.max_concurrent_jobs_per_owner}); "
                "use job_kill to stop an unneeded job, wait for it to finish, then retry"
            )
        hooks = spec["run"]()
        done = hooks["done"]
        cancel = hooks["cancel"]
        if not (hasattr(done, "add_done_callback") and hasattr(done, "result")):
            raise TypeError("hooks['done'] must expose add_done_callback()/result() (JobDoneBox or Future)")
        count = self._counters.get(kind, 0) + 1
        self._counters[kind] = count
        task_id = job_id(kind, count)
        job: dict = {
            "id": task_id,
            "kind": kind,
            "label": label,
            "outputLimitBytes": limit,
            "owner": owner,
            "cancel": cancel,
            "readOutput": hooks.get("read_output"),
            "status": "running",
            "detail": None,
            "output": None,
            "startedAt": now_ms(),
            "finishedAt": None,
            "reported": False,
            "waiters": 0,
            "settled": threading.Event(),
            "done": done,
        }
        self._store[task_id] = job
        # 等价 hooks.done.then(settle, reject → failed)（producer 契约违例也要收场）
        done.add_done_callback(lambda box, j=job: self._on_producer_done(j, box))
        self._notify_changed(owner)
        return task_id

    def list(self, caller: Any = None) -> list[dict]:
        """按注册序列出 caller 可见的作业（owned 只给同会话，unowned 全开放）。"""
        session = getattr(caller, "id", None)
        return [
            self._snapshot(j) for j in self._store.values()
            if j["owner"] is None or j["owner"].id == session
        ]

    def get(self, task_id: str, caller: Any = None) -> dict:
        """非消耗性快照；未知/外会话作业 fail loud。"""
        job = self._expect(task_id)
        self._assert_access(job, caller)
        return self._snapshot(job)

    def read(self, task_id: str, caller: Any = None) -> dict:
        """流式读增量或终态幂等输出；终态读取置 reported。"""
        job = self._expect(task_id)
        self._assert_access(job, caller)
        if job["readOutput"] is not None:
            text = job["readOutput"]()
        else:
            text = job["output"] if job["status"] in TERMINAL_STATUSES else ""
        if job["status"] in TERMINAL_STATUSES:
            job["reported"] = True
        return {"text": text or "", "snapshot": self._snapshot(job)}

    def kill(self, task_id: str, caller: Any = None, reason: str | None = None) -> str:
        """请求取消并置 stopping + reported；producer 抛错不改状态即传播。"""
        job = self._expect(task_id)
        self._assert_access(job, caller)
        if job["status"] in TERMINAL_STATUSES:
            job["reported"] = True
            return "already-finished"
        # 先 cancel：throw 让生命周期与 notice 状态都保持原样
        job["cancel"](reason)
        job["status"] = "stopping"
        job["reported"] = True
        self._notify_changed(job["owner"])
        return "requested"

    def wait(self, task_id: str, timeout_ms: float, caller: Any = None, signal: Any = None) -> dict:
        """等结算或超时，不取消作业；超时返回运行态快照，abort 只在在飞时抛。"""
        job = self._expect(task_id)
        self._assert_access(job, caller)
        if not isinstance(timeout_ms, (int, float)) or isinstance(timeout_ms, bool) or timeout_ms <= 0:
            raise ValueError(f"invalid wait timeout: expected a positive number of milliseconds, got {timeout_ms!r}")
        if job["status"] not in TERMINAL_STATUSES:
            if _signal_aborted(signal):
                raise RuntimeError("wait aborted")
            # waiter 计数让 settle 提前置 reported，抑制 notice（上游 waiters 语义）
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
        if job["status"] in TERMINAL_STATUSES:
            job["reported"] = True
        return self._snapshot(job)

    # ---------- 内部 ----------

    def _on_producer_done(self, job: dict, done: Any) -> None:
        outcome = self._extract_outcome(done)
        self._settle(job, outcome)

    @staticmethod
    def _extract_outcome(done: Any) -> dict:
        try:
            value = done.result()
        except BaseException as error:
            return {"status": "failed", "detail": str(error)}
        if not isinstance(value, dict) or value.get("status") not in TERMINAL_STATUSES:
            return {"status": "failed", "detail": f"producer done settled with invalid outcome: {value!r}"}
        return {
            "status": value["status"],
            **({"detail": value["detail"]} if value.get("detail") is not None else {}),
            **({"output": value["output"]} if value.get("output") is not None else {}),
        }

    def _settle(self, job: dict, outcome: dict) -> None:
        if job["status"] in TERMINAL_STATUSES:
            return  # first-wins：对迟到的 producer 结算免疫
        job["status"] = outcome["status"]
        job["detail"] = outcome.get("detail")
        job["output"] = outcome.get("output")
        job["finishedAt"] = now_ms()
        if job["waiters"] > 0:
            job["reported"] = True
        # 先放行 teardown 排干（上游 markSettled 在监听器投递前解析 promise）
        job["settled"].set()
        if self._listeners_closed:
            return
        self._notify_changed(job["owner"])
        snapshot = self._snapshot(job)
        for listener in self._listeners_for(job["owner"]):
            try:
                listener(snapshot, job["owner"])
            except Exception as error:
                self._warn(f"onJobDone listener threw for {job['id']}: {error}")

    def _warn(self, message: str) -> None:
        logger = getattr(self.ctx, "logger", None)
        if logger is not None and hasattr(logger, "warn"):
            logger.warn(message)
        else:
            print(f"[jobs] {message}")

    def _serves_owner(self, owner: Any) -> bool:
        """全局层有 controller → 服务所有 owner；否则沿 owner 链找任一
        有 controller 的 scoped 层（index.ts:315-319）。unowned 无链可走，
        只有全局层能接。"""
        if not self._layers.global_layer.controllers.isEmpty():
            return True
        return any(
            not layer.controllers.isEmpty()
            for layer in self._layers.chain_layers(_scope_of_owner(owner))
        )

    def _listeners_for(self, owner: Any) -> list[Callable]:
        """结算监听器投递序：全局层先，再 owner 链各层（index.ts:338-342）。"""
        listeners = list(self._layers.global_layer.listeners.values())
        for layer in self._layers.chain_layers(_scope_of_owner(owner)):
            listeners.extend(layer.listeners.values())
        return listeners

    def _changed_for(self, owner: Any) -> list[Callable]:
        """变更观察者投递序：与 _listeners_for 同规则（index.ts:388-392）。"""
        listeners = list(self._layers.global_layer.changed.values())
        for layer in self._layers.chain_layers(_scope_of_owner(owner)):
            listeners.extend(layer.changed.values())
        return listeners

    def _active_task_count(self, owner: Any) -> int:
        return sum(
            1 for j in self._store.values()
            if j["owner"] is owner and j["status"] in ("running", "stopping")
        )

    def _expect(self, task_id: str) -> dict:
        job = self._store.get(task_id)
        if job is None:
            raise RuntimeError(f"unknown job {task_id}")
        return job

    def _assert_access(self, job: dict, caller: Any) -> None:
        owner = job["owner"]
        if owner is not None and getattr(caller, "id", None) != owner.id:
            raise RuntimeError(f"job {job['id']} belongs to another session")

    @staticmethod
    def _snapshot(job: dict) -> dict:
        snap: dict = {
            "id": job["id"],
            "kind": job["kind"],
            "label": job["label"],
            "status": job["status"],
            "startedAt": job["startedAt"],
            "reported": job["reported"],
        }
        if job["outputLimitBytes"] is not None:
            snap["outputLimitBytes"] = job["outputLimitBytes"]
        if job["owner"] is not None:
            snap["ownerSession"] = job["owner"].id
        if job["detail"] is not None:
            snap["detail"] = job["detail"]
        if job["finishedAt"] is not None:
            snap["finishedAt"] = job["finishedAt"]
        return snap

    def _notify_changed(self, owner: Any) -> None:
        for listener in self._changed_for(owner):
            try:
                listener(owner)
            except Exception as error:
                self._warn(f"onJobsChanged listener threw: {error}")

    def _ensure_owner_cleanup(self, owner: Any) -> None:
        if not hasattr(owner, "id") or not getattr(owner, "id", None):
            raise RuntimeError("background job owner must expose a non-empty id")
        if not hasattr(owner, "ctx"):
            raise RuntimeError("background job owner must expose its composition context (owner.ctx)")
        key = id(owner)
        if key in self._owner_cleanups:
            return

        def detach() -> None:
            self._owner_cleanups.pop(key, None)
            self._dispose_owned(owner)

        owner.ctx.effect(lambda: detach, f"jobs owner detach {key}")
        self._owner_cleanups[key] = detach

    def _dispose_owned(self, owner: Any) -> None:
        owned = [j for j in self._store.values() if j["owner"] is owner]
        self._cancel_for_teardown(owned, "owner disposed")
        self._drain(owned)
        for job in owned:
            self._store.pop(job["id"], None)
        if owned:
            self._notify_changed(owner)

    def _dispose_all(self) -> None:
        # 监听器在注册它的 fiber 退出时卸载；服务自身不替它们兜底（上游注释语义）
        self._listeners_closed = True
        all_jobs = list(self._store.values())
        self._cancel_for_teardown(all_jobs, "jobs service disposed")
        self._drain(all_jobs)
        emptied = {id(j["owner"]) for j in all_jobs}
        self._store.clear()
        for key in list(self._owner_cleanups):
            self._owner_cleanups.pop(key, None)
        for job in all_jobs:
            if id(job["owner"]) in emptied:
                self._notify_changed(job["owner"])
                emptied.remove(id(job["owner"]))

    def _drain(self, jobs: list[dict]) -> None:
        """等 producer 到达静止（对齐上游 await Promise.all(settled)：调用方
        已先 cancel-all，此处逐任务等 settled 事件、无时间上限——producer
        永不结算会挂起 teardown，上游同款限制；producer 契约要求响应 cancel
        并最终结算）。"""
        for job in jobs:
            job["settled"].wait()

    def _cancel_for_teardown(self, jobs: list[dict], reason: str) -> None:
        for job in jobs:
            if job["status"] in TERMINAL_STATUSES:
                continue
            # teardown 无读者，先吃掉 notice；force-fail 也不 announce unreported 完成
            job["reported"] = True
            try:
                job["cancel"](reason)
                job["status"] = "stopping"
                self._notify_changed(job["owner"])
            except Exception as error:
                detail = f"cancel threw during teardown; work may be orphaned: {error}"
                self._warn(f"cancel of {job['id']} threw during teardown; job record forced failed: {error}")
                self._settle(job, {"status": "failed", "detail": detail})
