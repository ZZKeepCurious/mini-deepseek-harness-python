"""阶段 7：工具调用调度器（真并行，对齐 agent-loop/src/tool-calls.ts）。

调度语义（与上游逐条一致）：
  * 按模型序消费 tool-call：exclusive 调用形成单元素屏障，parallel 调用
    进入有界滚动池（max_parallel 上限），池满则等排空再补充
  * pre-execute（政策段）按模型序有序 await（startCall 顺序执行）；
    只有 execute 体在线程池真并行（"dispatch 重叠"）
  * 结果按模型序提交（commitReady 只推进连续槽位）
  * 并行组内后序调用被重新分类为 exclusive → 立即停止补池，
    等当前池排空，留作下一个屏障（注册表运行期变化可形成屏障）
  * abort：停止启动新调用，排干已启动的，未启动的按模型序补
    TOOL_ABORTED_BEFORE_DISPATCH 合成错误结果（先 tool/call 再 tool/result）
  * 调度器内部失败：停止新派发，排干已启动，抛第一个错误，不编造结果
"""
from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Any, Callable

from ..scope import Context
from ..session import Session, create_message, deep_freeze, text_block, tool_result_block
from ..tools import (
    ToolExec,
    ToolResult,
    execution_mode,
    pipeline_async_body,
    pipeline_policy_async,
)

# 对齐上游常量值（tools/src/index.ts:472）：TOOL_ABORTED_BEFORE_DISPATCH = 'ABORTED_BEFORE_DISPATCH'
TOOL_ABORTED_BEFORE_DISPATCH = "ABORTED_BEFORE_DISPATCH"
DEFAULT_MAX_PARALLEL_TOOL_CALLS = 10


@dataclass
class ParallelBarrier:
    """并行组的排干等待：满则停补，等全部在飞任务静默后再放行。

    对齐上游 runGroup 的滚动池语义；max_parallel=1 时即纯串行。
    """

    max_parallel: int = DEFAULT_MAX_PARALLEL_TOOL_CALLS


def append_tool_call(session: Session, turn: int, step: int, call_id: str,
                     name: str, arguments: str) -> int:
    """先落 tool/call，返回事件 seq 供 tool/result 引用（durable before dispatch）。"""
    ev = session.append("tool/call", {
        "turn": turn, "step": step, "callId": call_id, "name": name,
        "arguments": arguments,
    })
    return ev["seq"]


def _aborted_result() -> ToolResult:
    """未派发调用在 abort 时的合成错误结果（对齐 appendSkippedToolCall）。"""
    return ToolResult(ok=False, is_error=True, error="tool call aborted before dispatch",
                      _aborted=True,
                      error_info={"name": "AbortError", "code": TOOL_ABORTED_BEFORE_DISPATCH})


def emit_tool_result(session: Session, turn: int, step: int, call_id: str,
                     result: ToolResult, call_seq: int) -> None:
    """按模型序落 tool/result（surfaceOp append + sourceEventSeqs 引用 call）。"""
    content = result.content
    if result.is_error and result.error is not None:
        content = result.error
    message = create_message(
        "user",
        [tool_result_block(call_id, [text_block(str(content))], is_error=result.is_error)],
        {"kind": "tool", "callId": call_id},
    )
    data: dict[str, Any] = {"turn": turn, "step": step, "message": message}
    if result.is_error:
        # 对齐上游 tool/result error 字段（llm/src/types.ts:295）：
        # 仅当 error.info 存在才携带 {name, code}；普通工具体错误不带
        info = getattr(result, "error_info", None)
        if info is not None:
            data["error"] = info
    session.append("tool/result", data, surfaceOp="append", sourceEventSeqs=[call_seq])


def _parse_args(raw: str) -> dict:
    try:
        return json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return {}


async def schedule_tool_calls(
    session: Session,
    ctx: Context,
    tools: Any,
    turn: int,
    step: int,
    tool_calls: list[dict],
    signal: ToolExec,
    body_fn: Callable | None = None,
    max_parallel: int = DEFAULT_MAX_PARALLEL_TOOL_CALLS,
    agent: Any = None,
) -> tuple[bool, bool]:
    """调度一个 step 的全部工具调用。返回 (concluded, aborted)。

    tool_calls: 模型序 [{id, name, arguments}]；signal: 共享取消信号容器。
    body_fn: 可注入的"政策通过后执行体"（默认 pipeline_async_body）。
    agent: 所属 AgentLoop（上游 ToolExecution.agent），透传进 ToolExec 供
    工具按会话 id 栅栏访问后台作业；None = 无 agent 调用方。
    """
    planned = list(tool_calls)
    next_ = 0
    while next_ < len(planned):
        first = planned[next_]
        tool = tools.resolve(first["name"])
        mode = "parallel" if execution_mode(tool, _parse_args(first["arguments"])) == "parallel" else "exclusive"
        group = planned[next_:] if mode == "parallel" else [first]
        outcome = await _run_group(
            session, ctx, tools, turn, step, group, mode, signal, body_fn, max_parallel,
            agent=agent,
        )
        next_ += outcome["consumed"]
        if outcome["aborted"]:
            for call in planned[next_:]:
                seq = append_tool_call(session, turn, step, call["id"], call["name"], call["arguments"])
                emit_tool_result(session, turn, step, call["id"], _aborted_result(), seq)
            return False, True
    return False, False


async def _run_group(
    session: Session, ctx: Context, tools: Any, turn: int, step: int,
    group: list[dict], mode: str, signal: ToolExec,
    body_fn: Callable | None, max_parallel: int, agent: Any = None,
) -> dict:
    slots: list[ToolResult | None] = [None] * len(group)
    call_seqs: list[int] = [-1] * len(group)
    next_to_start = 0
    committed = 0
    started = 0
    aborted = signal.signal.is_set()
    scheduler_failure: BaseException | None = None
    in_flight: dict[int, asyncio.Task] = {}
    body_fn = body_fn or pipeline_async_body

    async def commit_ready() -> None:
        nonlocal committed
        while committed < len(group) and slots[committed] is not None:
            call = group[committed]
            emit_tool_result(session, turn, step, call["id"], slots[committed], call_seqs[committed])
            committed += 1

    async def start_call(index: int) -> None:
        nonlocal started, scheduler_failure
        call = group[index]
        call_seqs[index] = append_tool_call(
            session, turn, step, call["id"], call["name"], call["arguments"])
        started += 1
        tool = tools.resolve(call["name"])
        if tool is None:
            # 上游 ToolNotFoundError：code 'UNKNOWN_TOOL'（tools/src/index.ts:494-510）
            slots[index] = ToolResult(ok=False, is_error=True, error=f"未知工具: {call['name']}",
                                      error_info={"name": "ToolNotFoundError", "code": "UNKNOWN_TOOL"})
            return
        try:
            frozen = deep_freeze(_parse_args(call["arguments"]))
        except Exception as e:
            slots[index] = ToolResult(ok=False, is_error=True, error=f"参数无法物化: {e}")
            return
        try:
            rejected = await pipeline_policy_async(ctx, tool, frozen)
        except BaseException as e:
            scheduler_failure = e
            return
        if rejected is not None:
            slots[index] = rejected
            return
        exec_ = ToolExec(signal=signal.signal, agent=agent)
        task = asyncio.create_task(body_fn(ctx, tool, frozen, exec_))
        in_flight[index] = task

    async def fill_pool() -> None:
        nonlocal next_to_start, aborted
        while (not aborted and next_to_start < len(group)
               and len(in_flight) < max_parallel):
            next_call = group[next_to_start]
            if next_to_start > 0 and mode == "parallel":
                nt = tools.resolve(next_call["name"])
                if execution_mode(nt, _parse_args(next_call["arguments"])) != "parallel":
                    break   # 重新分类为 exclusive：等排空，留作下一个屏障
            await start_call(next_to_start)
            next_to_start += 1
            if scheduler_failure is not None:
                return
            await commit_ready()
            if scheduler_failure is not None:
                return
            if signal.signal.is_set():
                aborted = True

    try:
        await fill_pool()
        while in_flight and scheduler_failure is None:
            done, _ = await asyncio.wait(in_flight.values(),
                                         return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                index = next(i for i, t in in_flight.items() if t is task)
                del in_flight[index]
                try:
                    slots[index] = task.result()
                except BaseException as e:
                    scheduler_failure = e
            if scheduler_failure is not None:
                break
            await commit_ready()
            if signal.signal.is_set():
                aborted = True
            await fill_pool()
    except BaseException as e:
        if scheduler_failure is None:
            scheduler_failure = e
        for t in in_flight.values():
            t.cancel()
        await asyncio.gather(*in_flight.values(), return_exceptions=True)
        raise scheduler_failure

    if scheduler_failure is not None:
        for t in in_flight.values():
            t.cancel()
        await asyncio.gather(*in_flight.values(), return_exceptions=True)
        raise scheduler_failure

    if aborted:
        for call in group[started:]:
            seq = append_tool_call(session, turn, step, call["id"], call["name"], call["arguments"])
            emit_tool_result(session, turn, step, call["id"], _aborted_result(), seq)
        return {"consumed": len(group), "aborted": True}
    if committed != started:
        raise RuntimeError("tool-call scheduler: uncommitted settled calls")
    return {"consumed": started, "aborted": False}