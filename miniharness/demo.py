"""端到端演示：假模型 + 工具 + 会话持久化 + 崩溃恢复（无需 API key）。

用法：python -m miniharness.demo
"""
from __future__ import annotations

import tempfile
from pathlib import Path

from .bus import Context
from .llm import FakeLlmAdapter
from .loop import AgentLoop
from .persistence import JsonlPersistence, repair_and_replay
from .session import Session, derive_messages, turn_balance
from .tools import Tool, ToolRegistry


def main() -> None:
    print("=" * 60)
    print("MiniHarness 端到端演示（第 4 + 5 章验收）")
    print("=" * 60)

    tmp = tempfile.mkdtemp(prefix="miniharness-demo-")
    root = Path(tmp)

    # ---- 组装：Session + Context + 工具 + 假模型 + Loop ----
    session = Session("demo-001")
    ctx = Context(name="root")
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
    print("\n-- 回合结束，日志（事件溯源）--")
    for ev in session.events:
        print(f"  #{ev['seq']:<3} {ev['type']:<18} {dict(ev)}")
    print(f"\n-- 模型历史（deriveMessages 投影）--")
    for m in derive_messages(session.events):
        print(f"  [{m['role']:<9}] {m['content']}")
    print(f"\n-- 括号平衡: turn_balance = {turn_balance(session.events)} --")

    # ---- 持久化 + "崩溃" + 恢复 ----
    print("\n-- 模拟进程崩溃（未写 turn/end 就退出）--")
    crash_session = Session("demo-002")
    crash_loop = AgentLoop(crash_session, FakeLlmAdapter(final_text="快照"), reg, ctx)
    crash_session.append({"type": "turn/start"})
    crash_session.append({"type": "user/message", "content": "这条消息刚发出就崩了", "surfaceOp": "append"})

    pers = JsonlPersistence(root / "sessions")
    for ev in crash_session.events:
        pers.append("demo-002", dict(ev))
    pers.flush()

    print("重启后 load + 修复...")
    recovered = Session("demo-002")
    repair_and_replay(pers, "demo-002", recovered)
    print(f"  turn_balance = {turn_balance(recovered.events)}（interrupted 已补齐）")
    print(f"  最后一条事件: {recovered.events[-1]['type']} reason={recovered.events[-1].get('reason')}")

    print("\n-- 回放：从日志重建历史并继续对话 --")
    resumed = AgentLoop(recovered, FakeLlmAdapter(final_text="恢复完成。"), reg, ctx)
    resumed.followup("继续刚才的任务")
    print(f"  最终回答: {resumed.last_response()}")

    print("\n" + "=" * 60)
    print("演示完成。清理目录:", tmp)


if __name__ == "__main__":
    main()