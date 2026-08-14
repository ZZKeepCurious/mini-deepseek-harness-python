"""多轮对话示例（FakeLlmAdapter，无需 API key）。

演示：一轮带工具调用的回合（同 turn 内工具回灌续跑）+ 两轮纯文本对话。
运行：python examples/chat_demo.py
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from miniharness import (
    AgentLoop,
    Context,
    FakeLlmAdapter,
    Session,
    Tool,
    ToolRegistry,
    turn_balance,
)


def make_calc(args, env):
    return f"结果: {eval(args['expr'])}"


def main():
    session = Session("chat-demo")
    ctx = Context()
    reg = ToolRegistry(ctx)
    reg.register(Tool(
        name="calc",
        description="计算表达式",
        parameters={"type": "object", "properties": {"expr": {"type": "string"}}, "required": ["expr"]},
        execute=make_calc,
    ))

    loop = AgentLoop(
        session,
        FakeLlmAdapter(tool_call={"name": "calc", "arguments": {"expr": "1+1"}}, final_text="结果是 2。"),
        reg, ctx,
    )
    print("=== 回合 1：带工具调用（1+1 等于几？）===")
    print("答复:", loop.run("1+1 等于几？"))

    chat_loop = AgentLoop(session, FakeLlmAdapter(final_text="明白。"), reg, ctx)
    print("=== 回合 2/3：纯文本多轮对话 ===")
    print("答复:", chat_loop.run("今天天气怎么样"))
    print("答复:", chat_loop.run("那明天呢"))

    turns = sum(1 for e in session.events if e["type"] == "turn/start")
    print(f"=== 日志：turn/start x{turns}，事件 {len(session.events)} 条，balance={turn_balance(session.events)} ===")


if __name__ == "__main__":
    main()
