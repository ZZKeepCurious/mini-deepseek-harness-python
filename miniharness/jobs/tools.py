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

mini 简化（须在文档标注）：
  * 无结构化 canonical value + native renderer 分离：execute 直接返回
    模型可见渲染文本（tool/result 以文本落日志，语义一致）
  * 无 finalizeContent/pre-execute 挂钩：字节封顶在 execute 内完成
  * owner 无 agent/inbox/claimed 会话事件：经 AgentLoop.on_inbox_claimed
    钩子列表近似（payload 语义对齐，见 AGENTS.md 简化清单）
"""
from __future__ import annotations

import asyncio
import threading
from typing import Any

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

    wakeup：idle owner 开 turn（预算 maxConsecutiveWakes，user 输入恢复）；
    busy owner 一律注入（notice 进下一步 inbox，同一步合并多个结算）。
    `ctx` 为注册方上下文（上游 tool-jobs 从自己组合 scope 注册；mini 显式
    传参）：监听器只接收该 scope 覆盖的 owner 的结算，缺省=全局层。
    """
    cfg = resolve_config(config)
    delivery = cfg["completionDelivery"]
    wake_budget = cfg["maxConsecutiveWakes"]
    spent_wakes: dict[int, int] = {}
    armed: set[int] = set()
    lock = threading.Lock()

    def reset_budget(owner: Any) -> None:
        with lock:
            spent_wakes.pop(id(owner), None)

    def on_done(snapshot: dict, owner: Any) -> None:
        if snapshot["reported"] or owner is None:
            return
        # 用户输入消费后恢复预算（mini 经 AgentLoop.on_inbox_claimed 钩子近似）
        if id(owner) not in armed and hasattr(owner, "on_inbox_claimed"):
            armed.add(id(owner))
            owner.on_inbox_claimed(reset_budget)
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


# ---------- 三工具 ----------

def job_output_tool(jobs, wait_default: int, wait_cap: int) -> Tool:
    async def execute(args: dict, exec_: Any) -> str:
        task_id = validate_job_id(args.get("job_id"))
        caller = getattr(exec_, "agent", None)
        jobs.get(task_id, caller)  # 存在性 + 会话栅栏先验
        if args.get("wait") is True:
            timeout = min(args.get("timeout_ms") or wait_default, wait_cap)
            # jobs.wait 是阻塞轮询：to_thread 防止卡住事件循环（abort 信号透传）
            await asyncio.to_thread(jobs.wait, task_id, timeout, caller,
                                    getattr(exec_, "signal", None))
        read = jobs.read(task_id, caller)
        body = read["text"] if read["text"] else "(no new output)"
        if body.endswith("\n"):
            body = body[:-1]
        suffix = f"\n{status_line(read['snapshot'])}"
        return fit_with_suffix(body, suffix, read["snapshot"].get("outputLimitBytes"), "\n[output truncated]")

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
        execute=execute,
    )


def job_list_tool(jobs) -> Tool:
    async def execute(_args: dict, exec_: Any) -> str:
        visible = [public_job(s) for s in jobs.list(getattr(exec_, "agent", None))]
        if not visible:
            return "(no background jobs)"
        return "\n".join(f"{t['id']} [{t['kind']}] {t['status']} — {t['label']}" for t in visible)

    return Tool(
        name="job_list",
        description="List your background jobs (running and finished) with their ids, kinds, and statuses.",
        parameters={"type": "object", "properties": {}},
        output={"schema": {"type": "array", "items": PUBLIC_TASK_SCHEMA}},
        execute=execute,
    )


def job_kill_tool(jobs) -> Tool:
    async def execute(args: dict, exec_: Any) -> str:
        task_id = validate_job_id(args.get("job_id"))
        caller = getattr(exec_, "agent", None)
        result = jobs.kill(task_id, caller, args.get("reason"))
        snapshot = public_job(jobs.get(task_id, caller))
        if result == "already-finished":
            return f"job {task_id} had already finished {status_line(snapshot)}"
        return f"requested cancellation of job {task_id}"

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
        execute=execute,
    )


def register(tool_registry, jobs, config: dict | None = None) -> None:
    """把三工具注册进 ToolRegistry（上游 tool-jobs apply 的工具注册面）。"""
    cfg = resolve_config(config)
    tool_registry.register(job_output_tool(jobs, cfg["waitTimeoutMs"], cfg["maxWaitTimeoutMs"]))
    tool_registry.register(job_list_tool(jobs))
    tool_registry.register(job_kill_tool(jobs))
