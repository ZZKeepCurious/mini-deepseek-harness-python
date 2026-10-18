"""模型侧子代理委托工具（对齐 packages/subagent/tool-subagent/src/index.ts）。

契约要点（与上游逐字一致，0.2.1 起）：
  * schema：`cwd`（可选）/ `description`（必填）/ `prompt`（必填）；模型选择
    启用时追加 provider/model/reasoning_effort。**不再有 run_in_background /
    background_mode**——每次委托都是独立管理的激活，工具立即返回子 id，
    运行结束由运行时通知模型（index.ts:215）。
  * 描述后缀取 provider 有 `prepareContinuable` 分支（mini 进程内通道恒有）：
    "This tool starts an independently managed subagent and immediately returns
    its id. The runtime notifies you when it finishes. The child reports results
    with `send_message`; ..."（index.ts:215-218）。
  * canonical value 恒为 `{kind:'activation', subagentId}`（index.ts:264-274）；
    render → `started subagent {id}`。
  * 配置拒收 `persona`/`toolFilter`：0.2.1 起人设与工具过滤改由 Agent Preset
    承担并以 `agentPreset` 选择（index.ts:147-151，逐字错误）。
  * maxDepth 由 manager 强制（上游 depthLimit 能力位）；工具另注册
    `tool:{name}:depth` 运行时上下文，在达到上限的 agent 上提示不要委派
    （index.ts:382-395）。

mini 教学适配（有意保留，须在文档标注）：
  * 上游 provider 抽象（capabilities/provider-added|removed 事件/多通道）未复
    现：mini 单一 in-process continuable 通道，provider 名固定 CONTINUATION_PROVIDER。
    配置 `agent_preset`/`allowed_agent_presets` 使子代理可独立选择 preset（经
    `ctx.agentPresets` 挂载子组合：工具视图 + persona）；因 mini 合并了上游
    spawn/fork 双通道，选择不受 `inheritsParentContext` 限制（上游 fork 通道禁选），
    见 verified-diffs 载体差异。
  * 同步载体下无 driver 的父内联泵完子首回合再返回 id（上游子回合并发运行）；
    结算通知照常投递父（见 continuation.py 模块头 A7/A8）。
"""
from __future__ import annotations

from typing import Any

from ...core.system_prompt import SYSTEM_PROMPT_SERVICE
from ...core.tools import Tool, ToolExec
from .continuation import (
    CONTINUATION_PROVIDER,
    SubagentContinuationManager,
    SubagentError,
    delegation_depth_of,
    epoch_stop_reason,
    final_assistant_output,
    subagent_diagnostic,
)

__all__ = ["install_subagent_delegation_tool"]

# 工具描述与参数描述（逐字 index.ts:113-137，inheritsParentContext=true 分支：
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
_PARAM_DESC_CWD = (
    "Initial child working directory. Relative paths use your current directory; omitted inherits it. "
    "Later directory changes in either agent are independent."
)

# 描述后缀（逐字 index.ts:215-218，provider 有 prepareContinuable 分支）
_SUFFIX_MANAGED = (
    " This tool starts an independently managed subagent and immediately returns its id. The runtime "
    "notifies you when it finishes. The child reports results with `send_message`; use `send_message` "
    "to steer it while running or continue its conversation after it finishes."
)

# continuable 常驻提示节（逐字 index.ts:379）
_SECTION_TEXT = (
    "Start independent delegations with {names} together in one assistant message and continue "
    "useful work while they run."
)

# 委派深度上下文（逐字 index.ts:392-393）
_DEPTH_CONTEXT_TEXT = (
    "Delegation depth for `{name}`: current {depth}, maximum {max_depth}, remaining levels 0."
    " Do not call `{name}` at this depth; carry out the work directly."
)

# 工具 canonical 输出 schema（逐字 index.ts:264-274）
_ACTIVATION_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "const": "activation"},
        "subagentId": {"type": "string"},
    },
    "required": ["kind", "subagentId"],
}

# 已弃用的配置键（0.2.1 起逐字拒绝，index.ts:147-151）：mini 配置用 snake_case
# 键名，错误文案沿用上游的 persona/toolFilter 字段名。
_REMOVED_CONFIG_FIELDS = (("persona", "persona"), ("tool_filter", "toolFilter"),
                          ("toolFilter", "toolFilter"))

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


def _activation_render(args: dict, value: dict) -> list[dict]:
    """canonical 激活值 → 模型可见文本（逐字 index.ts:273）。"""
    return [{"type": "text", "text": f"started subagent {value['subagentId']}"}]


def _epoch_result(manager: SubagentContinuationManager, child_id: str,
                  base: int) -> tuple[str, list | None, str | None]:
    """从持久化事件折叠某 epoch 的（终局关键词, 最终输出, 失败诊断）。

    供编程调用方（workflow_ptc 的 `agent` 绑定）在子首回合结算后读取终局；
    模型侧委托工具不再使用（0.2.1 起结果经运行时通知投递）。
    """
    info = manager.persistence.inspect(child_id)
    epoch = info["events"][base:]
    return (epoch_stop_reason(epoch), final_assistant_output(epoch),
            subagent_diagnostic(epoch))


class _SubagentPresetSelection:
    """一个委托工具上部署方拥有的 preset 选择（对齐 preset-selection.ts）。

    config：`agent_preset?`（独立缺省 preset；模型未选择且配置了缺省时用它，否则
    继承父）+ `allowed_agent_presets?`（非空允许名单，暴露模型参数 `agent_preset`；
    省略即关闭模型选择）。缺省 id 必须属于允许名单；名单不得有重复。
    """

    def __init__(self, config: dict):
        agent_preset = config.get("agent_preset")
        allowed = config.get("allowed_agent_presets")
        if agent_preset is not None and (not isinstance(agent_preset, str)
                                         or agent_preset == ""):
            raise RuntimeError("tool-subagent: agent_preset must be a non-empty string")
        if allowed is not None:
            if (not isinstance(allowed, (list, tuple)) or len(allowed) == 0
                    or any(not isinstance(item, str) or item == "" for item in allowed)):
                raise RuntimeError(
                    "tool-subagent: allowed_agent_presets must be a non-empty array "
                    "of preset ids")
            if len(set(allowed)) != len(allowed):
                raise RuntimeError(
                    "tool-subagent: allowed_agent_presets contains duplicate preset ids")
            if agent_preset is not None and agent_preset not in allowed:
                raise RuntimeError(
                    "tool-subagent: agent_preset must be included in allowed_agent_presets")
        self._agent_preset = agent_preset
        self._allowed = list(allowed) if allowed is not None else None

    @property
    def configured(self) -> bool:
        return self._agent_preset is not None or self._allowed is not None

    @property
    def choices(self) -> list[str] | None:
        """模型参数里公开的 preset id 集合（未配允许名单 → None，参数不暴露）。"""
        return self._allowed

    @property
    def description(self) -> str:
        fallback = ("Omit to inherit the parent agent's current preset."
                    if self._agent_preset is None
                    else f"Omit to use {self._agent_preset}.")
        text = f"Agent preset for this child. {fallback}"
        if self._allowed:
            text += f" Available presets: {', '.join(self._allowed)}"
        return text

    def validate(self, ctx: Any) -> None:
        """发布工具前的配置校验：配置了选择就必须挂 agent-presets 服务。"""
        if self.configured and ctx.get("agentPresets") is None:
            raise RuntimeError(
                "tool-subagent: agent preset selection requires the agent-presets service")

    def resolve(self, args: dict) -> str | None:
        """校验模型输入并套用部署缺省；返回选中 id，或 None 表示继承父。"""
        value = args.get("agent_preset")
        if value is None:
            return self._agent_preset
        if self._allowed is None:
            raise RuntimeError(
                "agent_preset selection is disabled for this tool instance")
        if not isinstance(value, str) or value not in self._allowed:
            raise RuntimeError(
                f"agent_preset must be one of: {', '.join(self._allowed)}")
        return value


def install_subagent_delegation_tool(
    ctx: Any,
    reg: Any,
    manager: SubagentContinuationManager,
    config: dict | None = None,
) -> Tool:
    """在父注册表安装模型侧委托工具（默认名 `subagent`）。

    config：tool_name / provider / agent_options（{provider?, model?,
    reasoningEffort?} 子默认路由）/ model_selection（opt-in 模型选择；真值要求
    model_routes 非空）/ model_routes（[{provider, model}] 允许路由，成为
    list_subagent_models 编目与策略拦截面）/ max_depth（子绝对深度上限）。

    0.2.1 起拒收 `persona`/`toolFilter`（逐字错误，index.ts:147-151）；`provider`
    非 CONTINUATION_PROVIDER 逐字 UNAVAILABLE。注册 `tool:{name}` 常驻提示节与
    `tool:{name}:depth` 委派深度上下文（systemPrompt 服务须已提供）。
    """
    cfg = config or {}
    for key, field in _REMOVED_CONFIG_FIELDS:
        if key in cfg:
            raise SubagentError(
                f"tool-subagent: {field} is no longer supported; configure persona and tool "
                "plugins in an Agent Preset and select it with agentPreset (spawn only)",
                "UNSUPPORTED_CONFIG",
            )
    provider = cfg.get("provider", CONTINUATION_PROVIDER)
    if provider != CONTINUATION_PROVIDER:
        raise SubagentError(
            f"tool-subagent: provider {provider!r} 不可用（mini 仅内建 {CONTINUATION_PROVIDER!r}）",
            "UNAVAILABLE",
        )
    tool_name = cfg.get("tool_name", "subagent")
    agent_options = cfg.get("agent_options")
    model_selection = cfg.get("model_selection") is True
    model_routes = _validate_model_routes(cfg.get("model_routes") or []) if model_selection else []
    if model_selection and not model_routes:
        raise RuntimeError(
            "enabled subagent model selection requires at least one allowed model")
    max_depth = cfg.get("max_depth")
    preset_selection = _SubagentPresetSelection(cfg)
    preset_selection.validate(ctx)

    svc = ctx.get(SYSTEM_PROMPT_SERVICE)
    if svc is not None:
        section_api = getattr(svc, "section", None)
        if section_api is not None:
            section_api(f"tool:{tool_name}",
                        svc.get_section_order("TOOL_SUBAGENT"),
                        _SECTION_TEXT.format(names=f"`{tool_name}`"))
        context_api = getattr(svc, "context", None)
        if context_api is not None and manager.resolve_max_depth(max_depth) is not None:
            context_api(f"tool:{tool_name}:depth",
                        svc.get_context_order("SUBAGENT_DELEGATION"),
                        _depth_context_callable(tool_name, manager, max_depth))

    if model_selection:
        # 发现工具只随启用实例注册（index.ts:356 同款：policy 定义才注册）
        reg.register(_list_subagent_models_tool(model_routes, manager))

    properties: dict[str, Any] = {
        "cwd": {"type": "string", "description": _PARAM_DESC_CWD},
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
    if preset_selection.choices is not None:
        # 允许名单配置时才暴露（index.ts:235-241：choices === undefined → 无参数）
        properties["agent_preset"] = {
            "type": "string",
            "enum": list(preset_selection.choices),
            "description": preset_selection.description,
        }

    async def execute(args: dict, exec_: ToolExec):
        parent = exec_.agent
        if parent is None:
            raise RuntimeError("subagent tool requires a calling agent (exec.agent was undefined)")
        label = args["description"]
        agent_preset = preset_selection.resolve(args)
        requested = _requested_agent_options(parent, agent_options, args, model_selection)
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
        # 取消信号（上游 exec.signal）在 mini 同步载体上体现为线程取消事件，
        # 经 ToolExec 传入；缺省 None 表示不取消。
        request: dict[str, Any] = {
            "prompt": [{"type": "text", "text": args["prompt"]}],
            "parent": parent,
        }
        if args.get("cwd") is not None:
            request["cwd"] = args["cwd"]
        if requested is not None:
            request["agent_options"] = requested
        if agent_preset is not None:
            request["agent_preset"] = agent_preset
        resolved_max = manager.resolve_max_depth(max_depth)
        if resolved_max is not None:
            request["max_depth"] = resolved_max
        started = await manager.start_activation_async({
            "provider": provider,
            "label": label,
            "request": request,
            "signal": getattr(exec_, "signal", None),
            "delivery": "parent",
        })
        return {"kind": "activation", "subagentId": started["childId"]}

    tool = Tool(
        name=tool_name,
        description=_DESCRIPTION + _SUFFIX_MANAGED
        + (_CHOICE_DESCRIPTION if model_selection else ""),
        parameters={"type": "object", "properties": properties,
                    "required": ["description", "prompt"]},
        execute=execute,
        output={"schema": _ACTIVATION_OUTPUT_SCHEMA},
        render=_activation_render,
    )
    reg.register(tool)
    return tool


def _depth_context_callable(tool_name: str, manager: SubagentContinuationManager,
                            max_depth: Any):
    """`tool:{name}:depth` 上下文可调用对象（上游 index.ts:382-395）。

    达到上限的 agent 上返回提示文本；否则空串（渲染时跳过）。
    """
    def render(context: dict) -> str:
        agent = (context or {}).get("agent")
        if agent is None:
            return ""
        resolved = manager.resolve_max_depth(max_depth)
        if resolved is None:
            return ""
        depth = delegation_depth_of(agent)
        if depth < resolved:
            return ""
        return _DEPTH_CONTEXT_TEXT.format(name=tool_name, depth=depth,
                                          max_depth=resolved)
    return render
