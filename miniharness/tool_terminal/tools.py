"""terminal 六工具 + `pty-send` 后台作业 + maxResultBytes 收口。

对齐 packages/terminal/tool-terminal/src/index.ts。契约要点：
  * 六工具正则名 terminal_open/send/read/signal/close/list；owner 身份取
    执行 `ToolExec.agent` 精确实例（无 agent 调用方逐字拒绝）。
  * terminal_open 走 `terminals.spawn(owner, {type, name?, cwd?}, signal)`
    （缺省 name/cwd 不传递）；terminal_send 后台模式经 `jobs.start` 开
    `kind='pty-send'` 作业（label `<id>: <text|(input)>`，outputLimitBytes=
    maxResultBytes）；readOutput 钩子消费 operation 增量并渲染
    `render_send_read`，结算 done 按 cancelRequested 出 completed/killed，
    失败 settle 为 failed 终态（上游 done.then 双回调的等价）。
  * 前台 send 等 operation 结算（同步载体用轮询桥接后端结算线程）后读
    `kind='foreground'` + 结算视图；exec 中止信号经协作取消驱动 operation.
    cancel 并结算后抛 'terminal send aborted'。
  * 全部结果（含错误）经 finalizeContent 按 boundTerminalText 二次收口：
    单 text 块才收，其余形状保持（对齐 rawContentText 语义）。

载体差异（登记录入 verified-diffs §3.31）：
  * 上游 `operation.done` 是 Promise（await 回落）；mini 同步结算契约在
    tool 层用轮询线程桥接（等待线程 sleep 10ms），对可结算后端语义等价。
  * 上游 tools/execute 中间件不存在于 mini 管线：finalizeContent 施加在
    schema 拒绝 / pre-execute 决策 / execute 异常 / post-execute block /
    成功五类既有 ToolResult 路径（核心差异见 verified-diffs）。
  * presentationMeta 是上游 Host/Web 卡片元数据投影，mini Tool 无该通道
    （ToolResult.meta 未被工具管线消费）；前台 canonical 值已完整携带。
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Any, Callable

from ..core.tools import Tool, ToolRegistry
from ..core.system_prompt import install_system_prompt
from ..jobs import JobDoneBox
from ..terminal.service import install_terminals
from .render import (
    bound_terminal_text,
    render_list,
    render_read,
    render_send,
    render_send_read,
    render_spawn,
)

__all__ = [
    "DEFAULT_MAX_RESULT_BYTES",
    "INJECT",
    "MIN_MAX_RESULT_BYTES",
    "PLUGIN_NAME",
    "TOOL_PTY_ORDER",
    "TOOL_PTY_SECTION",
    "apply",
    "install_tool_terminal",
    "register_terminal_tools",
    "resolve_config",
    "send_detail",
    "session_id",
    "require_agent",
]

#: 上游 plugin name（index.ts:25）。
PLUGIN_NAME = "tool-terminal"
#: 上游 inject（index.ts:27）。
INJECT = ("terminals", "tools", "systemPrompt")

#: 单条完整 terminal 结果的默认上限（index.ts:30）。
DEFAULT_MAX_RESULT_BYTES = 256 * 1024
#: 保留一切 counter-backed pty/job id 的最小上限（index.ts:32）。
MIN_MAX_RESULT_BYTES = 64

#: 引导节名与 order（上游 'tool:pty' + systemPrompt TOOL_PTY=1700；
#: mini 无 getSectionOrder，用字面量并登记简化）。
TOOL_PTY_SECTION = "tool:pty"
TOOL_PTY_ORDER = 1700

#: tool:pty 引导文案（index.ts:159，逐字）。
_TOOL_PTY_TEXT = (
    "Use a terminal session only when work needs persistent terminal state or interactive stdin; "
    "prefer shell/read/write/edit for bounded one-shot operations. Track every terminal session id "
    "and close sessions that no longer matter. An inferred_idle or timeout result does not prove the "
    "foreground command exited."
)

_WAIT_REASONS = ("stdin_read", "inferred_idle", "timeout", "session_exit")
_SIGNAL_ENUM = ("SIGINT", "SIGTERM", "SIGKILL", "SIGTSTP", "SIGHUP")


class _SignalAdapter:
    """ToolExec.signal（threading.Event / FusedSignal）→ terminal Cancellation 判读面。

    上游 terminal_send 把 exec.signal（AbortSignal）透传 startSession/startSend；
    mini 服务端 expects types.Cancellation 的 is_set/throw_if_aborted 面，此处按
    Event 置位状态轮询映射（工具执行时序无并发帧，语义等价）。
    """

    def __init__(self, signal: Any):
        self._signal = signal

    def is_set(self) -> bool:
        return _is_aborted(self._signal)

    def throw_if_aborted(self) -> None:
        if _is_aborted(self._signal):
            raise RuntimeError("cancelled")

#: 会话状态 oneOf（index.ts SESSION_STATUS_SCHEMA）。
SESSION_STATUS_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"kind": {"type": "string", "required": True, "const": "running"}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "kind": {"type": "string", "required": True, "const": "exited"},
                "exitCode": {"required": True, "oneOf": [{"type": "integer"}, {"type": "null"}]},
                "signal": {"required": True, "oneOf": [{"type": "string"}, {"type": "null"}]},
            },
        },
    ],
}

SESSION_SNAPSHOT_PROPERTIES = {
    "sessionId": {"type": "string", "required": True},
    "name": {"type": "string"},
    "type": {"type": "string", "required": True},
    "pid": {"type": "integer"},
    "status": {**SESSION_STATUS_SCHEMA, "required": True},
}

SESSION_SNAPSHOT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": SESSION_SNAPSHOT_PROPERTIES,
}

_FOREGROUND_SEND_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "required": True, "const": "foreground"},
        "viewport": {"type": "string", "required": True},
        "waitReason": {"type": "string", "required": True, "enum": list(_WAIT_REASONS)},
        "sessionStatus": {**SESSION_STATUS_SCHEMA, "required": True},
        "truncated": {"type": "boolean", "required": True},
    },
}

_BACKGROUND_JOB_OUTPUT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "kind": {"type": "string", "required": True, "const": "background"},
        "jobId": {"type": "string", "required": True},
    },
}


def resolve_config(config: dict | None) -> dict:
    """解析 tool-terminal 配置（index.ts Config 缺省 + min 校验 fail loud）。"""
    config = config or {}
    cfg = {
        "enableRunInBackground": config.get("enableRunInBackground", True),
        "maxResultBytes": config.get("maxResultBytes", DEFAULT_MAX_RESULT_BYTES),
    }
    max_result_bytes = cfg["maxResultBytes"]
    if not isinstance(max_result_bytes, int) or isinstance(max_result_bytes, bool) \
            or max_result_bytes < MIN_MAX_RESULT_BYTES:
        raise ValueError(
            f"tool-terminal: maxResultBytes must be a safe integer of at least {MIN_MAX_RESULT_BYTES}")
    return cfg


def require_agent(exec_: Any):
    """owner = 执行的精确实例；缺失逐字拒绝（index.ts:117-120）。"""
    agent = getattr(exec_, "agent", None)
    if agent is None:
        raise RuntimeError("terminal tools require an initiating agent")
    return agent


def session_id(args: dict) -> str:
    """sessionId 非空校验（index.ts:122-127）。"""
    sid = args["sessionId"]
    if len(sid) == 0:
        raise RuntimeError("sessionId must be a non-empty string")
    return sid


def send_detail(result: dict) -> str:
    """作业 detail：运行中 wait 原因 / 已退出归一说明（index.ts:139-143）。"""
    status = result["sessionStatus"]
    if status.get("kind") == "running":
        return f"wait: {result['waitReason']}"
    exit_code = status.get("exitCode")
    if exit_code is not None:
        return f"session exited: {exit_code}"
    signal = status.get("signal")
    if signal is not None:
        return f"session exited: {signal}"
    return "session exited: unknown"


def raw_content_text(content: Any) -> str | None:
    """规整单 text 块 → 文本；其余形状 None（index.ts:133-137）。"""
    if isinstance(content, (list, tuple)) and len(content) == 1:
        block = content[0]
        if isinstance(block, dict) and block.get("type") == "text":
            text = block.get("text")
            return text if isinstance(text, str) else None
    return None


def _is_aborted(signal: Any) -> bool:
    """两种 signal 形状统一判读（threading.Event.is_set / FusedSignal.aborted）。"""
    if signal is None:
        return False
    aborted = getattr(signal, "aborted", None)
    if aborted is not None:
        return bool(aborted)
    is_set = getattr(signal, "is_set", None)
    return bool(is_set()) if callable(is_set) else False


def _await_settle(operation, signal: Any):
    """等 operation 结算（轮询桥接同步结算契约）；中止信号协作取消后继续排干。

    等价上游 startSend(…, signal) 的 abort 接线：signal 置位即 operation.cancel()
    （触发 on_cancel → 前台 SIGINT），随后仍在后台等真正结算（'已启动的 promise
    排干到静止'）。返回 operation.done——失败时重抛（上游 done 拒绝）。
    """
    while not operation.settled:
        if _is_aborted(signal):
            if operation.cancel() is False:
                pass  # 已结算/已取消，继续等结算
        time.sleep(0.01)
    return operation.done


def bound_finalize(max_bytes: int) -> Callable[[Any, dict], list | None]:
    """finalizeContent：单 text 块按 boundTerminalText 二次收口（index.ts:129-155）。"""
    def _hook(_exec_: Any, result: dict) -> list | None:
        raw = raw_content_text(result["content"])
        if raw is None:
            return None
        return [{"type": "text", "text": bound_terminal_text(raw, max_bytes)}]
    return _hook


def _present_send_call(args: dict) -> dict:
    """terminal_send 挂起卡片（index.ts:282-290，locale 键面。"""
    if args.get("run_in_background") is True:
        return {"card": "generic", "title": f"Send to terminal {args['sessionId']} in background",
                "kind": "execute", "rawInput": args.get("text")}
    return {"card": "terminal", "title": args.get("text") or "(send input)",
            "description": f"Terminal {args['sessionId']}"}


def _present_send_result(_args: dict, result: dict):
    """terminal_send 完成卡片（index.ts:291-295）；仅前台成功单 text 块。"""
    if result.get("kind") == "background" or result.get("isError") or result.get("error"):
        return None
    raw = raw_content_text(result.get("content"))
    return None if raw is None else {"card": "terminal", "output": raw}


def _start_background_send(terminals, jobs, agent, session_id: str, request: dict,
                           max_bytes: int) -> str:
    """开一个 `pty-send` 后台作业并返回 jobId（index.ts:250-276）。

    run() 在 jobs 注册时同步执行：创建 operation、挂 box/取消/增量读三钩子，
    后台线程把 operation 结算桥接进 JobDoneBox（失败 settle 为 failed 终态）。
    """
    cancel_requested = False
    lock = threading.Lock()

    def run() -> dict:
        operation = terminals.start_send(agent, session_id, request)
        box = JobDoneBox()

        def cancel(_reason=None) -> None:
            nonlocal cancel_requested
            with lock:
                cancel_requested = True
            operation.cancel()

        def produce() -> None:
            try:
                result = _await_settle(operation, None)
            except BaseException as error:
                box.settle({"status": "failed", "detail": str(error)})
                return
            with lock:
                killed = cancel_requested
            box.settle({
                "status": "killed" if killed else "completed",
                "detail": send_detail(result),
            })

        threading.Thread(target=produce, daemon=True).start()
        return {
            "done": box,
            "cancel": cancel,
            "read_output": lambda: render_send_read(operation.read_output()),
        }

    return jobs.start({
        "kind": "pty-send",
        "label": f"{session_id}: {request['text'] or '(input)'}",
        "owner": agent,
        "outputLimitBytes": max_bytes,
        "run": run,
    })


# ---------- 六工具 ----------

def _terminal_open_tool(terminals, max_bytes: int) -> Tool:
    async def execute(args: dict, exec_: Any) -> dict:
        agent = require_agent(exec_)
        if len(args["type"]) == 0:
            raise RuntimeError("type must be a non-empty string")
        request: dict = {"type": args["type"]}
        if args.get("name") is not None:
            request["name"] = args["name"]
        if args.get("cwd") is not None:
            request["cwd"] = args["cwd"]
        signal = getattr(exec_, "signal", None)
        return terminals.spawn(agent, request,
                               _SignalAdapter(signal) if signal is not None else None)

    return Tool(
        name="terminal_open",
        description=(
            "Create a persistent, owner-isolated terminal session from a registered backend type. "
            "Use this for shell or REPL state that must survive across tool calls."
        ),
        parameters={
            "type": "object",
            "properties": {
                "type": {
                    "type": "string", "required": True,
                    "description": "Registered terminal backend type, usually \"shell\".",
                },
                "name": {
                    "type": "string",
                    "description": "Optional owner-local display name such as \"main\" or \"gdb\".",
                },
                "cwd": {
                    "type": "string",
                    "description": "Initial working directory. Defaults to the deployment workspace root.",
                },
            },
        },
        output={
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {**SESSION_SNAPSHOT_PROPERTIES,
                               "motd": {"type": "string", "required": True}},
            },
        },
        render=lambda _args, value: [{"type": "text", "text": render_spawn(value, max_bytes)}],
        execute=execute,
        finalize_content=bound_finalize(max_bytes),
        present_call=lambda args: {
            "card": "generic",
            "title": f"Open terminal {args.get('name') or args['type']}",
            "kind": "execute",
        },
    )


def _terminal_send_tool(terminals, jobs_getter: Callable[[], Any], config: dict) -> Tool:
    enable_run_in_background = config["enableRunInBackground"]
    max_bytes = config["maxResultBytes"]

    async def execute(args: dict, exec_: Any) -> dict:
        agent = require_agent(exec_)
        sid = session_id(args)
        request = {"text": args["text"], "submit": args.get("submit", True)}
        if args.get("run_in_background") is True:
            if not enable_run_in_background:
                raise RuntimeError("background terminal sends are disabled by tool-terminal configuration")
            if _is_aborted(getattr(exec_, "signal", None)):
                raise RuntimeError("terminal send aborted")
            jobs = jobs_getter()
            if jobs is None:
                raise RuntimeError(
                    "background terminal sends require @deepseek-ai/dsh-jobs and @deepseek-ai/dsh-tool-jobs")
            return {"kind": "background", "jobId": _start_background_send(
                terminals, jobs, agent, sid, request, max_bytes)}
        operation = terminals.start_send(agent, sid, request)
        result = await asyncio.to_thread(_await_settle, operation, getattr(exec_, "signal", None))
        if _is_aborted(getattr(exec_, "signal", None)):
            raise RuntimeError("terminal send aborted")
        return {"kind": "foreground", **result}

    parameters: dict = {
        "type": "object",
        "properties": {
            "sessionId": {
                "type": "string", "required": True,
                "description": "Terminal session id returned by terminal_open or terminal_list.",
            },
            "text": {"type": "string", "required": True, "description": "UTF-8 text to write to the terminal."},
            "submit": {
                "type": "boolean",
                "description": "Submit Enter after text (default true). Set false for control characters or incomplete REPL input.",
            },
        },
    }
    if enable_run_in_background:
        parameters["properties"]["run_in_background"] = {
            "type": "boolean",
            "description": "Return a job id immediately; collect with job_output or stop with job_kill.",
        }
    description = (
        "Send text to a persistent terminal. By default Enter is submitted and the call waits "
        "for a prompt, stdin wait, output silence, timeout, or session exit."
    )
    if enable_run_in_background:
        description += " Background mode returns a job id for job_output/job_kill."

    return Tool(
        name="terminal_send",
        description=description,
        parameters=parameters,
        output={
            "schema": {"oneOf": [_BACKGROUND_JOB_OUTPUT_SCHEMA, _FOREGROUND_SEND_SCHEMA]},
        },
        render=lambda _args, value: [{
            "type": "text",
            "text": f"started background job {value['jobId']}"
                    if value["kind"] == "background"
                    else render_send(value, max_bytes),
        }],
        execute=execute,
        finalize_content=bound_finalize(max_bytes),
        present_call=_present_send_call,
        present_result=_present_send_result,
    )


def _terminal_read_tool(terminals, max_bytes: int) -> Tool:
    def execute(args: dict, exec_: Any) -> dict:
        request: dict = {}
        if args.get("offset") is not None:
            request["offset"] = args["offset"]
        if args.get("count") is not None:
            request["count"] = args["count"]
        return terminals.read(require_agent(exec_), session_id(args), request)

    return Tool(
        name="terminal_read",
        description="Read a bounded page of retained output from a persistent terminal without sending input.",
        parameters={
            "type": "object",
            "properties": {
                "sessionId": {"type": "string", "required": True, "description": "Terminal session id."},
                "offset": {
                    "type": "number",
                    "description": "Newest-relative line offset (default 0).",
                },
                "count": {
                    "type": "number",
                    "description": "Requested line count (default 500; backend caps apply).",
                },
            },
        },
        output={
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "text": {"type": "string", "required": True},
                    "totalLines": {"type": "integer", "required": True},
                    "lineBegin": {"type": "integer", "required": True},
                    "lineEnd": {"type": "integer", "required": True},
                    "truncated": {"type": "boolean", "required": True},
                },
            },
        },
        render=lambda _args, value: [{"type": "text", "text": render_read(value, max_bytes)}],
        execute=execute,
        finalize_content=bound_finalize(max_bytes),
        present_call=lambda args: {
            "card": "generic", "title": f"Read terminal {args['sessionId']}",
            "kind": "read", "rawInput": args,
        },
    )


def _terminal_signal_tool(terminals, max_bytes: int) -> Tool:
    def execute(args: dict, exec_: Any) -> dict:
        return terminals.signal(require_agent(exec_), session_id(args), args["signal"])

    return Tool(
        name="terminal_signal",
        description=(
            "Send an allowed signal to the current foreground process group of a persistent terminal."
        ),
        parameters={
            "type": "object",
            "properties": {
                "sessionId": {"type": "string", "required": True, "description": "Terminal session id."},
                "signal": {
                    "type": "string", "required": True, "enum": list(_SIGNAL_ENUM),
                    "description": "Signal to deliver. Shell-targeted SIGKILL is rejected; use terminal_close.",
                },
            },
        },
        output={
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "delivered": {"type": "boolean", "required": True, "const": True},
                    "targetPgid": {"type": "integer", "required": True},
                },
            },
        },
        render=lambda args, value: [{
            "type": "text",
            "text": f"delivered {args['signal']} to foreground process group {value['targetPgid']}",
        }],
        execute=execute,
        finalize_content=bound_finalize(max_bytes),
        present_call=lambda args: {
            "card": "generic", "title": f"Signal terminal {args['sessionId']}",
            "kind": "execute", "rawInput": args,
        },
    )


def _terminal_close_tool(terminals, max_bytes: int) -> Tool:
    async def execute(args: dict, exec_: Any) -> dict:
        sid = session_id(args)
        closed = terminals.kill(require_agent(exec_), sid)
        return {"sessionId": sid, "outcome": "closed" if closed else "already-closing"}

    return Tool(
        name="terminal_close",
        description="Close one persistent terminal and wait until its captured owned process tree is gone.",
        parameters={
            "type": "object",
            "properties": {
                "sessionId": {"type": "string", "required": True, "description": "Terminal session id."},
            },
        },
        output={
            "schema": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "sessionId": {"type": "string", "required": True},
                    "outcome": {"type": "string", "required": True,
                                "enum": ["closed", "already-closing"]},
                },
            },
        },
        render=lambda _args, value: [{
            "type": "text",
            "text": f"closed terminal session {value['sessionId']}"
                    if value["outcome"] == "closed"
                    else f"terminal session {value['sessionId']} was already closing",
        }],
        execute=execute,
        finalize_content=bound_finalize(max_bytes),
        present_call=lambda args: {
            "card": "generic", "title": f"Close terminal {args['sessionId']}", "kind": "delete",
        },
    )


def _terminal_list_tool(terminals, max_bytes: int) -> Tool:
    def execute(_args: dict, exec_: Any) -> list[dict]:
        return terminals.list(require_agent(exec_))

    return Tool(
        name="terminal_list",
        description="List persistent terminal sessions owned by the current agent.",
        parameters={"type": "object", "properties": {}},
        output={"schema": {"type": "array", "items": SESSION_SNAPSHOT_SCHEMA}},
        render=lambda _args, value: [{"type": "text", "text": render_list(value, max_bytes)}],
        execute=execute,
        finalize_content=bound_finalize(max_bytes),
        present_call=lambda _args: {
            "card": "generic", "title": "List terminal sessions", "kind": "read",
        },
    )


# ---------- 装配面 ----------

def register_terminal_tools(tool_registry, terminals, jobs_getter: Callable[[], Any],
                            config: dict | None = None) -> None:
    """注册六工具（对齐上游 tool-terminal apply 的工具注册面）。"""
    cfg = resolve_config(config)
    tool_registry.register(_terminal_open_tool(terminals, cfg["maxResultBytes"]))
    tool_registry.register(_terminal_send_tool(terminals, jobs_getter, cfg))
    tool_registry.register(_terminal_read_tool(terminals, cfg["maxResultBytes"]))
    tool_registry.register(_terminal_signal_tool(terminals, cfg["maxResultBytes"]))
    tool_registry.register(_terminal_close_tool(terminals, cfg["maxResultBytes"]))
    tool_registry.register(_terminal_list_tool(terminals, cfg["maxResultBytes"]))


def install_tool_terminal(ctx, config: dict | None = None) -> Any:
    """组装面装配（对齐 install_compaction 惯例，幂等）：缺服务即建，重复安装跳过。

    返回 terminals 服务（镜像 install_terminal_bash 的返回值习惯）。
    """
    cfg = resolve_config(config)
    terminals = install_terminals(ctx)
    if getattr(ctx, "_miniharness_tool_terminal_installed", False):
        return terminals
    system_prompt = install_system_prompt(ctx)
    system_prompt.section(TOOL_PTY_SECTION, TOOL_PTY_ORDER, _TOOL_PTY_TEXT)
    registry = ctx.get("tools")
    if registry is None:
        registry = ToolRegistry(ctx)
    if getattr(registry, "_miniharness_tool_terminal_registered", None) is None:
        register_terminal_tools(registry, terminals, lambda: ctx.get("jobs"), cfg)
        registry._miniharness_tool_terminal_registered = True
    ctx._miniharness_tool_terminal_installed = True
    return terminals


def apply(ctx, config: dict | None = None) -> None:
    """对齐上游 `apply(ctx, config)` 形状（tool-terminal 插件面）。"""
    install_tool_terminal(ctx, config)