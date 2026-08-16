"""轻量斜杠命令服务：/plan、/goal 等命令的解析、注册与生命周期事件。

上游对照：packages/interaction/commands/src/index.ts（CommandRegistry）+ types.ts
（command/run、command/done 事件，commandId 配对；args 是 parseCommand 的 verbatim
rawInput，分隔空白在内）。

契约（与上游一致）：
  * `command/run` {commandId, name, args, source:{kind:'user'}} 进入已注册命令的
    handler 时落日志；`command/done` {commandId, kind, text} 配对收尾。两者都是
    log-only 非 surface 事件，按 commandId 配对（上游 types.ts:76-101）。
  * 未命中已注册命令的斜杠行不是命令：不进 handler、不落事件，保持普通文本。
  * handler 抛出异常 → 结算为 kind:'error' + 渲染失败（上游同语义）。

教学扩展：上游命令由人类 UI 表面（web/CLI）派发；mini 无交互式 UI，本服务提供
命令契约与处理器，由 REPL 示例（examples/plan_goal_demo.py）演示。headless /
ACP / SDK 是自动化表面，不路由命令（对齐上游——命令只属人类 UI）。简化标注：
无 commands/change 通知、command/done 不携带 sourceEventSeq（上游可由领域事件
提供更富呈现，mini 直接渲染 handler 文本）、命令名按小写匹配。
"""
from __future__ import annotations

import re
import uuid
from typing import Any, Callable

from ..core.scope import Context
from ..core.session import Session

__all__ = [
    "CommandRegistry",
    "install_commands",
    "parse_command",
    "route_command",
]

#: 命令名的匹配模式：`/名字[分隔空白+剩余]`；名字小写（上游 CommandDescriptor.name）。
_NAME_PATTERN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9-]*)")


def parse_command(text: str) -> tuple[str, str] | None:
    """把一行用户输入解析成 (name, rawArgs)；非命令行返回 None。

    name 是首 token（去前导 `/`、转小写）；rawArgs 是去除名字后的逐字剩余
    （分隔空白在内，对齐上游 parseCommand 的 verbatim rawInput）。
    """
    if not isinstance(text, str) or not text.startswith("/"):
        return None
    rest = text[1:]
    match = _NAME_PATTERN.match(rest)
    if match is None:
        return None
    name = match.group(1).lower()
    return name, rest[match.end():]


class CommandRegistry:
    """命令注册表：register/dispatch，事件按 commandId 配对（上游 CommandRegistry）。"""

    def __init__(self, ctx: Context):
        self._ctx = ctx
        self._commands: dict[str, dict[str, Any]] = {}

    def register(self, name: str, description: str, handler: Callable,
                 input_hint: str | None = None) -> Callable:
        """注册命令；返回 disposer。重复注册同名单 = 冲突（fail loud）。"""
        self._ctx._assert_alive()
        if name in self._commands:
            raise RuntimeError(f"命令 /{name} 已注册")
        self._commands[name] = {
            "description": description,
            "handler": handler,
            "input_hint": input_hint,
        }
        return lambda: self._commands.pop(name, None)

    def names(self) -> list[str]:
        return sorted(self._commands)

    def dispatch(self, agent: Any, text: str) -> dict | None:
        """执行一行命令输入；非命令行返回 None。

        命令进入 handler 前先落 `command/run`（durable before dispatch，对齐上游），
        handler 结算后落 `command/done` 配对。返回归一化的 {kind, text}。
        """
        parsed = parse_command(text)
        if parsed is None:
            return None
        name, raw = parsed
        entry = self._commands.get(name)
        if entry is None:
            return None
        session: Session = agent.session
        command_id = f"command-{uuid.uuid4()}"
        session.append("command/run", {
            "commandId": command_id,
            "name": name,
            "args": raw,
            "source": {"kind": "user"},
        })
        try:
            result = entry["handler"](agent, raw)
        except Exception as error:  # noqa: BLE001 - 抛错的 handler 结算为 error（上游同语义）
            result = {"kind": "error", "text": str(error)}
        if not isinstance(result, dict):
            result = {"kind": "success", "text": str(result)}
        kind = result.get("kind")
        if kind not in ("success", "error"):
            kind = "error"
        text = result.get("text")
        text = str(text) if text is not None else ""
        session.append("command/done", {"commandId": command_id, "kind": kind, "text": text})
        return {"kind": kind, "text": text}


def install_commands(ctx: Context) -> CommandRegistry:
    """提供 ctx 服务 `commands`（可选服务：plan/goal 经 inject 鸭子类型注册）。"""
    registry = CommandRegistry(ctx)
    ctx.provide("commands", registry)
    return registry


def route_command(text: str, agent: Any, ctx: Context) -> str | None:
    """表面便捷入口：命中已注册命令返回 handler 文本，否则返回 None。

    无 commands 服务时返回 None（命令不可用即普通文本）。
    """
    try:
        commands = ctx.inject("commands")
    except KeyError:
        return None
    result = commands.dispatch(agent, text)
    if result is None:
        return None
    return result["text"]
