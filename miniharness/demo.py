"""端到端演示：假模型 + 工具 + 会话持久化 + 崩溃恢复（无需 API key）。

用法：python -m miniharness.demo
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from .core.scope import Context
from .llm import FakeLlmAdapter
from .llm.retry import apply_retry_planner
from .core.agent_loop.agent import AgentLoop
from .core.session.persistence import JsonlPersistence, repair_and_replay
from .core.session import Session, create_message, derive_messages, text_block, turn_balance
from .core.tools import Tool, ToolRegistry


def main() -> None:
    # Windows 管道/控制台默认 cp1252 无法编码中文，统一 UTF-8 输出
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    print("=" * 60)
    print("MiniHarness 端到端演示（第 4 + 5 章验收）")
    print("=" * 60)

    tmp = tempfile.mkdtemp(prefix="miniharness-demo-")
    root = Path(tmp)

    # ---- 组装：Session + Context + 工具 + 假模型 + Loop ----
    session = Session("demo-001")
    ctx = Context(name="root")
    apply_retry_planner(ctx)
    reg = ToolRegistry(ctx)
    reg.register(Tool(
        name="bash",
        description="Run a shell command.",
        parameters={
            "type": "object",
            "properties": {"cmd": {"type": "string", "description": "command to run"}},
            "required": ["cmd"],
        },
        execute=lambda args, e: f"stdout: {args['cmd']}",
    ))
    adapter = FakeLlmAdapter(
        tool_call={"name": "bash", "arguments": {"cmd": "ls -la"}},
        final_text="我执行了 ls -la，任务完成。",
    )
    loop = AgentLoop(session, adapter, reg, ctx, system_prompt="你是一个可靠的小助手。")

    # ---- 跑一个回合 ----
    loop.followup("帮我看看目录里有什么")
    print("\n-- 回合结束，日志（事件溯源，信封 {type, seq, time, data}）--")
    for ev in session.events:
        print(f"  #{ev['seq']:<3} {ev['type']:<18} {dict(ev['data'])}")
    print(f"\n-- 模型历史（deriveMessages 投影，ContentBlock 消息）--")
    for m in derive_messages(session.events):
        blocks = " | ".join(
            b["type"] + (":" + b.get("text", "") if b.get("text") is not None else "")
            for b in m["content"]
        )
        print(f"  [{m['role']:<9}] {blocks}")
    print(f"\n-- 括号平衡: turn_balance = {turn_balance(session.events)} --")

    # ---- 持久化 + "崩溃" + 恢复 ----
    print("\n-- 模拟进程崩溃（未写 turn/end 就退出）--")
    crash_session = Session("demo-002")
    crash_loop = AgentLoop(crash_session, FakeLlmAdapter(final_text="快照"), reg, ctx)
    crash_session.append("turn/start", {"turn": 1})
    crash_session.append("user/message", create_message(
        "user", [text_block("这条消息刚发出就崩了")], {"kind": "user"},
    ), surfaceOp="append")

    pers = JsonlPersistence(root / "sessions")
    for ev in crash_session.events:
        pers.append("demo-002", dict(ev))
    pers.flush()

    print("重启后 load + 修复...")
    recovered = repair_and_replay(pers, "demo-002", Session("demo-002"))
    print(f"  turn_balance = {turn_balance(recovered.events)}（interrupted 已补齐）")
    last = next(ev for ev in reversed(recovered.events) if ev["type"] != "session/end-seed")
    print(f"  最后一条事件: {last['type']} reason={last['data']['reason']}")

    print("\n-- 回放：从日志重建历史并继续对话 --")
    resumed = AgentLoop(recovered, FakeLlmAdapter(final_text="恢复完成。"), reg, ctx)
    resumed.followup("继续刚才的任务")
    print(f"  最终回答: {resumed.last_response()}")

    print("\n" + "=" * 60)
    print("演示完成。清理目录:", tmp)


if __name__ == "__main__":
    main()