"""PTC 模式 `run_code` 传输：模型侧工具 + 子派发事件日志。

PTC = programmatic tool calls（程序化工具调用）：模型不逐一调用工具，而是写一段程序，
在程序内通过宿主机提供的异步绑定（如 ``await tools.add({...})``）完成多步操作；
`run_code` 是该模式的模型侧入口，模型交付一段程序（语言取
runtime.language flavor，typescript/python），程序内调用绑定枚举的 agent 可见工具（上游更名记录：
packages/.agents/notes/archived/architecture/2026-08-25-rename-code-mode-to-ptc.md）。

对应 dsh 真实源码：packages/core/tools/src/ptc.ts（`createRunCodeTool`）。

程序经 `tools` 绑定命名空间调用注册表里 agent 可见的工具（嵌套执行）；每次
子派发记录 `tool/ptc-dispatch-start` / `tool/ptc-dispatch` 事件（可重建），
但只有外层精心挑选的结果进入模型历史。

mini 载体差异：上游用 registry 的分阶段调度接口（prepare/dispatch/finalize/
finish）+ 并发池；mini 的工具管线是同步/简单载体，故子派发经 `run_pipeline`
直接执行（顺序、单飞），事件序与 payload 形状对齐，并发上限简化登记。

只有一层：run_code 不绑定自身（`RUN_CODE_NAME` 从可见集剔除）。
"""
from __future__ import annotations

import json
from typing import Any

from ..core.session import Session
from ..core.tools import Tool, ToolExec, ToolRegistry, ToolResult, run_pipeline

__all__ = [
    "RUN_CODE_DESCRIPTION_PARAM_DESCRIPTION",
    "RUN_CODE_NAME",
    "RunCodeFailedError",
    "TYPESCRIPT_FLAVOR",
    "PYTHON_FLAVOR",
    "RUN_CODE_FLAVORS",
    "create_run_code_tool",
]

#: PTC 模式工具的模型可见名。
RUN_CODE_NAME = "run_code"

RUN_CODE_DESCRIPTION_PARAM_DESCRIPTION = (
    "Clear, concise description of what this program does in active voice, "
    "5-10 words (shown in the UI). Examples: \"Count TODO markers across packages\"; "
    "\"Read failing test and its fixture\"; \"Rename config key in every cordis.yml\"."
)

TYPESCRIPT_FLAVOR = {
    "description": (
        "Execute a TypeScript program against the available tools. Takes two required "
        "arguments: `code`, the BODY of an async function (erasable syntax only; top-level "
        "`await` and `return` work), and `description`, a short summary of what the program "
        "does. Call tools as `await tools.name(args)` per the declarations in the system "
        "prompt. Only what you print or return is program output — curate it."),
    "codeDescription": "The program: the body of an async TypeScript function.",
}
PYTHON_FLAVOR = {
    "description": (
        "Execute a Python program against the available tools. Takes two required "
        "arguments: `code`, the BODY of an async function (top-level `await` and `return` "
        "work), and `description`, a short summary of what the program does. Call tools as "
        "`await tools.name(args)` per the declarations in the system prompt. Use "
        "`print(...)` and/or `return <value>` for program output — curate it."),
    "codeDescription": "The program: the body of an async Python function.",
}

#: 每个语言一套 run_code schema 文案。
RUN_CODE_FLAVORS: dict[str, dict] = {
    "typescript": TYPESCRIPT_FLAVOR,
    "python": PYTHON_FLAVOR,
}


class RunCodeFailedError(RuntimeError):
    """程序运行本身失败（异常/预算到期/中止/基底死亡）。

    对齐上游 `CodeRunFailedError`（code `CODE_RUN_FAILED`）；执行管线把它折算为
    结构化 isError 结果，文本携带失败 kind + 捕获日志，使模型可自我纠正。
    """

    code = "CODE_RUN_FAILED"

    def __init__(self, message: str):
        super().__init__(message)
        self.name = "RunCodeFailedError"


def _resolve_flavor(runtime):
    language = runtime.language if runtime is not None else "typescript"
    flavor = RUN_CODE_FLAVORS.get(language)
    if flavor is None:
        known = ", ".join(json.dumps(name) for name in RUN_CODE_FLAVORS)
        raise ValueError(
            f"dsh-tools: no run_code schema flavor registered for runtime language "
            f"{json.dumps(language)} (known: {known})")
    return flavor


def _render_value(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, indent=2, ensure_ascii=False)


def create_run_code_tool(registry: ToolRegistry, *, runtime, ctx=None,
                         session: Session | None = None) -> Tool:
    """构建 `run_code` 工具（上游 createRunCodeTool 的 mini 载体）。

    @param registry - 拥有注册表（子派发经其解析工具）。
    @param runtime - 已挂载的 PtcRuntime（决定语言 flavor 与执行）。
    @param ctx - 装配上下文（子派发管线用）。
    @param session - 目标会话；子派发事件写它的日志。
    """
    flavor = _resolve_flavor(runtime)

    def _visible_names() -> list[str]:
        return [name for name in registry.names() if name != RUN_CODE_NAME]

    def execute(args: dict, exec_: ToolExec):
        description = args.get("description", "")
        if not isinstance(description, str) or description.strip() == "":
            raise ValueError("invalid description: expected a non-empty string")
        code = args.get("code", "")
        timeout_ms = args.get("timeoutMs")
        if timeout_ms is not None and (not isinstance(timeout_ms, (int, float))
                                       or isinstance(timeout_ms, bool) or timeout_ms <= 0):
            raise ValueError("invalid timeoutMs: expected a positive finite number")

        agent = exec_.agent
        agent_session = session if session is not None else (
            getattr(agent, "session", None) if agent is not None else None)
        parent_call_id = getattr(exec_, "call_id", None) or ""
        root_call_id = getattr(exec_, "root_call_id", None) or parent_call_id
        state = {"dispatch": 0}

        def binding(name: str):
            def call(*call_args):
                # 程序按 `await tools.name(args)` 调用：单个位置参数即工具参数对象
                # （上游 binding 收 rawArgs 对象）。多位置参数时按列表传递。
                sub_args = call_args[0] if len(call_args) == 1 else list(call_args)
                state["dispatch"] += 1
                sub_call_id = f"{parent_call_id}:ptc:{state['dispatch']}"
                if agent_session is not None:
                    agent_session.append("tool/ptc-dispatch-start", {
                        "rootCallId": root_call_id,
                        "parentCallId": parent_call_id,
                        "subCallId": sub_call_id,
                        "name": name,
                        "arguments": sub_args,
                    })
                tool = registry.resolve(name, getattr(agent, "ctx", None) if agent else None)
                if tool is None:
                    raise RuntimeError(f"run_code: tool {name!r} is not available to this agent")
                sub_exec = ToolExec(agent=agent, parent=exec_, name=name, arguments=sub_args)
                result = run_pipeline(ctx, tool, dict(sub_args), sub_exec)
                # 嵌套派发的 post-execute additionalContexts（如 spill-policy 把被
                # 省略整图的预览重注为 ptc-mode user 消息）只在成功结果上转发给外层
                # run_code 调用（对齐上游 result.additionalContexts 语义）。
                if not result.is_error and sub_exec.additional_contexts:
                    exec_.additional_contexts.extend(sub_exec.additional_contexts)
                    sub_exec.additional_contexts.clear()
                if agent_session is not None:
                    agent_session.append("tool/ptc-dispatch", {
                        "rootCallId": root_call_id,
                        "parentCallId": parent_call_id,
                        "subCallId": sub_call_id,
                        "name": name,
                        "arguments": sub_args,
                        "isError": bool(result.is_error),
                        **({} if result.error_info is None else {"error": result.error_info}),
                        "content": result.content if result.content is not None else [],
                    })
                if result.is_error:
                    raise RuntimeError(result.error or f"tool {name} failed")
                return result.value if result.value is not None else result.content
            return call

        functions = {name: binding(name) for name in _visible_names()}
        from ..ptc_runtime.types import PtcBindingNamespace

        bindings = [PtcBindingNamespace("tools", functions)]
        cwd = None
        if agent_session is not None:
            cwd = agent_session.meta.get("cwd")
        spec = runtime.resolve(_request(code=code, bindings=bindings, cwd=cwd,
                                        timeout_ms=timeout_ms,
                                        signal=getattr(exec_, "signal", None)))
        import asyncio

        result = asyncio.run(runtime.run(spec)) if not _in_event_loop() else _run_in_new_loop(runtime, spec)
        if result.error is not None:
            logs_text = ("\nCaptured output:\n" + "\n".join(result.logs)) if result.logs else ""
            raise RunCodeFailedError(
                f"code run failed ({result.error.kind}): {result.error.message}{logs_text}")
        output: dict = {"logs": result.logs}
        if result.has_value:
            output["result"] = result.value
        if result.sandbox is not None:
            output["sandbox"] = {
                "mode": result.sandbox.mode, "denied": result.sandbox.denied,
                **({} if result.sandbox.enforcement is None
                   else {"enforcement": result.sandbox.enforcement}),
            }
        return output

    def _request(*, code, bindings, cwd, timeout_ms, signal):
        from ..ptc_runtime.types import PtcRunRequest

        return PtcRunRequest(
            program=code, bindings=bindings,
            **({} if cwd is None else {"cwd": cwd}),
            **({} if timeout_ms is None else {"timeoutMs": int(timeout_ms)}),
            signal=signal)

    def render(args: dict, value: dict) -> list[dict]:
        rendered = "" if "result" not in value else _render_value(value["result"])
        parts = [part for part in ["\n".join(value.get("logs") or []), rendered] if part]
        if value.get("sandbox", {}).get("denied"):
            parts.append(f"The {value['sandbox']['mode']} file sandbox denied an operation.")
        return [{"type": "text",
                 "text": "\n".join(parts) if parts else "(run_code completed with no output)"}]

    def present_call(args: dict) -> dict:
        return {"card": "generic", "title": args.get("description", ""),
                "kind": "execute", "rawInput": args.get("code", "")}

    return Tool(
        name=RUN_CODE_NAME,
        description=flavor["description"],
        parameters={
            "code": {"type": "string", "description": flavor["codeDescription"]},
            "description": {"type": "string",
                            "description": RUN_CODE_DESCRIPTION_PARAM_DESCRIPTION},
        },
        output={"schema": {"type": "object"}},
        execute=execute,
        render=render,
        present_call=present_call,
    )


def _in_event_loop() -> bool:
    import asyncio

    try:
        asyncio.get_running_loop()
        return True
    except RuntimeError:
        return False


def _run_in_new_loop(runtime, spec):
    """在独立线程里跑新事件循环（execute 在事件循环内被调用时避免嵌套 asyncio.run）。"""
    import asyncio
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(lambda: asyncio.run(runtime.run(spec))).result()
