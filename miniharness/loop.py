"""第 4 章：Agent Loop —— turn/step 状态机（同步简化版）。

对应 dsh 真实源码：packages/core/agent-loop。

状态机要点（与真实实现一致的契约）：
  * turn 打开于认领输入之前 —— "被拒绝的尝试"也会留下持久化记录
  * step = 一次模型请求 + 它调用的工具
  * 工具结果回灌后在同一 turn 内自动进入下一步（_continue）
  * 模型可见 ⟺ 已记录：user/message 在 pre-step 通过后才落日志
"""
from __future__ import annotations

import json
from collections import deque
from typing import Any

from .bus import Context
from .llm import LlmAdapter
from .session import Session, derive_messages
from .tools import ToolRegistry, ToolResult, run_pipeline


class AgentLoop:
    def __init__(
        self,
        session: Session,
        adapter: LlmAdapter,
        tools: ToolRegistry,
        ctx: Context,
        system_prompt: str = "你是一个助手。",
        max_steps: int = 50,
    ):
        self.session = session
        self.adapter = adapter
        self.tools = tools
        self.ctx = ctx
        self.system_prompt = system_prompt
        self.max_steps = max_steps
        self.status = "idle"
        self.inbox: deque[dict[str, Any]] = deque()
        self._turn_open = False
        self._continue = False

    # ---------- 对外入口 ----------

    def followup(self, content: str, source: str = "user") -> None:
        """用户输入：先进 inbox，待 pre-step 通过后才 append 进日志。"""
        self.inbox.append({"role": "user", "content": content, "source": source})
        self._pump()

    def run(self, content: str) -> str:
        """同步跑完一次输入，返回最终 assistant 文本。"""
        self.followup(content)
        return self.last_response()

    def last_response(self) -> str:
        for m in reversed(derive_messages(self.session.events)):
            if m["role"] == "assistant":
                return m["content"]
        return ""

    # ---------- turn 生命周期 ----------

    def _open_turn(self) -> None:
        if self._turn_open:
            return
        self.status = "running"
        self.session.append({"type": "turn/start"})
        self._turn_open = True

    def _close_turn(self, reason: str = "completed") -> None:
        if not self._turn_open:
            return
        self.session.append({"type": "turn/end", "reason": reason})
        self._turn_open = False
        self.status = "idle"

    # ---------- 主循环 ----------

    def _pump(self) -> None:
        steps = 0
        while self.inbox or self._continue:
            steps += 1
            if steps > self.max_steps:
                raise RuntimeError(f"超过最大 step 数 {self.max_steps}，疑似死循环")
            self._open_turn()
            claimed = self.inbox.popleft() if self.inbox else None
            self._run_step(claimed)
            if not self.inbox and not self._continue:
                self._close_turn()

    def _run_step(self, claimed: dict | None) -> None:
        """一个 step：pre-step → 落日志 → LLM 流式 → 工具管线 → step/end。"""
        if claimed is not None:
            # agent/pre-step waterfall：可以拒绝（短路即决策）
            decision = self.ctx.waterfall("agent/pre-step", {"messages": [claimed]})
            if isinstance(decision, dict) and decision.get("verdict") == "reject":
                return  # 零 step 尝试：不留 step/start，turn 照常闭合
            self.session.append({"type": "step/start"})
            self.session.append({
                "type": "user/message",
                "content": claimed["content"],
                "surfaceOp": "append",
                "source": claimed.get("source", "user"),
            })
        else:
            # next-step 继续：工具结果回灌后同一 turn 内的再次请求
            self.session.append({"type": "step/start"})

        history = derive_messages(self.session.events)
        messages = [{"role": "system", "content": self.system_prompt}] + history

        # LLM 流式（简化：不逐 chunk 落 assistant/chunk，只落合并后的 message）
        chunks = list(self.adapter.stream(messages, self._tool_definitions()))
        text = "".join(c.get("delta", "") for c in chunks if c["kind"] == "text-delta")
        tool_calls = [c for c in chunks if c["kind"] == "tool-call-delta"]

        self.session.append({
            "type": "assistant/message",
            "content": text,
            "surfaceOp": "append",
            "toolCalls": [{"name": c["name"], "arguments": c.get("arguments", "")} for c in tool_calls],
        })

        for call in tool_calls:
            self._run_tool(call["name"], call.get("arguments", ""))

        self.session.append({"type": "step/end"})
        self._continue = bool(tool_calls)

    def _tool_definitions(self) -> list[dict]:
        defs = []
        for name in self.tools.names():
            tool = self.tools.resolve(name)
            if tool:
                defs.append({
                    "schema": {
                        "type": "function",
                        "function": {
                            "name": tool.name,
                            "description": tool.description,
                            "parameters": tool.parameters,
                        },
                    }
                })
        return defs

    def _run_tool(self, name: str, arguments: Any) -> None:
        tool = self.tools.resolve(name)
        if tool is None:
            result = ToolResult(ok=False, is_error=True, error=f"未知工具: {name}")
        else:
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments)
                except json.JSONDecodeError:
                    arguments = {}
            # 执行前先记录（durable）
            self.session.append({"type": "tool/call", "name": name, "arguments": arguments})
            result = run_pipeline(self.ctx, tool, arguments)
        self.session.append({
            "type": "tool/result",
            "name": name,
            "content": result.content,
            "isError": result.is_error,
            "error": result.error,
            "surfaceOp": "append",
        })