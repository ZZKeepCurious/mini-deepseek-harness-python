"""system prompt 分节装配：有序 prompt section 注册与渲染。

上游对照：packages/core/system-prompt/src/index.ts（SystemPrompt.section /
assemble / renderPrompt）。mini 保留最小子集：分节注册 + 按 order 升序渲染 +
空文本跳过；上游的 system-prompt/assemble waterfall、contexts、tools 提供器、
variables 插值、scope 层叠均未复现（后置余量，见 tasks.md 待办池）。

装配：install_system_prompt(ctx) 提供 ctx "systemPrompt" 服务（幂等）。
AgentLoop._derive_history 在渲染时经 ctx.inject("systemPrompt") 懒取；
无该服务时仅用 AgentLoop.system_prompt 字符串（既有行为不变）。
"""
from __future__ import annotations

from typing import Any, Callable

from .scope import Context

__all__ = ["SYSTEM_PROMPT_SERVICE", "SystemPromptService", "install_system_prompt"]

SYSTEM_PROMPT_SERVICE = "systemPrompt"


class SystemPromptService:
    """按序收集 prompt section，供每次模型请求装配 system 消息。

    每个 section 携带 name / order / text（字符串或按装配上下文求值的
    可调用对象）；同名重复注册抛错（上游 NamedEntries 同语义）。
    """

    def __init__(self, ctx: Context):
        self._ctx = ctx
        self._sections: list[dict[str, Any]] = []
        self._seq = 0

    def section(self, name: str, order: int, text: str | Callable[[dict], str]) -> Callable:
        """注册一个有序 prompt section，返回其 disposer。

        @param name - 唯一节名（重复注册抛 ValueError）。
        @param order - 升序连接位置（上游约定：-100 身份、0 人设、plan 用 50）。
        @param text - 静态文本或按装配上下文（含 agent）求值的可调用对象。
        """
        if not isinstance(name, str) or name == "":
            raise ValueError("prompt section 需要一个非空 name")
        if not isinstance(order, int):
            raise ValueError(f"prompt section {name!r} 的 order 必须是整数")
        if not (callable(text) or isinstance(text, str)):
            raise TypeError(f"prompt section {name!r} 的 text 必须是字符串或可调用对象")
        if any(s["name"] == name for s in self._sections):
            raise ValueError(f"prompt section {name!r} 已注册")
        entry = {"name": name, "order": order, "text": text, "seq": self._seq}
        self._seq += 1
        self._sections.append(entry)

        def disposer() -> None:
            if entry in self._sections:
                self._sections.remove(entry)

        return self._ctx.effect(disposer)

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


def install_system_prompt(ctx: Context) -> SystemPromptService:
    """提供 ctx "systemPrompt" 服务（幂等：已存在则复用现有实例）。"""
    try:
        return ctx.inject(SYSTEM_PROMPT_SERVICE)
    except KeyError:
        service = SystemPromptService(ctx)
        ctx.provide(SYSTEM_PROMPT_SERVICE, service)
        return service
