"""模型侧子代理委托工具（对齐 packages/subagent/tool-subagent/src/index.ts）。

契约要点（与上游逐字一致）：
  * schema：description / prompt 必填；`run_in_background` 可选布尔——
    enableRunInBackground=false 的实例整个省略该参数，且执行期强制后台调用
    逐字拒绝（index.ts:257）。
  * 文案随 provider 形态变化：mini 子会话恒继承父 completed turns → 取
    inheritsParentContext=true 分支（index.ts:214-225）；后台默认值随
    backgroundMode（continuable 默认后台、one-shot 默认前台，
    resolveDelegationRun index.ts:249-267）。
  * canonical value 三形态 + render（index.ts:329-367）：background → job id、
    continuable → 持久 child id、foreground → 输出文本块拼接。
  * 非 completed 终局按 stopReasonError 映射为错误并附保留的部分输出
    （index.ts:125-157）；maxDepth 由 manager 强制（上游 depthLimit 能力位）。

mini 教学适配（有意保留，须在文档标注）：
  * 上游 provider 抽象（capabilities/provider-added|removed 事件/多通道）未复
    现：mini 单一 in-process continuable 通道，provider 名固定 CONTINUATION_PROVIDER。
  * 后台两模式统一经 ctx.jobs 承载（producer 线程跑子首回合；cancel →
    interrupt 子代理）：上游 continuable 后台不建 job（inbox 接受即返回），
    mini 同步模型需要确定性线程边界；完成通知因此经 jobs notice + 结算
    notice 双路到达（语义一致，多一条 jobs 收条）。
"""
from __future__ import annotations

import threading
from typing import Any

from ...core.system_prompt import SYSTEM_PROMPT_SERVICE
from ...core.tools import Tool, ToolExec
from ...jobs.types import JobDoneBox
from .continuation import (
    CONTINUATION_PROVIDER,
    SubagentContinuationManager,
    SubagentError,
    epoch_stop_reason,
    final_assistant_output,
)

__all__ = ["install_subagent_delegation_tool"]

# 结算关键词 → 错误头（逐字 index.ts:125-142）
_STOP_REASON_ERRORS = {
    "completed": None,
    "aborted": "subagent run was cancelled",
    "error": "subagent run failed",
    "max-tokens": "subagent run hit its token limit before finishing",
    "refusal": "subagent declined the task",
}

# 工具描述与参数描述（逐字 index.ts:214-237，inheritsParentContext=true 分支：
# mini 子会话由父 completed-turn 前缀播种，恒继承会话历史）
_DESCRIPTION = (
    "Delegate a task to a subagent that inherits this conversation: a child agent seeded with all "
    "completed turns so far (it does not see the current in-flight turn). Use this when the subtask "
    "builds on this conversation's context — a follow-up analysis, "
    "a review, a continuation — without consuming this conversation's context for the work itself. "
    "You receive its result, not its intermediate steps."
)
_PROMPT_DESCRIPTION = (
    "The task for the subagent. It already sees this conversation's completed turns, so build on them "
    "freely and state only what is new."
)

# 描述后缀（逐字 index.ts:301-308）
_SUFFIX_CONTINUABLE = (
    " This tool runs in the background by default, immediately returns a durable subagent id, and keeps "
    "the child conversation available for later turns. When that run settles, the runtime sends the "
    "parent a notice containing its outcome and any final assistant message; `send_message` starts a "
    "later turn in the same child conversation. Set `run_in_background: false` only when your next "
    "action depends on receiving the result."
)
_SUFFIX_ONE_SHOT = (
    " This call waits for the result by default. Set `run_in_background: true` to return a job id; "
    "collect with `job_output` and stop with `job_kill`."
)
_SUFFIX_DISABLED = " This call waits for the subagent and returns its result."

# run_in_background 参数描述（逐字 index.ts:320-326）
_PARAM_DESC_CONTINUABLE = (
    "Whether to run in the background and return a durable subagent id immediately. Defaults to true. "
    "Set false to wait for the result when your next action depends on it."
)
_PARAM_DESC_ONE_SHOT = (
    "Whether to run as a background job and return its id. Defaults to false; collect with "
    "job_output or stop with job_kill."
)

# continuable 常驻提示节（逐字 index.ts:466）
_SECTION_TEXT = (
    "Use {name} in the background by default. Start independent delegations together in one assistant "
    "message and continue useful work while they run. Set `run_in_background: false` only when your "
    "next action depends on that subagent's result. When a background run settles, the runtime sends "
    "you a notice containing its outcome and any final assistant message."
)


def _stop_reason_error(stop: str) -> str | None:
    """终局关键词 → 错误头（合并可扩展 union：未知原因按异常终局处理）。"""
    if stop in _STOP_REASON_ERRORS:
        return _STOP_REASON_ERRORS[stop]
    return f"subagent run ended abnormally ({stop})"


def _with_partial_text(error: str, output: list | None) -> str:
    """错误头附保留的部分输出（逐字 index.ts:151-157）。"""
    text = "".join(
        b.get("text", "") for b in (output or []) if isinstance(b, dict) and b.get("type") == "text"
    )
    if not text:
        return error
    return f"{error}\nPartial output before the run ended:\n{text}"


def _output_value_text(values: Any) -> str:
    """canonical 输出块数组 → 模型可见文本（只信 text 块，index.ts:102-109）。"""
    if not isinstance(values, list):
        return ""
    return "".join(
        b.get("text", "")
        for b in values
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    )


def _resolve_delegation_run(request: dict, *, background_enabled: bool, continuable: bool) -> bool:
    """模型的后台请求解析为唯一执行路由（index.ts:249-267）。

    @returns 是否后台执行。
    """
    if not background_enabled:
        # 校验器不拒绝未声明键，schema 省略还需执行期强制
        if request.get("run_in_background") is True:
            raise RuntimeError(
                "run_in_background is disabled for this tool instance "
                "(enableRunInBackground: false)"
            )
        return False
    return bool(request.get("run_in_background", continuable))


def install_subagent_delegation_tool(
    ctx: Any,
    reg: Any,
    manager: SubagentContinuationManager,
    config: dict | None = None,
) -> Tool:
    """在父注册表安装模型侧委托工具（默认名 `subagent`）。

    config：tool_name / enable_run_in_background / background_mode
    （'one-shot' | 'continuable'）/ persona / tool_filter（allow 列表）。
    continuable 模式同时注册常驻提示节（systemPrompt 服务须已提供）。
    """
    cfg = config or {}
    provider = cfg.get("provider", CONTINUATION_PROVIDER)
    if provider != CONTINUATION_PROVIDER:
        raise SubagentError(
            f"tool-subagent: provider {provider!r} 不可用（mini 仅内建 {CONTINUATION_PROVIDER!r}）",
            "UNAVAILABLE",
        )
    tool_name = cfg.get("tool_name", "subagent")
    background_enabled = cfg.get("enable_run_in_background", True) is not False
    continuable = (cfg.get("background_mode") or "one-shot") == "continuable"
    persona = cfg.get("persona")
    tool_filter = cfg.get("tool_filter")

    if background_enabled and continuable:
        svc = ctx.get(SYSTEM_PROMPT_SERVICE)
        section_api = getattr(svc, "section", None) if svc is not None else None
        if section_api is not None:
            # order 118：report-guidance(117) 与 delegation-context(120) 之间
            # （mini 约定；上游 PromptLayer 优先级体系未复现）
            section_api(f"tool:{tool_name}", 118, _SECTION_TEXT.format(name=tool_name))

    suffix = (
        _SUFFIX_CONTINUABLE if background_enabled and continuable
        else _SUFFIX_ONE_SHOT if background_enabled
        else _SUFFIX_DISABLED
    )
    properties: dict[str, Any] = {
        "description": {
            "type": "string",
            "description": "A short (3-5 word) description of the delegated task, for display.",
        },
        "prompt": {"type": "string", "description": _PROMPT_DESCRIPTION},
    }
    if background_enabled:
        properties["run_in_background"] = {
            "type": "boolean",
            "description": _PARAM_DESC_CONTINUABLE if continuable else _PARAM_DESC_ONE_SHOT,
        }

    async def execute(args: dict, exec_: ToolExec):
        parent = exec_.agent
        if parent is None:
            raise RuntimeError("subagent tool requires a calling agent (exec.agent was undefined)")
        label = args["description"]
        run_in_background = _resolve_delegation_run(
            args, background_enabled=background_enabled, continuable=continuable,
        )
        if not run_in_background:
            child_id, output, stop = await _run_foreground(
                manager, label, args["prompt"], persona, tool_filter)
            error = _stop_reason_error(stop)
            if error is not None:
                raise RuntimeError(_with_partial_text(error, output))
            return {"kind": "foreground", "runId": child_id, "output": output or []}
        return _start_background(ctx, manager, parent, label, args["prompt"],
                                 persona, tool_filter, continuable)

    tool = Tool(
        name=tool_name,
        description=_DESCRIPTION + suffix,
        parameters={"type": "object", "properties": properties,
                    "required": ["description", "prompt"]},
        execute=execute,
        render=lambda value: [
            {"type": "text", "text": (
                f"started background subagent job {value['jobId']}"
                if value.get("kind") == "background"
                else f"started subagent {value['subagentId']}"
                if value.get("kind") == "continuable"
                else _output_value_text(value.get("output"))
            )},
        ],
    )
    reg.register(tool)
    return tool


def _epoch_result(manager: SubagentContinuationManager, child_id: str, base: int) -> tuple[str, list | None]:
    """从持久化事件折叠本 epoch 的（终局关键词, 最终输出）。"""
    info = manager.persistence.inspect(child_id)
    epoch = info["events"][base:]
    return epoch_stop_reason(epoch), final_assistant_output(epoch)


async def _run_foreground(
    manager: SubagentContinuationManager, label: str, prompt: str,
    persona: str | None, tool_filter: list[str] | None,
) -> tuple[str, list | None, str]:
    """前台委托：创建子会话 + 循环内内联泵首回合，收集（child_id, 输出, 终局）。

    必须走 send_message_async（循环内检测 → _submit_async 内联
    `await child._pump_async()`）：子 turn/end 先于工具返回落盘，结果确定性。
    同步门面 send_message 在运行中的事件循环里会退化成 fire-and-forget driver，
    与 asyncio.run 的拆除竞速。上游前台对 continuable provider 也等待首回合
    结果（jobs.spec.ts:1115 同款语义：仅显式 run_in_background:false 时等待）；
    结算通知照常投递父代理（父 running → next-step 边界消费）。
    """
    child_id = manager.start_continuable(label=label, tool_filter=tool_filter, persona=persona)
    base = len(manager.persistence.inspect(child_id)["events"])
    await manager.send_message_async(child_id, prompt, source="parent")
    stop, output = _epoch_result(manager, child_id, base)
    return child_id, output, stop


def _job_outcome(stop: str) -> dict:
    """终局关键词 → JobOutcome（aborted → killed；其余非 completed → failed）。"""
    if stop == "completed":
        return {"status": "completed"}
    if stop == "aborted":
        return {"status": "killed"}
    return {"status": "failed", "detail": _stop_reason_error(stop) or "subagent run failed"}


def _start_background(
    ctx: Any, manager: SubagentContinuationManager, parent: Any, label: str,
    prompt: str, persona: str | None, tool_filter: list[str] | None,
    continuable: bool,
) -> dict:
    """后台委托：jobs producer 在工作线程跑子首回合。

    continuable 模式回持久 child id（子会话保留供 send_message 续跑）；
    one-shot 回 job id。job cancel → interrupt 子代理（kill 语义）。
    """
    jobs = ctx.get("jobs")
    if jobs is None:
        raise RuntimeError(
            "background jobs unavailable: load @deepseek-ai/dsh-jobs and @deepseek-ai/dsh-tool-jobs"
        )
    child_id = manager.start_continuable(label=label, tool_filter=tool_filter, persona=persona)
    base = len(manager.persistence.inspect(child_id)["events"])
    box = JobDoneBox()

    def work() -> None:
        try:
            manager.send_message(child_id, prompt, source="parent")
        except BaseException as error:  # noqa: BLE001 - reject → 注册表转 failed
            box.fail(error)
            return
        stop, _ = _epoch_result(manager, child_id, base)
        box.settle(_job_outcome(stop))

    worker = threading.Thread(target=work, name=f"subagent-job-{child_id}", daemon=True)
    job_id_ = jobs.start({
        "kind": "subagent",
        "label": label,
        "owner": parent,
        "run": lambda: {
            "done": box,
            # kill 语义：中断激活中的子代理（已结算 → 接受性 no-op）
            "cancel": lambda reason=None: manager.interrupt(child_id, cause="parent"),
        },
    })
    worker.start()
    if continuable:
        return {"kind": "continuable", "subagentId": child_id}
    return {"kind": "background", "jobId": job_id_}
