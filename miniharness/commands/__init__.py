"""轻量斜杠命令服务：/plan、/goal 等命令的解析、注册与生命周期事件。

上游对照：packages/interaction/commands/src/index.ts（CommandRuntime）+ types.ts
（command/run、command/done 事件，commandId 配对；args 是 parseCommand 的 verbatim
rawInput，分隔空白在内）。

契约（与上游一致）：
  * `command/run` {commandId, name, args?, source:{kind:'user'}} 进入已注册命令的
    handler 时落日志；`command/done` {commandId, kind, text?, sourceEventSeq?}
    配对收尾。两者都是 log-only 非 surface 事件，按 commandId 配对
    （上游 types.ts:76-101）。recordInput=false 的命令不落 args（领域事件自己
    承载载荷，上游 index.ts:311）。
  * 未命中已注册命令的斜杠行不是命令：不进 handler、不落事件，保持普通文本。
  * handler 抛出异常 → 结算为 kind:'error' + 渲染失败（上游同语义）；非法返回
    值在注册表边界 fail-loud（normalizeResult，上游 index.ts:192-218）。
  * commandId 形如 `cmd-<8位uuid前缀>-<单调序号>`（上游 mintCommandId：instance
    token 前缀 + 每实例递增计数，resume 日志不重复）。
  * 命令名解析不做大小写转换：上游 parseCommand 正则 `/^\/([a-z][a-z0-9_-]*)
    (?=$|[\t\n\r ])/`，首字符必须小写字母（index.ts:103）。
  * 注册即 notify：commands/change 事件（上游 notifyChange，非 vetoing，各自
    回调独立 contain）。

教学扩展（有意保留，须在文档标注）：上游命令由人类 UI 表面（web/CLI）派发，
handler 收到 CommandInvocation {commandId, agent, rawInput, signal}；mini 无交互
式 UI，handler 签名保持 `(agent, raw)`（plan/goal 已按此签名注册），命令契约由
REPL 示例（examples/plan_goal_demo.py）演示。headless / ACP / SDK 是自动化表面，
不路由命令（对齐上游——命令只属人类 UI）。无 AbortSignal（signal 取消语义未复现，
与 asyncio 化简化清单一致）。
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

#: 注册命令名的合法形状（上游 index.ts:25 COMMAND_NAME）。
_COMMAND_NAME = re.compile(r"^[a-z][a-z0-9_-]*$")

#: 命令行的解析模式（上游 parseCommand，首字符必须小写字母、后面可含数字/下划线/
#: 连字符，随后必须是行尾或空白；rawInput 保留分隔空白在内，verbatim）。
_COMMAND_LINE = re.compile(r"^/([a-z][a-z0-9_-]*)(?=$|[\t\n\r ])")

#: instance token：每实例前缀，使跨进程 resume 的 commandId 不重复（上游
#: mintCommandId，uuid 前 8 字符）。
_instance_token = uuid.uuid4().hex[:8]


def parse_command(text: str) -> tuple[str, str] | None:
    """把一行用户输入解析成 (name, rawArgs)；非命令行返回 None。

    对齐上游 parseCommand：名字不做大小写转换（大写/数字开头的斜杠行不匹配），
    rawArgs 是去除名字后的逐字剩余（分隔空白在内）。
    """
    if not isinstance(text, str):
        return None
    match = _COMMAND_LINE.match(text)
    if match is None:
        return None
    return match.group(1), text[match.end():]


class CommandRegistry:
    """命令注册表：register/dispatch，事件按 commandId 配对（上游 CommandRuntime）。"""

    def __init__(self, ctx: Context):
        self._ctx = ctx
        self._commands: dict[str, dict[str, Any]] = {}
        self._command_seq = 0

    def register(self, name: str, description: str, handler: Callable,
                 input_hint: str | None = None, record_input: bool = True) -> Callable:
        """注册命令；返回 disposer。元数据非法 / 重复注册 = fail loud（上游
        normalizeDefinition 同款校验，index.ts:151-189）。"""
        self._ctx._assert_alive()
        if not _COMMAND_NAME.match(name):
            raise TypeError(
                f'command name "{name}" must match {_COMMAND_NAME.pattern}'
            )
        if not isinstance(description, str) or description.strip() == "":
            raise TypeError(f'command "/{name}" description must be a non-empty string')
        if not callable(handler):
            raise TypeError(f'command "/{name}" handler must be a function')
        if input_hint is not None and (not isinstance(input_hint, str) or input_hint.strip() == ""):
            raise TypeError(f'command "/{name}" input hint must be a non-empty string')
        if not isinstance(record_input, bool):
            raise TypeError(f'command "/{name}" recordInput must be a boolean')
        if name in self._commands:
            raise RuntimeError(f"command /{name} is already registered")
        self._commands[name] = {
            "description": description,
            "handler": handler,
            "input_hint": input_hint,
            "record_input": record_input,
        }
        self._notify_change()

        def dispose() -> None:
            removed = self._commands.pop(name, None)
            if removed is not None:
                self._notify_change()

        return dispose

    def names(self) -> list[str]:
        return sorted(self._commands)

    def _mint_command_id(self) -> str:
        self._command_seq += 1
        return f"cmd-{_instance_token}-{self._command_seq}"

    def _notify_change(self) -> None:
        """commands/change 通知（上游 notifyChange：非 vetoing，各自 contain）。"""
        try:
            self._ctx.emit("commands/change")
        except Exception:  # noqa: BLE001 - 通知失败不影响注册（上游各自 contain）
            pass

    def dispatch(self, agent: Any, text: str) -> dict | None:
        """执行一行命令输入；非命令行/未知命令返回 None。

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
        command_id = self._mint_command_id()
        run_data: dict[str, Any] = {
            "commandId": command_id,
            "name": name,
            "source": {"kind": "user"},
        }
        if entry["record_input"]:
            run_data["args"] = raw
        session.append("command/run", run_data)
        try:
            output = entry["handler"](agent, raw)
        except Exception as error:  # noqa: BLE001 - 抛错的 handler 结算为 error（上游同语义）
            session.append("command/done", {
                "commandId": command_id, "kind": "error",
                "text": str(error),
            })
            return {"kind": "error", "text": str(error)}
        result = _normalize_result(name, output)
        done_data: dict[str, Any] = {"commandId": command_id, "kind": result["kind"]}
        if "text" in result:
            done_data["text"] = result["text"]
        if result["kind"] == "success" and result.get("sourceEventSeq") is not None:
            done_data["sourceEventSeq"] = result["sourceEventSeq"]
        session.append("command/done", done_data)
        return dict(result)


def _normalize_result(command: str, value: Any) -> dict:
    """注册表边界的返回校验：非法返回 fail loud（上游 normalizeResult
    index.ts:192-218）。success 文本/源 seq 可选；error 文本必须非空。"""
    if not isinstance(value, dict) or "kind" not in value:
        raise TypeError(f'command "/{command}" handler must return a CommandResult')
    kind = value.get("kind")
    if kind == "success":
        text = value.get("text")
        if text is not None and not isinstance(text, str):
            raise TypeError(f'command "/{command}" success text must be a string when supplied')
        seq = value.get("sourceEventSeq")
        if seq is not None and (not isinstance(seq, int) or isinstance(seq, bool) or seq < 0):
            raise TypeError(
                f'command "/{command}" success sourceEventSeq must be a '
                "non-negative safe integer when supplied"
            )
        result: dict[str, Any] = {"kind": "success"}
        if text is not None:
            result["text"] = text
        if seq is not None:
            result["sourceEventSeq"] = seq
        return result
    if kind == "error":
        text = value.get("text")
        if not isinstance(text, str) or text.strip() == "":
            raise TypeError(f'command "/{command}" error text must be a non-empty string')
        return {"kind": "error", "text": text}
    raise TypeError(f'command "/{command}" returned unknown result kind "{kind}"')


def install_commands(ctx: Context) -> CommandRegistry:
    """提供 ctx 服务 `commands`（可选服务：plan/goal 经 inject 鸭子类型注册）。"""
    registry = CommandRegistry(ctx)
    ctx.provide("commands", registry)
    return registry


def route_command(text: str, agent: Any, ctx: Context) -> str | None:
    """表面便捷入口：命中已注册命令返回 handler 文本，否则返回 None。

    无 commands 服务时返回 None（命令不可用即普通文本）。
    """
    commands = ctx.get("commands")
    if commands is None:
        return None
    result = commands.dispatch(agent, text)
    if result is None:
        return None
    return result["text"]
