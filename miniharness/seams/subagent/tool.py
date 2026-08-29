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
    subagent_diagnostic,
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

# ---- 模型选择（对齐 tool-subagent model-selection.ts + index.ts:364-412）----
# providerRouteDefaults 恒未定义（mini continuation provider 无路由默认值），
# 取上游 describe `: inherit the parent route` 分支；inheritsParentContext=true
# 追加"换路由可能破坏继承前缀"句。
_SELECTION_DESCRIPTION_NO_DEFAULTS = (
    " Child LLM selection is optional. Omit `provider`, `model`, and `reasoning_effort` to use configured "
    "child defaults and inherit compatible missing values from the parent Agent. Supply `provider` and "
    "`model` together after using `list_subagent_models` to inspect advertised routes and efforts. "
    "Changing the effective route without naming an effort uses the selected model's default effort."
)
_CHOICE_DESCRIPTION = _SELECTION_DESCRIPTION_NO_DEFAULTS + (
    " Changing the route can prevent provider-side reuse of the inherited conversation prefix."
)
_PARAM_DESC_PROVIDER = (
    "LLM provider route for the child. Supply together with model; omit both to use configured child "
    "defaults or inherit the parent route."
)
_PARAM_DESC_MODEL = (
    "Model id interpreted by provider. Supply together with provider; omit both to use configured child "
    "defaults or inherit the parent route."
)
_PARAM_DESC_REASONING_EFFORT = (
    "Adapter-owned reasoning effort for the effective child route. Omit to inherit a compatible configured/"
    "parent effort or use a newly selected model's default."
)

# list_subagent_models 描述（逐字 list-models.ts:89-94）
_LIST_MODELS_DESCRIPTION = (
    "Discover LLM routes for subagents without changing the current Agent. Call with no arguments to list "
    "registered providers, with `provider` to list its advertised models, or with `provider` and `model` "
    "to inspect that exact model and its reasoning efforts. Catalog membership is advisory: an adapter may "
    "accept an unlisted model id. Use the returned ids with a delegation tool's `provider`, `model`, and "
    "`reasoning_effort` fields."
)


def _parent_options_for_delegation(parent: Any) -> dict:
    """父适配器当前路由（mini 版 parentAgentOptionsForDelegation）。

    上游以最新 request/header 优先于创建选项；mini 无 header 服务，适配器
    属性即有效路由（对齐该函数"有效值优先"的核心语义）。
    """
    adapter = getattr(parent, "adapter", None)
    options: dict[str, Any] = {}
    if adapter is not None:
        provider = getattr(adapter, "provider", None)
        model = getattr(adapter, "model", None)
        effort = getattr(adapter, "reasoning_effort", None)
        if provider is not None:
            options["provider"] = provider
        if model is not None:
            options["model"] = model
        if effort is not None:
            options["reasoningEffort"] = effort
    return options


def _has_delegation_model_request(request: dict) -> bool:
    """调用是否显式选择任一子 LLM 值（上游 hasDelegationModelRequest）。"""
    return any(key in request for key in ("provider", "model", "reasoning_effort"))


def _has_configured_llm_selection(options: dict | None) -> bool:
    """配置的子选项是否声明了 provider/model/reasoningEffort（上游
    hasConfiguredLlmSelection）。"""
    return bool(options and any(
        key in options for key in ("provider", "model", "reasoningEffort")))


def _effective_route(parent: Any, requested: dict | None) -> dict:
    """解析后的生效路由（provider/model/reasoningEffort 三键）。"""
    parent_options = _parent_options_for_delegation(parent)
    return {
        "provider": (requested or {}).get("provider", parent_options.get("provider")),
        "model": (requested or {}).get("model", parent_options.get("model")),
        "reasoningEffort": (requested or {}).get("reasoningEffort"),
    }


def _requested_agent_options(parent: Any, configured: dict | None,
                             request: dict, enabled: bool) -> dict | None:
    """模型选择合并（逐字语义 requestedAgentOptions，model-selection.ts:99-128）。

    无模型选择字段 → 原样返回配置默认（不动）；disabled 实例强行选择 →
    逐字拒绝；provider/model 必须成对提供；换路由而未带 effort →
    丢弃配置里路由属地的 effort（所选模型用自身默认档）。
    """
    if not _has_delegation_model_request(request):
        return configured
    if not enabled:
        raise RuntimeError("child model selection is disabled for this tool instance")
    for field in ("provider", "model", "reasoning_effort"):
        value = request.get(field)
        if value is not None and (not isinstance(value, str) or value == ""):
            raise RuntimeError(f"child LLM `{field}` must be non-empty")
    if (request.get("provider") is None) != (request.get("model") is None):
        raise RuntimeError("child LLM `provider` and `model` must be supplied together")
    parent_options = _parent_options_for_delegation(parent)
    baseline_provider = (configured or {}).get("provider", parent_options.get("provider"))
    baseline_model = (configured or {}).get("model", parent_options.get("model"))
    route_changed = request.get("provider") is not None and (
        request["provider"] != baseline_provider or request["model"] != baseline_model)
    configured_without_effort = {
        key: value for key, value in (configured or {}).items() if key != "reasoningEffort"}
    merged = dict(configured_without_effort if (
        route_changed and request.get("reasoning_effort") is None) else (configured or {}))
    if request.get("provider") is not None:
        merged["provider"] = request["provider"]
        merged["model"] = request["model"]
    if request.get("reasoning_effort") is not None:
        merged["reasoningEffort"] = request["reasoning_effort"]
    return merged


def _assert_allowed_model_selection(routes: list[dict], parent: Any,
                                    requested: dict | None, request: dict) -> None:
    """策略路由拦截（逐字语义 assertAllowedModelSelection，model-selection.ts:139-153）。

    无策略（未启用）或无显式选择 → 纯继承不受限；显式字段必须解析到策略
    routes 内的精确路由。
    """
    if not routes or not _has_delegation_model_request(request):
        return
    parent_options = _parent_options_for_delegation(parent)
    provider = (requested or {}).get("provider", parent_options.get("provider"))
    model = (requested or {}).get("model", parent_options.get("model"))
    if provider is None or model is None:
        raise RuntimeError(
            "cannot select child LLM values without an effective provider and model")
    if any(route["provider"] == provider and route["model"] == model for route in routes):
        return
    raise RuntimeError(
        f'child LLM route "{provider}/{model}" is not allowed for this Session')


def _validate_model_routes(routes: Any) -> list[dict]:
    """策略路由校验（逐字语义 assertAllowedModelRoutes，model-selection.ts:42-62）。

    数组、每项非空 provider/model、无重复路由。
    """
    if not isinstance(routes, (list, tuple)):
        raise RuntimeError("subagent model selection requires an array of routes")
    seen: set[str] = set()
    validated: list[dict] = []
    for candidate in routes:
        if (not isinstance(candidate, dict)
                or not isinstance(candidate.get("provider"), str)
                or not isinstance(candidate.get("model"), str)
                or not candidate["provider"] or not candidate["model"]):
            raise RuntimeError(
                "subagent model selection requires non-empty provider and model ids")
        route = {"provider": candidate["provider"], "model": candidate["model"]}
        key = f"{route['provider']}\x00{route['model']}"
        if key in seen:
            raise RuntimeError(
                f'subagent model selection repeats route "{route["provider"]}/{route["model"]}"')
        seen.add(key)
        validated.append(route)
    return validated


def _list_subagent_models_tool(routes: list[dict], manager: Any) -> Tool:
    """父注册表的 list_subagent_models 发现工具（对齐 list-models.ts）。

    教学简化：mini 无上游的 llm 服务与模型 catalog——路由策略本身就是
    编目（allowed routes 即全部可用子路由）；物理 provider 集合取命名
    provider 注册表 + 内建 fake 适配器工厂。
    """

    def known_providers() -> set[str]:
        return set(getattr(manager, "_providers", {})) | {"fake"}

    async def execute(args: dict, exec_: ToolExec):
        if args.get("model") is not None and args.get("provider") is None:
            raise RuntimeError("`model` requires `provider`")
        if args.get("provider") is None:
            providers = sorted(
                p for p in known_providers()
                if any(route["provider"] == p for route in routes))
            if not providers:
                return "(no LLM providers)"
            return "\n".join(f"{p} — {p}" for p in providers)
        if args.get("provider") == "":
            raise RuntimeError("`provider` must be non-empty")
        allowed = [route for route in routes
                   if route["provider"] == args["provider"]]
        if not allowed:
            raise RuntimeError(
                f'LLM provider "{args["provider"]}" is not allowed for this Session')
        if args["provider"] not in known_providers():
            available = ", ".join(
                p for p in sorted(known_providers())
                if any(route["provider"] == p for route in routes)) or "(none)"
            raise RuntimeError(
                f'LLM provider "{args["provider"]}" is not registered; '
                f"available providers: {available}")
        if args.get("model") is None:
            models = [route["model"] for route in allowed]
            if not models:
                return f"(no advertised models for {args['provider']})"
            return "\n".join(
                f"{args['provider']}/{model} — {model}" for model in sorted(models))
        if args.get("model") == "":
            raise RuntimeError("`model` must be non-empty")
        if not any(route["model"] == args["model"] for route in allowed):
            raise RuntimeError(
                f'child LLM route "{args["provider"]}/{args["model"]}" '
                "is not allowed for this Session")
        return (
            f"{args['provider']}/{args['model']} — {args['model']}\n"
            "Reasoning efforts:\n(no advertised reasoning efforts)"
        )

    return Tool(
        name="list_subagent_models",
        description=_LIST_MODELS_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "provider": {
                    "type": "string",
                    "description": (
                        "Registered LLM provider id. Omit to list providers."
                    ),
                },
                "model": {
                    "type": "string",
                    "description": (
                        "Exact model id to inspect. Requires provider; omit to list that provider's "
                        "advertised models."
                    ),
                },
            },
        },
        execute=execute,
        render=lambda value: [{"type": "text", "text": value}],
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
    （'one-shot' | 'continuable'）/ persona / tool_filter（allow 列表）/
    agent_options（{provider?, model?, reasoningEffort?} 子默认路由）/
    model_selection（opt-in 模型选择；真值要求 model_routes 非空）/
    model_routes（[{provider, model}] 允许路由，成为 list_subagent_models
    编目与策略拦截面）。
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
    agent_options = cfg.get("agent_options")
    model_selection = cfg.get("model_selection") is True
    model_routes = _validate_model_routes(cfg.get("model_routes") or []) if model_selection else []
    if model_selection and not model_routes:
        raise RuntimeError(
            "enabled subagent model selection requires at least one allowed model")

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
    if model_selection:
        # 发现工具只随启用实例注册（index.ts:356 同款：policy 定义才注册）
        reg.register(_list_subagent_models_tool(model_routes, manager))
    properties: dict[str, Any] = {
        "description": {
            "type": "string",
            "description": "A short (3-5 word) description of the delegated task, for display.",
        },
        "prompt": {"type": "string", "description": _PROMPT_DESCRIPTION},
    }
    if model_selection:
        properties["provider"] = {"type": "string", "description": _PARAM_DESC_PROVIDER}
        properties["model"] = {"type": "string", "description": _PARAM_DESC_MODEL}
        properties["reasoning_effort"] = {
            "type": "string", "description": _PARAM_DESC_REASONING_EFFORT,
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
        requested = _requested_agent_options(
            parent, agent_options, args, model_selection)
        _assert_allowed_model_selection(model_routes, parent, requested, args)
        if _has_delegation_model_request(args) or _has_configured_llm_selection(agent_options):
            # preflight：子创建前解析路由（上游 preflightChildLlmRoute 时机）。
            # mini 无 llm 服务，以"路由可解析为适配器"为等价判据；未知 provider
            # 的 UNAVAILABLE 在此由 resolve_route 抛出（fail loud before start）。
            effective = _effective_route(parent, requested)
            if effective["provider"] is None or effective["model"] is None:
                raise RuntimeError(
                    "cannot select child LLM values without an effective provider and model")
            manager.resolve_route(effective["provider"], effective["model"],
                                  effective.get("reasoningEffort"))
        run_in_background = _resolve_delegation_run(
            args, background_enabled=background_enabled, continuable=continuable,
        )
        if not run_in_background:
            child_id, output, stop = await _run_foreground(
                manager, label, args["prompt"], persona, tool_filter, parent=parent,
                agent_options=requested)
            error = _stop_reason_error(stop)
            if error is not None:
                raise RuntimeError(_with_partial_text(error, output))
            return {"kind": "foreground", "runId": child_id, "output": output or []}
        return _start_background(ctx, manager, parent, label, args["prompt"],
                                 persona, tool_filter, continuable,
                                 agent_options=requested)

    tool = Tool(
        name=tool_name,
        description=(_DESCRIPTION + suffix
                     + (_CHOICE_DESCRIPTION if model_selection else "")),
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


def _epoch_result(manager: SubagentContinuationManager, child_id: str, base: int) -> tuple[str, list | None, str | None]:
    """从持久化事件折叠本 epoch 的（终局关键词, 最终输出, 失败诊断）。"""
    info = manager.persistence.inspect(child_id)
    epoch = info["events"][base:]
    return (epoch_stop_reason(epoch), final_assistant_output(epoch),
            subagent_diagnostic(epoch))


async def _run_foreground(
    manager: SubagentContinuationManager, label: str, prompt: str,
    persona: str | None, tool_filter: list[str] | None,
    parent: Any = None, agent_options: dict | None = None,
) -> tuple[str, list | None, str]:
    """前台委托：创建子会话 + 循环内内联泵首回合，收集（child_id, 输出, 终局）。

    必须走 send_message_async（循环内检测 → _submit_async 内联
    `await child._pump_async()`）：子 turn/end 先于工具返回落盘，结果确定性。
    同步门面 send_message 在运行中的事件循环里会退化成 fire-and-forget driver，
    与 asyncio.run 的拆除竞速。上游前台对 continuable provider 也等待首回合
    结果（jobs.spec.ts:1115 同款语义：仅显式 run_in_background:false 时等待）；
    结算通知照常投递父代理（父 running → next-step 边界消费）。
    @param parent - 委托方 agent loop（嵌套时为子代理自身）；授权与所有权主体。
    @param agent_options - 模型选择解析后的子路由（start_continuable 透传）。
    """
    child_id = manager.start_continuable(label=label, tool_filter=tool_filter,
                                         persona=persona, parent=parent,
                                         agent_options=agent_options)
    base = len(manager.persistence.inspect(child_id)["events"])
    await manager.send_message_async(child_id, prompt, source="parent", parent=parent)
    stop, output, _ = _epoch_result(manager, child_id, base)
    return child_id, output, stop


def _job_outcome(stop: str, diagnostic: str | None = None,
                 output: str | None = None) -> dict:
    """终局关键词 → JobOutcome（对齐上游 run-settlement.ts runOutcome）。

    completed 携带最终文本（finalText）；无诊断的 aborted（本地取消）→
    killed；带 provider 诊断的 aborted（远程中止）与其余一切 reason →
    failed。失败 detail 取原始 stopReason 词，有诊断附 "; diagnostic: ..."
    （run-settlement failureDetail，rc.2 起 SubagentResult.diagnostic 经此
    进入 jobs 通道；诊断本身不进 subagent/end）。
    """
    if stop == "completed":
        return {"status": "completed", "output": output or ""}
    if stop == "aborted":
        if diagnostic is None:
            return {"status": "killed"}
        return {"status": "failed", "detail": f"aborted; diagnostic: {diagnostic}"}
    detail = stop
    if diagnostic:
        detail = f"{detail}; diagnostic: {diagnostic}"
    return {"status": "failed", "detail": detail}


def _start_background(
    ctx: Any, manager: SubagentContinuationManager, parent: Any, label: str,
    prompt: str, persona: str | None, tool_filter: list[str] | None,
    continuable: bool, agent_options: dict | None = None,
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
    child_id = manager.start_continuable(label=label, tool_filter=tool_filter,
                                         persona=persona, parent=parent,
                                         agent_options=agent_options)
    base = len(manager.persistence.inspect(child_id)["events"])
    box = JobDoneBox()

    def work() -> None:
        try:
            manager.send_message(child_id, prompt, source="parent", parent=parent)
        except BaseException as error:  # noqa: BLE001 - reject → 注册表转 failed
            box.fail(error)
            return
        stop, output, diagnostic = _epoch_result(manager, child_id, base)
        box.settle(_job_outcome(stop, diagnostic, _output_value_text(output)))

    worker = threading.Thread(target=work, name=f"subagent-job-{child_id}", daemon=True)
    job_id_ = jobs.start({
        "kind": "subagent",
        "label": label,
        "owner": parent,
        "run": lambda: {
            "done": box,
            # kill 语义：以委托方 ancestor 身份中断激活中的子代理
            # （已结算 → 接受性 no-op）
            "cancel": lambda reason=None: manager.interrupt(
                child_id, {"kind": "ancestor", "agent": parent}),
        },
    })
    worker.start()
    if continuable:
        return {"kind": "continuable", "subagentId": child_id}
    return {"kind": "background", "jobId": job_id_}
