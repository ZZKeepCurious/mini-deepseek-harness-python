"""模型侧 job_output / job_list / job_kill 三工具 + 完成 notice 投递。

对齐 packages/jobs/tool-jobs/src/index.ts。契约要点：
  * 三工具是 kind 无关的通用作业控制面；共享 PublicJobSnapshot（id/kind/label/
    status/detail/startedAt/finishedAt），刻意剔除 ownerSession 与内部 reported
  * job_output 默认非阻塞；wait:true 等至配置上限，超时返回运行态而非 TOOL_TIMEOUT
  * 完成 notice：unreported 完成经 onJobDone 投递——busy owner 注入 inbox，
    idle owner 默认 wakeup 开 turn（maxConsecutiveWakes=3 封顶，自激链刹车），
    quiet 模式一律注入；user 输入消费后恢复预算
  * producer 提供 outputLimitBytes 时，输出读与 notice 都按完整 UTF-8 结果
    字节封顶（含 status 元数据），多字节字符不劈裂
  * canonical value + output.render 分离（execute 返回结构化数据，render 生成
    模型可见 content blocks）；工具层 finalizeContent 兜底二次截断：
    job_output/job_kill 结算前按 outputLimitBytes 收口模型可见内容（保状态行，
    对齐 tool-jobs finalizeTaskContent）。载体差异（登记录入 verified-diffs）：
    上游经 outputLimits WeakMap（tools/pre-execute prepend 缓存）取上限，mini
    直接每次现查即上游回退路径 `outputLimits.get(exec) ?? visibleOutputLimit(...)`
    的等价——无 policy 时行为一致

载体对齐（R2，2026-09-02）：owner 无钩子近似——jobs 经安装 ctx 订阅
  agent/inbox/claimed 恢复预算（payload {agent, message, turn}，仅 user 源
  恢复，对齐 tool-jobs spendWakes.delete），agent/disposed 防 id 键泄漏
"""
from __future__ import annotations

import asyncio
import threading
from types import MappingProxyType
from typing import Any, Callable

from ..core.tools import Tool

# 共享的公开作业快照 schema（对齐 tool-jobs PUBLIC_TASK_SCHEMA）
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
DEFAULT_MAX_CONSECUTIVE_WAKES = 3


def resolve_config(config: dict | None) -> dict:
    """解析 tool-jobs 配置（缺省值对齐 Config 表）；越界 fail loud。"""
    cfg = {
        "waitTimeoutMs": config.get("waitTimeoutMs", DEFAULT_WAIT_TIMEOUT_MS),
        "maxWaitTimeoutMs": config.get("maxWaitTimeoutMs", DEFAULT_MAX_WAIT_TIMEOUT_MS),
        "completionDelivery": config.get("completionDelivery", DEFAULT_COMPLETION_DELIVERY),
        "maxConsecutiveWakes": config.get("maxConsecutiveWakes", DEFAULT_MAX_CONSECUTIVE_WAKES),
    }
    if cfg["waitTimeoutMs"] > cfg["maxWaitTimeoutMs"]:
        raise ValueError(
            f"tool-jobs: waitTimeoutMs ({cfg['waitTimeoutMs']}) exceeds maxWaitTimeoutMs ({cfg['maxWaitTimeoutMs']})"
        )
    if cfg["completionDelivery"] not in ("quiet", "wakeup"):
        raise ValueError(f"tool-jobs: unknown completionDelivery {cfg['completionDelivery']!r}")
    if (not isinstance(cfg["maxConsecutiveWakes"], int)
            or isinstance(cfg["maxConsecutiveWakes"], bool)
            or cfg["maxConsecutiveWakes"] < 1):
        raise ValueError(
            f"tool-jobs: maxConsecutiveWakes ({cfg['maxConsecutiveWakes']}) must be a whole number of turns"
        )
    return cfg


# ---------- 公开快照与状态行 ----------

def public_job(snapshot: dict) -> dict:
    """去掉 ownerSession / reported 的模型可见投影。"""
    out: dict = {
        "id": snapshot["id"],
        "kind": snapshot["kind"],
        "label": snapshot["label"],
        "status": snapshot["status"],
        "startedAt": snapshot["startedAt"],
    }
    if snapshot.get("detail") is not None:
        out["detail"] = snapshot["detail"]
    if snapshot.get("finishedAt") is not None:
        out["finishedAt"] = snapshot["finishedAt"]
    return out


def status_line(snapshot: dict) -> str:
    """`[status: <status>]`，带可选 detail。"""
    detail = snapshot.get("detail")
    return f"[status: {snapshot['status']}, {detail}]" if detail is not None else f"[status: {snapshot['status']}]"


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


# ---------- Model-facing final cap (对齐 upstream finalizeTaskContent) ----------

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
    for snapshot in jobs.list(getattr(exec_, "agent", None)):
        if snapshot.get("id") == job_id:
            return snapshot.get("outputLimitBytes")
    return None


def finalize_job_task_content(jobs: Any) -> Callable[[Any, dict], list | None]:
    """job_output / job_kill 的 finalizeContent（对齐 tool-jobs finalizeTaskContent）。

    上游在结算时取 `outputLimits.get(exec) ?? visibleOutputLimit(ctx, exec)`；
    mini 无 WeakMap/pre-execute 捕获，回落为每次现查（等价回退路径），
    语义与上游默认（无 policy）一致。按调用方 agent 分辨率。
    """

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
                             "text": fit_with_suffix(content, suffix, max_bytes, "\n[output truncated]")}]
        return _bound_single_text(result["content"], max_bytes)

    return _hook


def fit_completion_notice(snapshot: dict) -> str:
    """完整 notice；超限时保留稳定 id 前缀与收集指令，先花剩余字节在变化部分。"""
    prefix = f"background job {snapshot['id']}"
    detail = f" ({snapshot['kind']}: {snapshot['label']}) finished {status_line(snapshot)}"
    action = "\nDone; job_output."
    complete = f"{prefix}{detail}. Read its output with job_output."
    max_bytes = snapshot.get("outputLimitBytes")
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
    """注册 onJobDone 监听：unreported 完成投递到精确 owner。

    wakeup：idle owner 开 turn（预算 maxConsecutiveWakes，user 输入经
    agent/inbox/claimed 事件恢复）；busy owner 一律注入（notice 进下一步
    inbox，同一步合并多个结算）。`ctx` 为注册方上下文（上游 tool-jobs 从
    自己组合 scope 注册；mini 显式传参）：onJobDone 监听与 claimed/disposed
    订阅都限该 scope 覆盖的 owner（事件经 ctx.on 以父 scope 收到子循环发送，
    对齐 tool-jobs ctx.on 订阅语义），缺省=全局层。
    """
    cfg = resolve_config(config)
    delivery = cfg["completionDelivery"]
    wake_budget = cfg["maxConsecutiveWakes"]
    spent_wakes: dict[int, int] = {}
    lock = threading.Lock()

    def handle_claimed(payload: dict) -> None:
        """agent/inbox/claimed：仅 user 源消息恢复预算（对齐 tool-jobs
        spendWakes.delete(agent) 的 source.kind === 'user' 判定）。"""
        message = payload.get("message") or {}
        source = message.get("source") if isinstance(message, dict) else None
        if isinstance(source, dict) and source.get("kind") == "user":
            with lock:
                spent_wakes.pop(id(payload.get("agent")), None)

    def handle_disposed(payload: dict) -> None:
        """agent/disposed：清掉已销毁 loop 的预算项（防 id 键泄漏）。"""
        with lock:
            spent_wakes.pop(id(payload.get("agent")), None)

    def on_done(snapshot: dict, owner: Any) -> None:
        if snapshot["reported"] or owner is None:
            return
        notice = fit_completion_notice(snapshot)
        with lock:
            should_wake = (delivery == "wakeup" and getattr(owner, "status", None) == "idle"
                           and spent_wakes.get(id(owner), 0) < wake_budget)
            if should_wake:
                spent_wakes[id(owner)] = spent_wakes.get(id(owner), 0) + 1
        if should_wake:
            owner.followup(notice, source="tool-jobs")
        else:
            owner.inject(notice, source="tool-jobs")

    jobs.on_job_done(on_done, ctx)
    scope = ctx if ctx is not None else getattr(jobs, "ctx", None)
    if scope is not None:
        scope.on("agent/inbox/claimed", handle_claimed)
        scope.on("agent/disposed", handle_disposed)


# ---------- 三工具 ----------

def job_output_tool(jobs, wait_default: int, wait_cap: int) -> Tool:
    async def execute(args: dict, exec_: Any) -> dict:
        task_id = validate_job_id(args.get("job_id"))
        caller = getattr(exec_, "agent", None)
        jobs.get(task_id, caller)  # 存在性 + 会话栅栏先验
        if args.get("wait") is True:
            timeout = min(args.get("timeout_ms") or wait_default, wait_cap)
            # jobs.wait 是阻塞轮询：to_thread 防止卡住事件循环（abort 信号透传）
            await asyncio.to_thread(jobs.wait, task_id, timeout, caller,
                                    getattr(exec_, "signal", None))
        read = jobs.read(task_id, caller)
        return {"text": read["text"], "job": public_job(read["snapshot"])}

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
        return [public_job(s) for s in jobs.list(getattr(exec_, "agent", None))]

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
        caller = getattr(exec_, "agent", None)
        result = jobs.kill(task_id, caller, args.get("reason"))
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
