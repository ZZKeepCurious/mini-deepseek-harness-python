"""system prompt 分节装配：sections / contexts / tools / variables 提供器 + 装配 waterfall。

上游对照：packages/core/system-prompt/src/index.ts（SystemPrompt 服务：
section / context / tools / variable / suppressRuntimeContext / assemble /
renderPrompt / renderContextSnapshot / orderTools）。

mini 范围说明：
  * 无 scope 层叠——所有注册视为全局层（上游 ScopedLayers 逐层 shadow，mini
    简化为单层；按注册序求值）。
  * assemble 派发 'system-prompt/assemble' waterfall（mini 瀑布流为单值
    线程化，payload = {assembly, context}，监听器返回改后的 wrapper 或调用
    next() 委派）；结果 assembly 权威，但 complete 节 / 运行时上下文抑制在
    waterfall 后恢复（上游同款）。
  * 装配上下文 {agent, session} 由 AgentLoop._system_prompt_text 传入。
  * runtime-context 快照（contexts）：render_context_sections /
    join_context_sections 提供节渲染面；AgentLoop 经
    core/agent_loop/runtime_context.py 投影把变化后的快照铸成 durable user
    消息注入对话流（上游 agent-loop/src/runtime-context.ts 同款）；无该投影
    时 contexts 仅存装配面。assembly.tools 提供器结果同理不直接成为请求工具
    列表（实际请求工具来自 ToolRegistry，见 _tool_definitions）。

装配：install_system_prompt(ctx) 提供 ctx "systemPrompt" 服务（幂等）。
AgentLoop._derive_history 在渲染时经 ctx.get("systemPrompt") 懒取；
无该服务时仅用 AgentLoop.system_prompt 字符串（既有行为不变）。
"""
from __future__ import annotations

import re
from typing import Any, Callable

from .scope import Context

__all__ = [
    "PERSONA_ORDER",
    "PERSONA_SECTION",
    "SYSTEM_PROMPT_SERVICE",
    "TOOL_ORDER_REST",
    "SystemPromptService",
    "install_system_prompt",
    "join_context_sections",
    "order_tools",
    "render_context_sections",
    "render_context_snapshot",
    "render_prompt",
]

SYSTEM_PROMPT_SERVICE = "systemPrompt"

# 部署人设节的保留名与 order（上游 index.ts：同名覆盖即替换生效）
PERSONA_SECTION = "deployment:persona"
PERSONA_ORDER = 0

# toolOrder 的未列出工具插入位（上游 index.ts TOOL_ORDER_REST）
TOOL_ORDER_REST = "<unlisted-tools>"

# 合法变量名（上游 index.ts VARIABLE_NAME）
_VARIABLE_NAME = re.compile(r"^[a-z][a-z0-9_]*$")
# 扫描位置的完整 {{name}} 组（后续再校验合法性）
_GROUP_AT = re.compile(r"^\{\{([^{}]*)\}\}")


def _validate_tool_order(tool_order) -> list[str] | None:
    if tool_order is None:
        return None
    seen = set()
    for name in tool_order:
        if name in seen:
            raise ValueError(f'toolOrder 重复列出 "{name}"')
        seen.add(name)
    if TOOL_ORDER_REST not in seen:
        raise ValueError(f'toolOrder 必须包含 rest 条目 "{TOOL_ORDER_REST}"（未列出工具插入位）')
    return list(tool_order)


def order_tools(tools: list[dict], tool_order: list[str] | None,
                known_names: set) -> list[dict]:
    """按 toolOrder 排序工具（上游 index.ts orderTools）：未列出工具在 rest
    位按名称字典序插入；配置了未注册工具名 fail loud。无 toolOrder 时字典序。
    """
    if any(t["name"] == TOOL_ORDER_REST for t in tools):
        raise ValueError(f'tool 提供器返回了保留名 "{TOOL_ORDER_REST}"')
    if tool_order is None:
        return sorted(tools, key=lambda t: t["name"])
    unknown = [name for name in tool_order
               if name != TOOL_ORDER_REST and name not in known_names]
    if unknown:
        raise ValueError(
            f'toolOrder 列出未注册工具 {unknown}; known: '
            f'{sorted(known_names) or "(none)"}'
        )
    listed = set(tool_order)
    rest = sorted((t for t in tools if t["name"] not in listed),
                  key=lambda t: t["name"])
    out = []
    for name in tool_order:
        if name == TOOL_ORDER_REST:
            out.extend(rest)
        else:
            out.extend(t for t in tools if t["name"] == name)
    return out


def _interpolate(input_: dict, variables: dict, kind: str) -> str:
    """插值一个节/上下文文本（上游 index.ts interpolate）：未知/畸形/空值
    引用抛错；孤立 `{{` 无闭合 `}}` 视为字面散文；替换值不再扫描。"""
    text = input_["text"]
    result = ""
    last = 0
    open_ = text.find("{{")
    while open_ >= 0:
        group = _GROUP_AT.search(text[open_:])
        if group is None:
            # 后续存在闭合括号 → 畸形；否则为字面散文
            if text.find("}}", open_ + 2) >= 0:
                raise ValueError(
                    f'畸形 prompt 变量引用 "{text[open_:open_ + 16]}…" in '
                    f'{kind} "{input_["name"]}" (引用必须是完整 {{name}} 组)'
                )
            result += text[last:open_ + 2]
            last = open_ + 2
            open_ = text.find("{{", last)
            continue
        name = group.group(1)
        if not _VARIABLE_NAME.match(name):
            raise ValueError(
                f'畸形 prompt 变量引用 "{{{{{name}}}}}" in {kind} '
                f'"{input_["name"]}" (变量名匹配 {_VARIABLE_NAME.pattern})'
            )
        if name not in variables:
            known = ", ".join(sorted(variables)) or "(none)"
            raise ValueError(
                f'未知 prompt 变量 "{{{{{name}}}}}" in {kind} '
                f'"{input_["name"]}"; registered variables: {known}'
            )
        value = variables[name]
        if value is None:
            raise ValueError(
                f'prompt 变量 "{{{{{name}}}}}" 本次装配无值 ({kind} "{input_["name"]}")'
            )
        result += text[last:open_] + value
        last = open_ + group.end()
        open_ = text.find("{{", last)
    return result + text[last:]


def render_prompt(assembly: dict) -> str:
    """渲染装配好的 prompt：插值、去空节、空行连接（上游 renderPrompt）。
    @param assembly - assemble() 返回的 PromptAssembly。
    """
    variables = assembly.get("variables", {})
    rendered = []
    for s in assembly.get("sections", []):
        text = _interpolate(s, variables, "section")
        if text:
            rendered.append(text)
    return "\n\n".join(rendered)


def render_context_sections(assembly: dict) -> list[dict]:
    """渲染运行时上下文节列表（上游 renderContextSections）：逐节插值后
    过滤空文本，返回 [{name, text}, ...]（保序）。"""
    variables = assembly.get("variables", {})
    return [
        {"name": c["name"], "text": t}
        for c in assembly.get("contexts", [])
        for t in [_interpolate(c, variables, "context")]
        if t != ""
    ]


def join_context_sections(sections: list[dict]) -> str:
    """连接节列表为完整快照文本（上游 joinContextSections）：空列表返回
    空串；非空带 "This snapshot supersedes..." 前缀按空行连接。"""
    body = "\n\n".join(s["text"] for s in sections)
    if body == "":
        return ""
    return f"Current runtime context. This snapshot supersedes earlier runtime-context snapshots.\n\n{body}"


def render_context_snapshot(assembly: dict) -> str:
    """渲染完整运行时上下文快照（上游 renderContextSnapshot）：
    join_context_sections(render_context_sections(assembly))。"""
    return join_context_sections(render_context_sections(assembly))


class SystemPromptService:
    """注册分节 / 上下文 / 工具 schema / 变量提供器，装配每次模型请求的 prompt。

    每个 section / context 携带 name / order / text（字符串或按装配上下文求值
    的可调用对象）；同名重复注册抛错（上游 NamedEntries 同语义）。
    """

    def __init__(self, ctx: Context, config: dict | None = None):
        self._ctx = ctx
        self._sections: list[dict[str, Any]] = []
        self._contexts: list[dict[str, Any]] = []
        self._tool_providers: list[Callable[[dict], dict]] = []
        self._variables: dict[str, Callable[[dict], str | None]] = {}
        self._runtime_context_suppressors: list[bool] = []
        self._seq = 0
        self._tool_order = _validate_tool_order((config or {}).get("toolOrder"))

    # ---------- 注册面 ----------

    def section(self, name: str, order: int, text: str | Callable[[dict], str],
                complete: bool = False) -> Callable:
        """注册一个有序 prompt section，返回其 disposer。
        @param name - 唯一节名（重复注册抛 ValueError）。
        @param order - 升序连接位置（上游约定：-100 身份、0 人设、plan 用 50）。
        @param text - 静态文本或按装配上下文（含 agent）求值的可调用对象。
        @param complete - 视作完整 system prompt：装配后该节成为唯一节。
        """
        if not isinstance(name, str) or name == "":
            raise ValueError("prompt section 需要一个非空 name")
        if not isinstance(order, int):
            raise ValueError(f"prompt section {name!r} 的 order 必须是整数")
        if not (callable(text) or isinstance(text, str)):
            raise TypeError(f"prompt section {name!r} 的 text 必须是字符串或可调用对象")
        if any(s["name"] == name for s in self._sections):
            raise ValueError(f"prompt section {name!r} 已注册")
        entry = {"name": name, "order": order, "text": text,
                 "complete": complete, "seq": self._seq}
        self._seq += 1
        self._sections.append(entry)
        return self._ctx.effect(
            lambda: (lambda: self._sections.remove(entry)),
            f"prompt section {name!r}")

    def context(self, name: str, order: int,
                text: str | Callable[[dict], str]) -> Callable:
        """注册一个有序动态上下文贡献（上游 PromptContext），返回 disposer。
        同名重复注册抛错；空文本在渲染时被跳过。"""
        if not isinstance(name, str) or name == "":
            raise ValueError("prompt context 需要一个非空 name")
        if not isinstance(order, int):
            raise ValueError(f"prompt context {name!r} 的 order 必须是整数")
        if not (callable(text) or isinstance(text, str)):
            raise TypeError(f"prompt context {name!r} 的 text 必须是字符串或可调用对象")
        if any(c["name"] == name for c in self._contexts):
            raise ValueError(f"prompt context {name!r} 已注册")
        entry = {"name": name, "order": order, "text": text, "seq": self._seq}
        self._seq += 1
        self._contexts.append(entry)
        return self._ctx.effect(
            lambda: (lambda: self._contexts.remove(entry)),
            f"prompt context {name!r}")

    def tools(self, provider: Callable[[dict], dict]) -> Callable:
        """注册一个工具 schema 提供器（上游 PromptLayer.toolProviders），
        返回 disposer。提供器返回 {schemas, knownNames?}；knownNames 缺省
        取 schemas 的名字集合，用于 toolOrder 校验。"""
        self._tool_providers.append(provider)
        return self._ctx.effect(
            lambda: (lambda: self._tool_providers.remove(provider)),
            "prompt tools provider")

    def variable(self, name: str,
                 provider: Callable[[dict], str | None]) -> Callable:
        """注册一个 prompt 变量（上游 PromptLayer.variables），返回 disposer。
        名须匹配 `[a-z][a-z0-9_]*`；重复注册抛错。提供器返回 None 时，引用
        该变量的节在渲染时报错。"""
        if not _VARIABLE_NAME.match(name):
            raise ValueError(f'无效 prompt 变量名 "{name}" (须匹配 {_VARIABLE_NAME.pattern})')
        if name in self._variables:
            raise ValueError(f"prompt 变量 {name!r} 已注册")
        self._variables[name] = provider
        return self._ctx.effect(
            lambda: (lambda: self._variables.pop(name, None)),
            f"prompt variable {name!r}")

    def suppress_runtime_context(self) -> Callable:
        """在当前作用域抑制全部动态上下文贡献（上游
        suppressRuntimeContext），返回 disposer。多个抑制器各自独立可撤销。"""
        self._runtime_context_suppressors.append(True)
        return self._ctx.effect(
            lambda: (lambda: self._runtime_context_suppressors.remove(True)),
            "prompt suppress runtime context")

    # ---------- 渲染面 ----------

    def render(self, context: dict) -> list[dict]:
        """按 order 升序（同 order 保持注册序）求值并返回非空节。
        @param context - 装配上下文（含 agent/session），传给可调用 text。
        @returns [{name, text}, ...]；空文本节被跳过（上游 renderPrompt 过滤语义）。
        """
        out = []
        for s in sorted(self._sections, key=lambda e: (e["order"], e["seq"])):
            text = s["text"](context) if callable(s["text"]) else s["text"]
            if text is None:
                continue
            if not isinstance(text, str):
                raise TypeError(f"prompt section {s['name']!r} 返回了非字符串")
            if text == "":
                continue
            out.append({"name": s["name"], "text": text})
        return out

    def assemble(self, context: dict | None = None) -> dict:
        """装配 sections / contexts / tools / variables，然后派发
        'system-prompt/assemble' waterfall（payload = {assembly, context}）。
        返回权威 assembly；但 complete 节恢复为唯一节、运行时上下文抑制后
        contexts 强制为空（上游同款恢复语义）。

        @param context - 装配上下文（含 agent/session，传给可调用提供器）。
        @returns PromptAssembly：{sections, contexts, tools, variables}。
        """
        context = context or {}
        suppressed = bool(self._runtime_context_suppressors)

        variables: dict[str, str | None] = {}
        for name, provider in self._variables.items():
            variables[name] = provider(context)

        collected: list[dict] = []
        known_names: set[str] = set()
        for provider in self._tool_providers:
            result = provider(context)
            schemas = [dict(s) for s in result.get("schemas", [])]
            collected.extend(schemas)
            for name in result.get("knownNames") or [t["name"] for t in schemas]:
                known_names.add(name)

        section_defs = sorted(self._sections, key=lambda e: (e["order"], e["seq"]))
        complete = [s for s in section_defs if s.get("complete")]
        if len(complete) > 1:
            names = ", ".join(s["name"] for s in complete)
            raise ValueError(f"多个 complete prompt section 生效: {names}")
        sections: list[dict] = []
        complete_section: dict | None = None
        for s in section_defs:
            text = s["text"](context) if callable(s["text"]) else s["text"]
            if not isinstance(text, str):
                raise TypeError(f"prompt section {s['name']!r} 返回了非字符串")
            assembled = {"name": s["name"], "text": text}
            if s.get("complete"):
                complete_section = dict(assembled)
            sections.append(assembled)

        contexts: list[dict] = []
        if not suppressed:
            for c in sorted(self._contexts, key=lambda e: (e["order"], e["seq"])):
                text = c["text"](context) if callable(c["text"]) else c["text"]
                if not isinstance(text, str):
                    raise TypeError(f"prompt context {c['name']!r} 返回了非字符串")
                contexts.append({"name": c["name"], "text": text})

        assembly: dict = {
            "sections": sections,
            "contexts": contexts,
            "tools": order_tools(collected, self._tool_order, known_names),
            "variables": variables,
        }
        transformed = self._ctx.waterfall("system-prompt/assemble",
                                          {"assembly": assembly, "context": context})
        result = transformed.get("assembly", assembly) if isinstance(transformed, dict) else assembly
        if complete_section is not None:
            result = {**result, "sections": [complete_section]}
        if suppressed:
            result = {**result, "contexts": []}
        return result


def install_system_prompt(ctx: Context, config: dict | None = None) -> SystemPromptService:
    """提供 ctx "systemPrompt" 服务（幂等：已存在则复用现有实例）。
    @param config - 可选 {toolOrder: [...]}（须含 rest 条目，非法即抛）。
    """
    service = ctx.get(SYSTEM_PROMPT_SERVICE)
    if service is None:
        service = SystemPromptService(ctx, config or {})
        ctx.provide(SYSTEM_PROMPT_SERVICE, service)
    return service