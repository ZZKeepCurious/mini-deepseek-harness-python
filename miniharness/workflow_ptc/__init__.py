"""workflow-ptc：Python 工作流执行引擎（对齐上游 workflow-ptc 的 mini 载体）。

上游 `packages/workflow/workflow-ptc`：引擎把工作流脚本跑在 Node VM（guest.ts +
runtime.ts + realm.ts），脚本语言是 **JavaScript**，硬性要求 Node TypeScript PTC
运行时（index.ts:117）。mini 的 PTC 运行时是 `PythonPtcRuntime`（language='python'）。

功能对标迁移（黄金法则 #5）：编排语义（meta 校验 / 六全局 / 并发槽 / caps /
schema 子集 / 结果物化 / 结算与取消）全部可移植到 **Python 脚本 + PythonPtcRuntime
绑定面**；脚本语言作为载体差异登记（同 run_code 的 typescript→python flavor）。

执行模型（对齐上游 guest.ts + host.ts）：
  * 每个 run 在全新 CPython 子进程（PythonPtcRuntime）里跑一段**引导程序**（guest）：
    guest 经 `workflowHost` 绑定与宿主握手（begin 拿 init），暴露六个脚本全局
    `agent`/`parallel`/`pipeline`/`phase`/`log`/`args`，然后执行模型提供的脚本体。
  * host 侧 `PtcWorkflowRun`：持有 continuation manager（子代理）+ ptcRuntime +
    sandboxPolicy；绑定 `begin`/`startChild`/`childResult`/`disposeChild`/`progress`
    经 PTC 行 JSON 协议往返；observer 把 phase/log/agent-start/agent-end 接到引擎事件。
  * 子代理经 mini `SubagentContinuationManager` 启动（start_continuable 占位 +
    send_message_async 内联泵首回合 + epoch_result 收 output/stop）。

载体差异登记：
  * 脚本语言 JS→Python（模型契约随之改写，同 run_code flavor）。
  * 子代理 `structured` 输出（上游 spawn provider 的 outputSchema 能力）mini 无
    载体——schema 子代理按上游「completed + schema + structured===undefined →
    failed → null」语义返回 null（上游 runtime.ts:206-215 同款）。
  * 并发槽 / caps / schema 校验子集为纯逻辑，全量移植。
  * 无 Node VM realm——materializeFromRealm 由 Python 侧无损 JSON 校验等价承担。
"""
from __future__ import annotations

import asyncio
import json
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable

from ..core.agent_loop.resident_loop import run_on_resident
from ..core.scope import Context
from ..seams.subagent import SubagentContinuationManager, SubagentError
from ..workflow import (
    WorkflowEngine,
    WorkflowError,
    is_fatal_workflow_error,
    new_workflow_run_id,
    validate_meta,
)
from .meta import (
    META_STATEMENT,
    assert_body_parses,
    resolve_max_total_agents,
    resolve_subagent_provider,
)

__all__ = [
    "DEFAULT_CONFIG",
    "PtcWorkflowEngine",
    "PtcWorkflowRun",
    "install_workflow_engine",
]

#: 引擎配置缺省（index.ts:31-43 + schemastery 默认）。
DEFAULT_CONFIG = {
    "provider": "spawn",
    "maxConcurrentAgents": 0,
    "maxTotalAgents": 1000,
    "maxItemsPerCall": 4096,
    "syncTimeoutMs": 5000,
}

#: 自动并发缺省：`min(16, max(1, 可用并行 - 2))`（index.ts:141-143）。
def _auto_concurrency() -> int:
    try:
        import os
        return min(16, max(1, (os.cpu_count() or 1) - 2))
    except Exception:  # noqa: BLE001
        return 1


#: 绑定函数的 host 侧实现签名（经 PTC 绑定收 args 单参）。
WorkflowBindingFn = Callable[[Any], Any]


class PtcWorkflowRun:
    """一次工作流运行（host 侧句柄）：PTC 执行 + 子代理所有权 + 结算/取消/拆解。

    `result` 永不 reject；`cancel(reason)` 幂等（首因优先）；`dispose()` 幂等
    （取消 + 等待脚本与子代理静默）。对齐上游 host.ts PtcWorkflowRun。
    """

    def __init__(self, *, engine: "PtcWorkflowEngine", run_id: str, meta: dict,
                 init: dict, provider: str, parent: Any, policy: dict,
                 observer: Callable[[str, dict], None], signal: Any = None):
        self.id = run_id
        self.meta = meta
        self.parent = parent
        self.provider = provider
        self.policy = policy
        self.observer = observer
        self._engine = engine
        self._init = init
        self._cancel_reason: str | None = None
        self._terminal = False
        self._children: dict[int, dict] = {}
        self._started = 0
        self._live_agents: dict[int, dict] = {}
        self._result: dict | None = None
        self._lock = threading.Lock()
        # 外部取消信号（同步门面）——经轮询桥接
        self.signal = signal
        self._abort_event = getattr(signal, "event", None) if signal is not None else None
        self._aborted = bool(signal is not None and getattr(signal, "aborted", False))
        self._run_coro = None

    # ---------- 外部控制 ----------

    def cancel(self, reason: str = "workflow cancelled") -> None:
        """首因优先取消（host.ts:148-153）：置 reason + 断开子代理。"""
        with self._lock:
            if self._terminal or self._cancel_reason is not None:
                return
            self._cancel_reason = reason
            self._aborted = True
        for record in list(self._children.values()):
            try:
                record.get("dispose", lambda: None)()
            except Exception:  # noqa: BLE001 - 子代理拆解失败不阻塞取消
                pass

    def dispose(self) -> Any:
        """幂等拆解：取消 + 等结果（host.ts:159-163）。"""
        self.cancel("workflow disposed")
        return self.result()

    def result(self) -> dict:
        """前台投影：阻塞至结算；永不 reject（host.ts:273-305 结局映射）。"""
        if self._result is not None:
            return self._result
        if self._run_coro is None:
            self._run_coro = self._drive()
        self._result = run_on_resident(self._run_coro)
        return self._result

    # ---------- 内部驱动 ----------

    async def _drive(self) -> dict:
        """跑 guest 程序并映射结局（host.ts:273-305）。"""
        runtime = self._engine.ptc_runtime
        bindings = self._bindings()
        guest = self._engine.guest_program()
        program = guest + "\n\n" + self._init["body"]
        from ..ptc_runtime.types import PtcRunRequest
        request = PtcRunRequest(
            program=program,
            bindings=[bindings],
            cwd=(self.parent.session.meta or {}).get("cwd"),
            timeoutMs=None,
            signal=self._abort_event,
        )
        try:
            outcome = await runtime.run(runtime.resolve(request))
        except Exception as error:  # noqa: BLE001 - 基础设施故障折 error
            return self._failure_outcome(f"workflow execution failed: {error}")
        if outcome.error is not None:
            if self._cancel_reason is not None:
                return self._cancelled_outcome()
            return self._failure_outcome(
                f"workflow execution failed ({outcome.error.kind}): "
                f"{outcome.error.message}")
        if self._cancel_reason is not None:
            return self._cancelled_outcome()
        value = outcome.value if outcome.has_value else None
        # 物化校验：结果必须无损 JSON（materializeResult 等价）
        if not _is_lossless_json(value):
            return self._failure_outcome(
                "the workflow's return value is not plain JSON data — "
                "Return only JSON-serializable objects/arrays/scalars.")
        return {
            "value": value,
            "stopReason": "completed",
            "agentsStarted": self._started,
        }

    def _failure_outcome(self, message: str) -> dict:
        self._terminal = True
        return {"value": None, "stopReason": "error", "error": message,
                "agentsStarted": self._started}

    def _cancelled_outcome(self) -> dict:
        self._terminal = True
        return {"value": None, "stopReason": "cancelled",
                "error": f"workflow run cancelled: {self._cancel_reason}",
                "agentsStarted": self._started}

    # ---------- 绑定命名空间 ----------

    def _bindings(self):
        from ..ptc_runtime.types import PtcBindingNamespace
        return PtcBindingNamespace("workflowHost", {
            "begin": self._b_begin,
            "startChild": self._b_start_child,
            "childResult": self._b_child_result,
            "disposeChild": self._b_dispose_child,
            "progress": self._b_progress,
        })

    def _require_active(self) -> None:
        if self._terminal:
            raise RuntimeError("workflow run is no longer active")
        if self._aborted:
            raise RuntimeError(self._cancel_reason or "workflow cancelled")

    def _b_begin(self, args):
        self._require_active()
        return self._init

    def _b_start_child(self, args):
        self._require_active()
        request = args if isinstance(args, dict) else {}
        prompt = request.get("prompt")
        if not isinstance(prompt, str) or prompt == "":
            raise ValueError("workflow startChild requires a non-empty prompt string")
        call_id = self._started + 1
        self._started = call_id
        label = request.get("label") or _default_label(prompt)
        phase = request.get("phase")
        agent_options = {}
        if request.get("provider") is not None:
            agent_options["provider"] = request["provider"]
        if request.get("model") is not None:
            agent_options["model"] = request["model"]
        schema = request.get("schema")
        info = {"seq": call_id, "label": label, "childId": f"child-{call_id}"}
        if phase is not None:
            info["phase"] = phase
        self._live_agents[call_id] = dict(info)
        self.observer("agent-start", {"info": self._run_info(), "agent": info})
        self._children[call_id] = {
            "prompt": prompt,
            "agent_options": agent_options,
            "schema": schema,
            "label": label,
            "phase": phase,
        }
        return {"callId": call_id, "childId": info["childId"]}

    def _b_child_result(self, args):
        self._require_active()
        request = args if isinstance(args, dict) else {}
        call_id = request.get("callId")
        record = self._children.get(call_id)
        if record is None:
            raise RuntimeError("workflow child call is not active")
        return run_on_resident(self._settle_child(call_id, record))

    async def _settle_child(self, call_id: int, record: dict) -> dict:
        """启动子代理并等首回合结算（continuation manager 面）。

        async：PTC `_BindingHost.invoke` 对 coroutine 经 `asyncio.run` 驱动；
        父无 driver 时 `send_message_async` 内联 `await child._pump_async()` 跑完
        首回合再读 epoch（确定性结算）。
        """
        manager: SubagentContinuationManager = self._engine.subagents
        parent = self.parent
        try:
            child_id = manager.start_continuable(
                label=record["label"], parent=parent,
                agent_options=record["agent_options"] or None)
        except SubagentError as error:
            self._end_agent(call_id, "failed")
            raise RuntimeError(f"agent() could not start a child: {error}") from error
        base = len(manager.persistence.inspect(child_id)["events"])
        await manager.send_message_async(
            child_id, record["prompt"], source="parent", parent=parent)
        from ..seams.subagent.tool import _epoch_result
        stop, output, _diagnostic = _epoch_result(manager, child_id, base)
        result = {"output": _output_text(output), "stopReason": stop}
        # schema 子代理：上游 structured 由 spawn provider 产出；mini 无载体 →
        # completed 但无 structured 视为失败 → null（上游 runtime.ts:206-215）。
        if record["schema"] is not None:
            self._end_agent(call_id, "failed" if stop != "completed" else "completed")
            return {**result, "structured": None}
        self._end_agent(call_id, "failed" if stop != "completed" else "completed")
        return result

    def _end_agent(self, call_id: int, outcome: str) -> None:
        info = self._live_agents.pop(call_id, None)
        if info is None:
            return
        self.observer("agent-end", {"info": self._run_info(),
                                    "agent": {**info, "outcome": outcome}})

    def _b_dispose_child(self, args):
        request = args if isinstance(args, dict) else {}
        call_id = request.get("callId")
        if call_id in self._children:
            self._children.pop(call_id, None)
        return None

    def _b_progress(self, args):
        # guest 批量进度（phase/log/agent-start/agent-end）——host 侧逐条派发。
        batch = args if isinstance(args, list) else [args]
        for event in batch:
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            payload = event.get("data") or {}
            if kind == "phase":
                self.observer("phase", {"info": self._run_info(), "title": payload})
            elif kind == "log":
                self.observer("log", {"info": self._run_info(), "message": payload})
            elif kind == "agent-start":
                self.observer("agent-start", {"info": self._run_info(),
                                              "agent": payload})
            elif kind == "agent-end":
                self.observer("agent-end", {"info": self._run_info(),
                                            "agent": payload})
        return None

    def _run_info(self) -> dict:
        return {"id": self.id, "meta": self.meta}


def _output_text(output: Any) -> str:
    """子代理最终输出 → 文本（上游 outputText：拼接 text 块）。"""
    if output is None:
        return ""
    if isinstance(output, str):
        return output
    if isinstance(output, (list, tuple)):
        return "".join(b.get("text", "") for b in output
                       if isinstance(b, dict) and b.get("type") == "text")
    return str(output)


def _default_label(prompt: str) -> str:
    """默认子代理标签：首行截断 48 字符（runtime.ts:46-50）。"""
    line = prompt.splitlines()[0] if prompt else ""
    return line[:47] + "\u2026" if len(line) > 47 else line


def _is_lossless_json(value: Any) -> bool:
    try:
        json.dumps(value, allow_nan=False)
    except (TypeError, ValueError):
        return False
    return True


class PtcWorkflowEngine(WorkflowEngine):
    """`ctx.workflowEngine` 实现：Python 工作流引擎（workflow-ptc 对应物）。

    inject：subagents（continuation manager）+ ptcRuntime + sandboxPolicy。
    """

    def __init__(self, ctx: Context, config: dict | None = None):
        config = dict(config or {})
        unknown = set(config) - set(DEFAULT_CONFIG)
        if unknown:
            raise ValueError(f"workflow-ptc: unknown config keys: {sorted(unknown)!r}")
        merged = {**DEFAULT_CONFIG, **config}
        self.config = merged
        self.provider = merged["provider"]
        self.max_concurrent_agents = merged["maxConcurrentAgents"]
        self.max_total_agents = merged["maxTotalAgents"]
        self.max_items_per_call = merged["maxItemsPerCall"]
        self.sync_timeout_ms = merged["syncTimeoutMs"]
        self.ptc_runtime = ctx.get("ptcRuntime")
        self.subagents = ctx.get("subagents") or ctx.get("subagentManager")
        self.sandbox_policy = ctx.get("sandboxPolicy")
        super().__init__(ctx)
        if self.ptc_runtime is None:
            raise ValueError("workflow-ptc requires the Python PTC runtime (install_ptc_runtime)")
        if self.subagents is None:
            raise ValueError(
                "workflow-ptc requires a subagent continuation manager "
                "(install a SubagentContinuationManager)")
        if self.max_concurrent_agents == 0:
            self.max_concurrent_agents = _auto_concurrency()

    def guest_program(self) -> str:
        """guest 引导程序（Python）：绑定 workflowHost + 暴露六全局 + 跑脚本体。"""
        return _GUEST_PROGRAM

    def start(self, request: dict) -> PtcWorkflowRun:
        """校验 + 解析 → 构造 run 并发射 workflow/start（index.ts:133-187）。"""
        meta = validate_meta(request.get("meta"))
        script = request.get("script")
        if not isinstance(script, str):
            raise WorkflowError("workflow script must be a string", "INVALID_ARGUMENT")
        if META_STATEMENT.search(script):
            raise WorkflowError(
                "workflow meta rides the `meta` request field, not the script: "
                "remove the `export const meta = {...}` statement from the body",
                "SCRIPT_PARSE")
        assert_body_parses(script, meta["name"])
        provider = resolve_subagent_provider(
            self.ctx, self.provider, request.get("subagentProvider"))
        max_total = resolve_max_total_agents(
            request.get("maxTotalAgents"), self.max_total_agents)
        parent = request.get("parent")
        if parent is None:
            raise WorkflowError(
                "workflow start requires a parent agent", "INVALID_ARGUMENT")
        args = request.get("args")
        init = {
            "meta": meta,
            "body": script,
            **({} if args is None else {"args": args}),
            "limits": {
                "maxConcurrentAgents": self.max_concurrent_agents,
                "maxTotalAgents": max_total,
                "maxItemsPerCall": self.max_items_per_call,
                "syncTimeoutMs": self.sync_timeout_ms,
            },
        }
        policy = None
        if self.sandbox_policy is not None:
            policy = self.sandbox_policy.resolve({"session": parent.session})
        run = PtcWorkflowRun(
            engine=self, run_id=new_workflow_run_id(), meta=meta, init=init,
            provider=provider, parent=parent, policy=policy,
            observer=self._observer, signal=request.get("signal"))
        self.emit_workflow_event("workflow/start", run._run_info())
        return run

    def _observer(self, kind: str, payload: dict) -> None:
        info = payload.get("info")
        if kind == "phase":
            self.emit_workflow_event("workflow/phase", info, payload.get("title"))
        elif kind == "log":
            self.emit_workflow_event("workflow/log", info, payload.get("message"))
        elif kind == "agent-start":
            self.emit_workflow_event("workflow/agent-start", info, payload.get("agent"))
        elif kind == "agent-end":
            self.emit_workflow_event("workflow/agent-end", info, payload.get("agent"))


def install_workflow_engine(ctx: Context, config: dict | None = None) -> PtcWorkflowEngine:
    """装配 `ctx.workflowEngine`（幂等，重复装返回既有实例）。

    要求 ctx.ptcRuntime + ctx.subagents（或 ctx.subagentManager）在场。
    """
    existing = ctx.get("workflowEngine")
    if existing is not None:
        return existing
    return PtcWorkflowEngine(ctx, config)


#: guest 引导程序（Python）：绑定 workflowHost 已由 PTC 注入全局；这里把绑定
#: 包成六个脚本全局（agent/parallel/pipeline/phase/log/args），然后执行脚本体。
#: 并发槽 FIFO、caps、schema 校验、结果物化都在 guest 侧实现（对齐 runtime.ts）。
#: 注意：PTC bootstrap 把本程序体包进 `async def __dsh_main__()`，因此顶层 await
#: 可用；`init["body"]`（用户脚本体）由 engine.start 拼接在末尾——它被
#: `async def __dsh_main__()` 包住后，`return <value>` 即整体结果。
_GUEST_PROGRAM = r'''
import asyncio, json, sys

# 脚本全局（对齐 runtime.ts 的 VM globals）
args = None

# 并发槽（runtime.ts:143-160 FIFO）
_slot_waiters = []
_active_slots = 0
_max_concurrent = 0
_max_total = 0
_max_items = 0
_started = 0
_current_phase = None

async def _acquire_slot():
    nonlocal _active_slots
    if _active_slots < _max_concurrent:
        _active_slots += 1
        return
    waiter = asyncio.Event()
    _slot_waiters.append(waiter)
    await waiter.wait()

def _release_slot():
    nonlocal _active_slots
    _active_slots -= 1
    if _slot_waiters:
        _slot_waiters.pop(0).set()

def _default_label(prompt):
    line = prompt.splitlines()[0] if prompt else ""
    return (line[:47] + "\u2026") if len(line) > 47 else line

def _check_items(n, hook):
    if n > _max_items:
        raise RuntimeError(
            f"{hook} received {n} items — over the per-call cap ({_max_items}); "
            "split the work or raise maxItemsPerCall in the engine config")

async def agent(prompt, opts=None):
    nonlocal _started
    if not isinstance(prompt, str) or prompt == "":
        raise RuntimeError("agent() requires a non-empty prompt string")
    opts = opts or {}
    if not isinstance(opts, dict):
        raise RuntimeError("agent() options must be an object")
    _SUPPORTED = {"label", "phase", "schema", "provider", "model"}
    for key in opts:
        if key not in _SUPPORTED:
            raise RuntimeError(
                f'agent() option "{key}" is not recognized '
                "(supported: label, phase, schema, provider, model)")
    if _started >= _max_total:
        raise RuntimeError(
            f"this run reached its total agent cap ({_max_total}) — a runaway-loop "
            "backstop; raise the applicable maxTotalAgents limit if the scale is intentional")
    seq = _started + 1
    _started = seq
    label = opts.get("label") or _default_label(prompt)
    phase = opts.get("phase") or _current_phase
    await _acquire_slot()
    try:
        child = await workflowHost.startChild({
            "prompt": prompt,
            "label": label,
            **({"phase": phase} if phase else {}),
            **({"schema": opts["schema"]} if "schema" in opts else {}),
            **({"provider": opts["provider"]} if "provider" in opts else {}),
            **({"model": opts["model"]} if "model" in opts else {}),
        })
        result = await workflowHost.childResult({"callId": child["callId"]})
        if result.get("stopReason") != "completed":
            return None
        if "schema" in opts:
            return result.get("structured")
        return result.get("output")
    finally:
        try:
            await workflowHost.disposeChild({"callId": child["callId"]})
        except Exception:
            pass
        _release_slot()

async def parallel(thunks):
    if not isinstance(thunks, list):
        raise RuntimeError("parallel() requires an array of zero-argument functions")
    _check_items(len(thunks), "parallel()")
    for i, item in enumerate(thunks):
        if not callable(item):
            raise RuntimeError(f"parallel() item {i} is not a function")
    async def _maybe_await(fn):
        value = fn()
        if asyncio.iscoroutine(value):
            return await value
        return value
    results = await asyncio.gather(*(_maybe_await(t) for t in thunks),
                                   return_exceptions=True)
    return [None if isinstance(r, BaseException) else r for r in results]

async def pipeline(items, *stages):
    if not isinstance(items, list):
        raise RuntimeError("pipeline() requires an items array")
    _check_items(len(items), "pipeline()")
    if len(stages) == 0:
        raise RuntimeError("pipeline() requires at least one stage function")
    for i, stage in enumerate(stages):
        if not callable(stage):
            raise RuntimeError(f"pipeline() stage {i} is not a function")
    out = []
    for index, item in enumerate(items):
        value = item
        dropped = False
        for stage in stages:
            try:
                stepped = stage(value, item, index)
                value = await stepped if asyncio.iscoroutine(stepped) else stepped
            except BaseException:
                dropped = True
                break
        out.append(None if dropped else value)
    return out

def phase(title):
    nonlocal _current_phase
    if not isinstance(title, str) or title == "":
        raise RuntimeError("phase() requires a non-empty title string")
    _current_phase = title

def log(message):
    if not isinstance(message, str):
        raise RuntimeError("log() requires a message string")

async def _install_globals():
    nonlocal args, _max_concurrent, _max_total, _max_items
    init = await workflowHost.begin()
    args = init.get("args")
    limits = init.get("limits") or {}
    _max_concurrent = limits.get("maxConcurrentAgents") or 1
    _max_total = limits.get("maxTotalAgents") or 1000
    _max_items = limits.get("maxItemsPerCall") or 4096

await _install_globals()
'''.strip()