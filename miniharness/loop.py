"""第 4 章：Agent Loop —— turn/step 状态机（同步简化版）。

对应 dsh 真实源码：packages/core/agent-loop。

状态机要点（与真实实现一致的契约）：
  * turn 打开于认领输入之前 —— "被拒绝的尝试"也会留下持久化记录
  * turn/step 编号从 1 起，每 turn 内 step 重置为 1（session/invariant.ts）
  * step = 一次模型请求 + 它调用的工具；每个 chunk 落 assistant/chunk，
    assistant/message 带 sourceEventSeqs 引用 chunk seq（模型可见 ⟺ 已记录）
  * 工具结果回灌后在同一 turn 内自动进入下一步（next-step 继续）
  * pre-step 拒绝 → turn 以 {kind:'blocked'} 结束；step/end 与 turn/end
    在 finally 中必定落日志（失败时 reason 为 {kind:'error'|'aborted'|...}）
"""
from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import Any, Callable

from .bus import Context
from .llm import BlockAssembler, LlmAdapter, LlmFailure
from .llm_retry import apply_retry_planner
from .scheduler import schedule_tool_calls
from .session import (
    Session,
    create_message,
    derive_messages,
    text_block,
    tool_call_block,
    tool_result_block,
)
from .tools import ToolExec, ToolRegistry, ToolResult, run_pipeline


class _AbortProxy:
    """AbortSignal 的同步替身：aborted 反映宿主 loop 的取消标记。

    供 agent/request-error 的 signal 字段与 llm-retry 的可取消等待使用
    （mini 同步模型下无真实 AbortSignal，语义对齐：取消后不再重试）。
    """

    def __init__(self, owner: "AgentLoop"):
        self._owner = owner

    @property
    def aborted(self) -> bool:
        return self._owner._cancelled


class AgentLoop:
    def __init__(
        self,
        session: Session,
        adapter: LlmAdapter,
        tools: ToolRegistry,
        ctx: Context,
        system_prompt: str = "你是一个助手。",
        max_steps: int = 50,
        max_parallel_tool_calls: int = 10,
    ):
        self.session = session
        self.adapter = adapter
        self.tools = tools
        self.ctx = ctx
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.max_parallel_tool_calls = max_parallel_tool_calls   # 阶段 7：并行池上限（上游 DEFAULT_MAX_PARALLEL_TOOL_CALLS）
        self.status = "idle"
        self.inbox: deque[dict[str, Any]] = deque()
        self._turn_open = False
        self._continue = False
        self._cancelled = False
        self._turn = 0     # 当前已打开的 turn 编号（1 起）
        self._step = 0     # 当前已打开的 step 编号（1 起，每 turn 重置）
        self._turn_end: dict | None = None
        self._step_signal: ToolExec | None = None   # 阶段 7：当前 step 的共享取消信号
        # 阶段 4：重试规划器（agent/request-error 监听器，幂等挂载）
        apply_retry_planner(ctx)
        self._abort_proxy = _AbortProxy(self)

    # ---------- 对外入口 ----------

    def followup(self, content: str, source: str = "user") -> None:
        """用户输入：先进 inbox，待 pre-step 通过后才 append 进日志。"""
        message = create_message(
            "user", [text_block(content)],
            {"kind": "user"} if source == "user" else {"kind": "plugin", "plugin": source},
        )
        self.inbox.append(message)
        self._pump()

    # ---------- 干预面（第 9 章：Agent 干预面） ----------

    def steer(self, content: str, source: str = "user") -> None:
        """下一 step 唤醒（上游 steer）：idle 时立即开 turn；
        running 时入 inbox，当前 step 跑完后的边界消费（同步模型下
        循环条件在每个 step 之后检查，等价"下个 step 边界"）。"""
        message = create_message(
            "user", [text_block(content)],
            {"kind": "user"} if source == "user" else {"kind": "plugin", "plugin": source},
        )
        self.inbox.append(message)
        self._pump()

    def inject(self, content: str, source: str = "plugin") -> None:
        """非唤醒注入（上游 inject）：只入 inbox，不开 turn。
        后续任一 followup/steer 触发 pump 时按 FIFO 一并消费。"""
        message = create_message(
            "user", [text_block(content)],
            {"kind": "plugin", "plugin": source},
        )
        self.inbox.append(message)

    def cancel(self, cause: str | None = None, keep_inbox: bool = False) -> None:
        """取消（上游 cancel）：清 inbox（除非 keep_inbox）+ 中止活跃回合。

        同步路径中无法中断正在执行的 step，取消在 step 边界生效：
        当前 step 跑完后不再继续（工具回调内调用也可），turn 以
        {kind:'aborted'} 闭合；无活跃回合且 inbox 为空 → idle no-op。
        阶段 7 async 路径：置位共享 signal，并行调度器检测后停止补池、
        排干已启动调用、未启动的按模型序补 TOOL_ABORTED_BEFORE_DISPATCH
        合成错误结果（与上游 appendSkippedToolCall 一致）。
        """
        if not self._turn_open and not self.inbox:
            return
        if not keep_inbox:
            self.inbox.clear()
        if self._turn_open:
            self._cancelled = True
            self._turn_end = {"kind": "aborted"}
            if self._step_signal is not None:
                self._step_signal.signal.set()

    def when_idle(self) -> bool:
        """quiescence（上游 whenIdle）：无活跃回合即 idle。
        同步模型下没有"在飞 step"，status 检查即为整机静默判定。"""
        return self.status == "idle" and not self._turn_open

    def run_maintenance(self, task: Callable[[], Any]) -> Any:
        """维护任务（上游 runMaintenance）：仅 true idle 下执行，
        不落 turn 日志、不产生会话事件；执行期间 status='maintenance'。"""
        if not self.when_idle():
            raise RuntimeError("run_maintenance 要求 true idle（无活跃回合）")
        self.status = "maintenance"
        try:
            return task()
        finally:
            self.status = "idle"

    def run(self, content: str) -> str:
        """同步跑完一次输入，返回最终 assistant 文本。"""
        self.followup(content)
        return self.last_response()

    async def run_async(self, content: str) -> str:
        """阶段 7：async 跑完一次输入（真并行工具执行路径）。"""
        message = create_message(
            "user", [text_block(content)], {"kind": "user"},
        )
        self.inbox.append(message)
        await self._pump_async()
        return self.last_response()

    def last_response(self) -> str:
        for m in reversed(derive_messages(self.session.events)):
            if m["role"] == "assistant":
                return "".join(b["text"] for b in m["content"] if b["type"] == "text")
        return ""

    # ---------- turn 生命周期 ----------

    def _open_turn(self) -> None:
        self.status = "running"
        self._turn += 1
        self._step = 0
        self._turn_open = True
        self._turn_end = None
        self.session.append("turn/start", {"turn": self._turn})

    def _close_turn(self, reason: dict | None = None) -> None:
        if not self._turn_open:
            return
        self.session.append("turn/end", {
            "turn": self._turn,
            "reason": reason or self._turn_end or {"kind": "completed"},
        })
        self._turn_open = False
        self._cancelled = False
        self.status = "idle"

    # ---------- 主循环 ----------

    def _pump(self) -> None:
        steps = 0
        try:
            while (self.inbox or self._continue) and not self._cancelled:
                steps += 1
                if steps > self.max_steps:
                    raise RuntimeError(f"超过最大 step 数 {self.max_steps}，疑似死循环")
                if not self._turn_open:
                    self._open_turn()
                claimed = self.inbox.popleft() if self.inbox else None
                self._run_step(claimed)
        except LlmFailure as e:
            self._turn_end = {"kind": "error", "failure": e.failure}
            raise
        except Exception as e:
            self._turn_end = {"kind": "error", "failure": {"code": "UNKNOWN", "message": str(e)}}
            raise
        finally:
            # turn/end 必定落日志（与上游 agent.ts finally 一致）
            if self._turn_open:
                self._close_turn(self._turn_end)

    async def _pump_async(self) -> None:
        """阶段 7：async 主循环（语义与 _pump 相同，step 走并行调度器）。"""
        steps = 0
        try:
            while (self.inbox or self._continue) and not self._cancelled:
                steps += 1
                if steps > self.max_steps:
                    raise RuntimeError(f"超过最大 step 数 {self.max_steps}，疑似死循环")
                if not self._turn_open:
                    self._open_turn()
                claimed = self.inbox.popleft() if self.inbox else None
                await self._run_step_async(claimed)
        except LlmFailure as e:
            self._turn_end = {"kind": "error", "failure": e.failure}
            raise
        except Exception as e:
            self._turn_end = {"kind": "error", "failure": {"code": "UNKNOWN", "message": str(e)}}
            raise
        finally:
            if self._turn_open:
                self._close_turn(self._turn_end)

    def _run_step(self, claimed: dict | None) -> None:
        """一个 step：pre-step → 落日志 → LLM 流式 → 工具管线 → step/end（finally）。"""
        if claimed is not None:
            decision = self.ctx.waterfall("agent/pre-step", {"messages": [claimed]})
            if isinstance(decision, dict) and decision.get("verdict") == "reject":
                self._turn_end = {"kind": "blocked"}
                return  # 零 step 尝试：不留 step/start，turn 以 blocked 闭合

        self._step += 1
        self._continue = False
        try:
            tool_calls = self._stream_step(claimed)
            self._execute_tools_sync(tool_calls)
            self._continue = bool(tool_calls)
        finally:
            # step/end 必定落日志（与上游 agent.ts finally 一致）
            self.session.append("step/end", {"turn": self._turn, "step": self._step})

    async def _run_step_async(self, claimed: dict | None) -> None:
        """阶段 7：async 版 step。pre-step 走 awaterfall，工具走并行调度器。"""
        if claimed is not None:
            decision = await self.ctx.awaterfall("agent/pre-step", {"messages": [claimed]})
            if isinstance(decision, dict) and decision.get("verdict") == "reject":
                self._turn_end = {"kind": "blocked"}
                return

        self._step += 1
        self._continue = False
        try:
            tool_calls = self._stream_step(claimed)
            await self._execute_tools_async(tool_calls)
            self._continue = bool(tool_calls)
        finally:
            self.session.append("step/end", {"turn": self._turn, "step": self._step})

    def _stream_step(self, claimed: dict | None) -> list[dict]:
        """落日志 + LLM 流式（同步适配器阻塞事件循环，可接受：不与在飞任务交错）。
        返回模型产出的 tool-call 块列表（模型序）。

        失败恢复（阶段 4）：适配器抛 LlmFailure 时派发 agent/request-error
        waterfall（上游 agent-loop 同语义扩展点）；{kind:'retry'} → 同 step
        内重新发起模型请求（同一 messages，历史不因失败 attempt 改变；
        request/header 只落一次——上游仅在 header 变化时追加）；其它/未
        挂载监听器 → 失败终局（抛给 _pump 闭合 turn）。
        """
        self.session.append("step/start", {"turn": self._turn, "step": self._step})
        if claimed is not None:
            self.session.append("user/message", claimed, surfaceOp="append")

        # 请求配置入日志（模型可见 ⟺ 已记录；上游 request/header 的简化版）
        self.session.append("request/header", {
            "header": {
                "model": getattr(self.adapter, "model", None),
                "provider": self.adapter.provider,
            },
            "reason": "initial",
        })

        history = derive_messages(self.session.events)
        system = create_message("system", [text_block(self.system_prompt)],
                                {"kind": "plugin", "plugin": "system-prompt"})
        messages = [system] + history

        return self._stream_attempt(messages)

    def _stream_attempt(self, messages: list[dict]) -> list[dict]:
        """一次 step 内的模型请求 attempt 循环（上游 request → retry → 终局）。"""
        while True:
            # 流式：逐 chunk 落 assistant/chunk，块组装，finish 时落 assistant/message
            assembler = BlockAssembler()
            chunk_seqs: list[int] = []
            try:
                for chunk in self.adapter.stream(messages, self._tool_definitions()):
                    ev = self.session.append("assistant/chunk", {
                        "turn": self._turn, "step": self._step, "chunk": chunk,
                    })
                    chunk_seqs.append(ev["seq"])
                    assembler.push(chunk)
            except LlmFailure as e:
                action = self.ctx.waterfall("agent/request-error", {
                    "agent": self,
                    "turn": self._turn,
                    "step": self._step,
                    "provider": self.adapter.provider,
                    "failure": e,
                    "retryPolicy": getattr(self.adapter, "retry_policy", None),
                    "signal": self._abort_proxy,
                })
                if isinstance(action, dict) and action.get("kind") == "retry":
                    continue
                raise

            assistant_message = assembler.message()
            if assembler.finish is None:
                assembler.finish = {"kind": "stop"}
            if assembler.finish["kind"] == "max-tokens":
                self._turn_end = {"kind": "max-tokens"}   # max-tokens 粘滞
            self.session.append("assistant/message", {
                "turn": self._turn, "step": self._step, "message": assistant_message,
                **({"usage": assembler.usage} if assembler.usage is not None else {}),
            }, surfaceOp="append", sourceEventSeqs=chunk_seqs)

            return [
                b for b in assistant_message["content"] if b.get("type") == "tool-call"
            ]

    def _execute_tools_sync(self, tool_calls: list[dict]) -> None:
        """同步路径：先记录 tool/call 再执行（durable before dispatch），逐个串行。"""
        for block in tool_calls:
            self._run_tool(block["id"], block["name"], block["arguments"])

    async def _execute_tools_async(self, tool_calls: list[dict]) -> None:
        """阶段 7：并行调度器（exclusive 屏障 + 有界滚动池 + 模型序提交）。"""
        if not tool_calls:
            return
        self._step_signal = ToolExec()
        try:
            await schedule_tool_calls(
                self.session, self.ctx, self.tools, self._turn, self._step,
                tool_calls, self._step_signal,
                max_parallel=self.max_parallel_tool_calls,
            )
        finally:
            self._step_signal = None

    def _tool_definitions(self) -> list[dict]:
        defs = []
        for name in self.tools.names():
            tool = self.tools.resolve(name)
            if tool:
                defs.append({
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.parameters,
                })
        return defs

    def _run_tool(self, call_id: str, name: str, arguments: str) -> None:
        # arguments 以模型产出的原始 JSON 字符串落盘（与上游字段一致）；
        # 未知工具同样先落 tool/call 再产出 error 结果（上游 appendToolCall 先于派发）
        self.session.append("tool/call", {
            "turn": self._turn, "step": self._step,
            "callId": call_id, "name": name, "arguments": arguments,
        })
        tool = self.tools.resolve(name)
        if tool is None:
            result = ToolResult(ok=False, is_error=True, error=f"未知工具: {name}")
        else:
            try:
                parsed = json.loads(arguments) if arguments else {}
            except json.JSONDecodeError:
                parsed = {}
            result = run_pipeline(self.ctx, tool, parsed)

        content = result.content
        if result.is_error and result.error is not None:
            content = result.error
        message = create_message(
            "user",
            [tool_result_block(call_id, [text_block(str(content))], is_error=result.is_error)],
            {"kind": "tool", "callId": call_id},
        )
        data: dict[str, Any] = {
            "turn": self._turn, "step": self._step, "message": message,
        }
        if result.is_error:
            data["error"] = {"name": "ToolExecutionError", "code": "TOOL_EXECUTION_ERROR"}
        self.session.append("tool/result", data, surfaceOp="append")