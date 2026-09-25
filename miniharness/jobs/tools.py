"""模型侧 job_output / job_list / job_kill 三工具 + 完成 notice 投递。

对齐 packages/jobs/tool-jobs/src/{index,render}.ts。契约要点：
  * 三工具是 kind 无关的通用作业控制面；共享 PublicJobSnapshot（id/kind/label/
    status/detail/startedAt/finishedAt），刻意剔除 owner 与内部游标
  * job_output 默认非阻塞；wait:true 等至配置上限，超时返回运行态而非 TOOL_TIMEOUT；
    渲染消费 delta（stdout/无标签 + 一个 [stderr] 段）后附结算 result（若有）
  * 完成 notice 经 `events.subscribe({owners:'scope'})` 投递：settled 且未
    awaited/非 teardown/owner 可见时——busy owner 注入 inbox，idle owner 默认
    wakeup 开 turn；`maxConsecutiveWakes` 缺省不设=无界唤醒，设值则封顶；user 输入
    被认领后恢复预算；模型自己 job_kill 的结算不再重复告知（killedByModel）
  * producer 提供 outputLimitBytes 时，输出读与 notice 都按完整 UTF-8 结果字节封顶
  * canonical value + output.render 分离；finalizeContent 兜底二次截断

载体对齐：owner 无钩子近似——jobs 经安装 ctx 订阅 agent/inbox/claimed 恢复预算；
`killedByModel` 以注册表上的私有集合在 notice 监听与 job_kill 工具间共享。
"""
from __future__ import annotations

import asyncio
import threading
from types import MappingProxyType
from typing import Any, Callable

from ..core.tools import Tool
from .render import public_job, render_model_delta, status_line

__all__ = [
    "PUBLIC_TASK_SCHEMA",
    "fit_completion_notice",
    "fit_with_suffix",
    "install_completion_delivery",
    "job_kill_tool",
    "job_list_tool",
    "job_output_tool",
    "public_job",
    "register",
    "resolve_config",
    "status_line",
    "validate_job_id",
]

# 共享的公开作业快照 schema（对齐 tool-jobs PUBLIC_JOB_SCHEMA）
PUBLIC_TASK_SCHEMA: dict = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "id": {"type": "string", "required": True},
        "kind": {"type": "string", "required": True},
        "label": {"type": "string", "required": True},
        "status": {
            "type": "string",
            "required": True,
            "enum": ["running", "stopping", "completed", "killed", "failed"],
        },
        "detail": {"type": "string"},
        "startedAt": {"type": "integer", "required": True},
        "finishedAt": {"type": "integer"},
    },
}

DEFAULT_WAIT_TIMEOUT_MS = 30_000
DEFAULT_MAX_WAIT_TIMEOUT_MS = 600_000
DEFAULT_COMPLETION_DELIVERY = "wakeup"
#: maxConsecutiveWakes 缺省不设（无界唤醒）；仅当显式设值时封顶。
DEFAULT_MAX_CONSECUTIVE_WAKES = None


def resolve_config(config: dict | None) -> dict:
    """解析 tool-jobs 配置（缺省值对齐 Config 表）；越界 fail loud。"""
    config = config or {}
    cfg = {
        "waitTimeoutMs": config.get("waitTimeoutMs", DEFAULT_WAIT_TIMEOUT_MS),
        "maxWaitTimeoutMs": config.get("maxWaitTimeoutMs", DEFAULT_MAX_WAIT_TIMEOUT_MS),
        "completionDelivery": config.get("completionDelivery", DEFAULT_COMPLETION_DELIVERY),
        "maxConsecutiveWakes": config.get("maxConsecutiveWakes", DEFAULT_MAX_CONSECUTIVE_WAKES),
    }
    if cfg["waitTimeoutMs"] > cfg["maxWaitTimeoutMs"]:
        raise ValueError(
            f"tool-jobs: waitTimeoutMs ({cfg['waitTimeoutMs']}) exceeds "
            f"maxWaitTimeoutMs ({cfg['maxWaitTimeoutMs']})"
        )
    if cfg["completionDelivery"] not in ("quiet", "wakeup"):
        raise ValueError(f"tool-jobs: unknown completionDelivery {cfg['completionDelivery']!r}")
    budget = cfg["maxConsecutiveWakes"]
    if budget is not None and (not isinstance(budget, int) or isinstance(budget, bool)
                               or budget < 1):
        raise ValueError(
            f"tool-jobs: maxConsecutiveWakes ({budget}) must be a whole number of turns"
        )
    return cfg


def _caller(exec_: Any):
    """执行上下文 → SessionId（mini 的 Agent.id 即会话 id）。"""
    agent = getattr(exec_, "agent", None)
    return getattr(agent, "id", None) if agent is not None else None


def _killed_by_model(jobs: Any) -> set:
    """模型自请求 kill 的 live 作业集（notice 监听与 job_kill 工具共享）。"""
    killed = getattr(jobs, "_tool_jobs_killed_by_model", None)
    if killed is None:
        killed = set()
        setattr(jobs, "_tool_jobs_killed_by_model", killed)
    return killed


# ---------- UTF-8 字节封顶（对齐 TextRetainer head/tail + fitWithSuffix） ----------

def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8"))


def _fit_head(text: str, max_bytes: int) -> str:
    """保留前 max_bytes 字节，不在多字节字符中间劈裂。"""
    if _utf8_len(text) <= max_bytes:
        return text
    out: list[str] = []
    size = 0
    for ch in text:
        width = len(ch.encode("utf-8"))
        if size + width > max_bytes:
            break
        size += width
        out.append(ch)
    return "".join(out)


def _fit_tail(text: str, max_bytes: int) -> str:
    """保留后 max_bytes 字节（等价 TextRetainer tail）。"""
    if _utf8_len(text) <= max_bytes:
        return text
    out: list[str] = []
    size = 0
    for ch in reversed(text):
        width = len(ch.encode("utf-8"))
        if size + width > max_bytes:
            break
        size += width
        out.append(ch)
    return "".join(reversed(out))


def fit_with_suffix(content: str, suffix: str, max_bytes: int | None, omitted: str) -> str:
    """完整内容超限时保留尾部 + 截断标记 + 控制后缀（对齐 fitWithSuffix）。"""
    complete = content + suffix
    if max_bytes is None or _utf8_len(complete) <= max_bytes:
        return complete
    fixed = ("" if content.endswith(omitted.strip()) else omitted) + suffix
    fixed_bytes = _utf8_len(fixed)
    if fixed_bytes >= max_bytes:
        return _fit_tail(fixed, max_bytes)
    return _fit_tail(content, max_bytes - fixed_bytes) + fixed


# ---------- Model-facing final cap (对齐 upstream finalizeContent) ----------

def _raw_single_text(content: Any) -> str | None:
    """规整的单 text 块 → 文本；其余形状 None（对齐 rawSingleText）。"""
    if isinstance(content, (list, tuple)) and len(content) == 1:
        block = content[0]
        if isinstance(block, (dict, MappingProxyType)) and block.get("type") == "text":
            text = block.get("text")
            return text if isinstance(text, str) else None
    return None


def _bound_single_text(content: Any, max_bytes: int) -> list[dict] | None:
    """单 text 块整体封顶（对齐 boundSingleText）。"""
    text = _raw_single_text(content)
    if text is None:
        return None
    return [{"type": "text", "text": fit_with_suffix(text, "", max_bytes, "\n[result truncated]")}]


def visible_output_limit(jobs: Any, exec_: Any) -> int | None:
    """job_output / job_kill 的模型可见输出上限（对齐 visibleOutputLimit）。"""
    name = getattr(exec_, "name", None)
    if name not in ("job_output", "job_kill"):
        return None
    args = getattr(exec_, "arguments", None)
    job_id = args.get("job_id") if isinstance(args, (dict, MappingProxyType)) else None
    if not isinstance(job_id, str) or job_id == "":
        return None
    for snapshot in jobs.list(_caller(exec_)):
        if snapshot.get("id") == job_id:
            return snapshot.get("outputLimitBytes")
    return None


def finalize_job_task_content(jobs: Any) -> Callable[[Any, dict], list | None]:
    """job_output / job_kill 的 finalizeContent（对齐 tool-jobs finalizeTaskContent）。"""

    def _hook(exec_: Any, result: dict) -> list | None:
        max_bytes = visible_output_limit(jobs, exec_)
        if max_bytes is None:
            return None
        if (getattr(exec_, "name", None) == "job_output" and not result["is_error"]
                and isinstance(result["value"], (dict, MappingProxyType))):
            value = result["value"]
            body = value.get("text")
            body = body if isinstance(body, str) and body else "(no new output)"
            content = body[:-1] if body.endswith("\n") else body
            job = value.get("job")
            if isinstance(job, (dict, MappingProxyType)):
                suffix = "\n" + status_line(dict(job))
                if _raw_single_text(result["content"]) == content + suffix:
                    return [{"type": "text",
                             "text": fit_with_suffix(content, suffix, max_bytes,
                                                     "\n[output truncated]")}]
        return _bound_single_text(result["content"], max_bytes)

    return _hook


def fit_completion_notice(job: dict) -> str:
    """完整 notice；超限时保留稳定 id 前缀与收集指令，先花剩余字节在变化部分。"""
    prefix = f"background job {job['id']}"
    detail = f" ({job['kind']}: {job['label']}) finished {status_line(public_job(job))}"
    action = "\nDone; job_output."
    complete = f"{prefix}{detail}. Read its output with job_output."
    max_bytes = job.get("outputLimitBytes")
    if max_bytes is None or _utf8_len(complete) <= max_bytes:
        return complete
    omitted = "\n[notice truncated]"
    fixed = f"{prefix}{omitted}{action}"
    fixed_bytes = _utf8_len(fixed)
    if fixed_bytes <= max_bytes:
        if fixed_bytes == max_bytes:
            return fixed
        return f"{prefix}{_fit_head(detail, max_bytes - fixed_bytes)}{omitted}{action}"
    compact = f"{prefix}{action}"
    compact_bytes = _utf8_len(compact)
    if compact_bytes <= max_bytes:
        return compact
    action_bytes = _utf8_len(action)
    if action_bytes >= max_bytes:
        return _fit_tail(action, max_bytes)
    return f"{_fit_head(prefix, max_bytes - action_bytes)}{action}"


def validate_job_id(value: Any) -> str:
    """job_id 非空校验（ParameterSchemaSpec 表达不了非空约束）。"""
    if not isinstance(value, str) or value == "":
        raise ValueError(f"invalid job_id: expected a non-empty string, got {value!r}")
    return value


# ---------- 完成 notice 投递 ----------

def install_completion_delivery(jobs: Any, config: dict | None = None,
                                ctx: Any = None) -> None:
    """订阅 settled 事件：未 awaited / 非 teardown / 非模型自 kill 的完成投到精确 owner。

    wakeup：idle owner 开 turn（预算 maxConsecutiveWakes，缺省无界；user 输入经
    agent/inbox/claimed 事件恢复）；busy owner 一律注入。`ctx` 为注册方上下文
    （与 registry.events_for 的 scope 一致），缺省=注册表自身 ctx。
    """
    cfg = resolve_config(config)
    delivery = cfg["completionDelivery"]
    wake_budget = cfg["maxConsecutiveWakes"]
    scope_ctx = ctx if ctx is not None else getattr(jobs, "ctx", None)
    killed = _killed_by_model(jobs)
    spent_wakes: dict[int, int] = {}
    lock = threading.Lock()

    def handle_claimed(payload: dict) -> None:
        """agent/inbox/claimed：仅 user 源消息恢复预算。"""
        message = payload.get("message") or {}
        source = message.get("source") if isinstance(message, dict) else None
        if isinstance(source, dict) and source.get("kind") == "user":
            with lock:
                spent_wakes.pop(id(payload.get("agent")), None)

    def handle_disposed(payload: dict) -> None:
        """agent/disposed：清掉已销毁 loop 的预算项（防 id 键泄漏）。"""
        with lock:
            spent_wakes.pop(id(payload.get("agent")), None)

    def on_event(event: dict) -> None:
        if event["type"] == "removed":
            killed.discard(event["job"]["id"])
            return
        if event["type"] != "settled":
            return
        job = event["job"]
        delivered = False
        if job["id"] in killed:
            killed.discard(job["id"])
            delivered = True
        delivered = delivered or event.get("awaited") is True
        if delivered or event.get("cause") == "teardown" or job.get("owner") is None:
            return
        agents = scope_ctx.get("agents") if scope_ctx is not None else None
        owner = agents.get(job["owner"]) if agents is not None else None
        if owner is None:
            return
        notice = fit_completion_notice(job)
        should_wake = False
        if delivery == "wakeup" and getattr(owner, "status", None) == "idle":
            if wake_budget is None:
                should_wake = True
            else:
                with lock:
                    spent = spent_wakes.get(id(owner), 0)
                    if spent < wake_budget:
                        spent_wakes[id(owner)] = spent + 1
                        should_wake = True
        if should_wake:
            owner.followup(notice, source="tool-jobs")
        else:
            owner.inject(notice, source="tool-jobs")

    jobs.events_for(scope_ctx).subscribe({"owners": "scope"}, on_event)
    if scope_ctx is not None:
        scope_ctx.on("agent/inbox/claimed", handle_claimed)
        scope_ctx.on("agent/disposed", handle_disposed)


# ---------- 三工具 ----------

def job_output_tool(jobs, wait_default: int, wait_cap: int) -> Tool:
    async def execute(args: dict, exec_: Any) -> dict:
        task_id = validate_job_id(args.get("job_id"))
        caller = _caller(exec_)
        if args.get("wait") is True:
            timeout = min(args.get("timeout_ms") or wait_default, wait_cap)
            # jobs.wait 是阻塞轮询：to_thread 防止卡住事件循环（abort 信号透传）
            await asyncio.to_thread(jobs.wait, task_id, timeout, caller,
                                    getattr(exec_, "signal", None))
        read = jobs.read(task_id, caller)
        delta = render_model_delta(
            read["chunks"], read["lossy"],
            read["job"].get("output", {}).get("spillPaths", []) or [])
        result = read.get("result")
        text = delta if result is None else (
            f"{delta}{'' if not delta or delta.endswith(chr(10)) else chr(10)}{result}")
        return {"text": text, "job": public_job(read["job"])}

    def render(value: dict) -> list[dict]:
        body = value["text"] if value["text"] else "(no new output)"
        separator = "" if body.endswith("\n") else "\n"
        return [{"type": "text", "text": f"{body}{separator}{status_line(value['job'])}"}]

    return Tool(
        name="job_output",
        description=(
            "Read a background job. Stream jobs return only output since the previous read; "
            "final-output jobs return their result after settlement. Every response ends with "
            "`[status: ...]`. Reads are non-blocking unless `wait: true`, which waits up to the configured cap."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string", "required": True,
                    "description": "Job id returned by the tool that started the background work.",
                },
                "wait": {
                    "type": "boolean",
                    "description": "Block until the job reaches a terminal status or the timeout expires. "
                    "A timed-out wait returns [status: running] and leaves the job alive.",
                },
                "timeout_ms": {
                    "type": "number",
                    "description": "Max wait in milliseconds (only meaningful with wait: true). "
                    "Defaults to the configured wait timeout; capped by the configured maximum.",
                },
            },
        },
        output={
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "text": {"type": "string", "required": True},
                    "job": {**PUBLIC_TASK_SCHEMA, "required": True},
                },
            },
        },
        render=render,
        execute=execute,
        finalize_content=finalize_job_task_content(jobs),
    )


def job_list_tool(jobs) -> Tool:
    async def execute(_args: dict, exec_: Any) -> list[dict]:
        return [public_job(s) for s in jobs.list(_caller(exec_))]

    def render(jobs_list: list[dict]) -> list[dict]:
        if not jobs_list:
            return [{"type": "text", "text": "(no background jobs)"}]
        lines = "\n".join(
            f"{t['id']} [{t['kind']}] {t['status']} — {t['label']}" for t in jobs_list
        )
        return [{"type": "text", "text": lines}]

    return Tool(
        name="job_list",
        description="List your background jobs (running and finished) with their ids, kinds, and statuses.",
        parameters={"type": "object", "properties": {}},
        output={"schema": {"type": "array", "items": PUBLIC_TASK_SCHEMA}},
        render=render,
        execute=execute,
    )


def job_kill_tool(jobs) -> Tool:
    async def execute(args: dict, exec_: Any) -> dict:
        task_id = validate_job_id(args.get("job_id"))
        caller = _caller(exec_)
        result = jobs.kill(task_id, caller, args.get("reason"))
        # 模型自己的 kill 即它的交付：结算 notice 只会重复这个工具结果。
        if result == "requested":
            _killed_by_model(jobs).add(task_id)
        snapshot = public_job(jobs.get(task_id, caller))
        outcome = "cancellation-requested" if result == "requested" else result
        return {
            "outcome": outcome,
            "job": snapshot,
        }

    def render(value: dict) -> list[dict]:
        if value["outcome"] == "already-finished":
            return [{"type": "text", "text": f"job {value['job']['id']} had already finished {status_line(value['job'])}"}]
        return [{"type": "text", "text": f"requested cancellation of job {value['job']['id']}"}]

    return Tool(
        name="job_kill",
        description=(
            "Request cancellation of a running background job by job id. Returns immediately; "
            "the job settles as killed once its work actually stops."
        ),
        parameters={
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string", "required": True,
                    "description": "Job id returned by the tool that started the background work.",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional short reason, recorded in the log and forwarded to the job.",
                },
            },
        },
        output={
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "outcome": {
                        "type": "string",
                        "required": True,
                        "enum": ["cancellation-requested", "already-finished"],
                    },
                    "job": {**PUBLIC_TASK_SCHEMA, "required": True},
                },
            },
        },
        render=render,
        execute=execute,
        finalize_content=finalize_job_task_content(jobs),
    )


def register(tool_registry, jobs, config: dict | None = None) -> None:
    """把三工具注册进 ToolRegistry（上游 tool-jobs apply 的工具注册面）。"""
    cfg = resolve_config(config)
    tool_registry.register(job_output_tool(jobs, cfg["waitTimeoutMs"], cfg["maxWaitTimeoutMs"]))
    tool_registry.register(job_list_tool(jobs))
    tool_registry.register(job_kill_tool(jobs))
