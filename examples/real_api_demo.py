"""真实 DeepSeek API 对话示例（需要 DEEPSEEK_API_KEY）。

运行：
  set DEEPSEEK_API_KEY=sk-xxx
  python examples/real_api_demo.py
可选：set DEEPSEEK_BASE_URL=https://... （指向兼容代理）
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from miniharness import AgentLoop, Context, DeepSeekAdapter, Session, Tool, ToolRegistry
from miniharness.llm.retry import apply_retry_planner


async def shell(args, env):
    return f"stdout: {args['cmd']}"


def main():
    if not os.environ.get("DEEPSEEK_API_KEY"):
        print("缺少 DEEPSEEK_API_KEY 环境变量，跳过真实调用。")
        return 1

    session = Session("real-api-demo")
    ctx = Context()
    apply_retry_planner(ctx)
    reg = ToolRegistry(ctx)
    reg.register(Tool(
        name="bash",
        description="执行 shell 命令",
        parameters={"type": "object", "properties": {"cmd": {"type": "string"}}, "required": ["cmd"]},
        execute=shell,
    ))

    loop = AgentLoop(session, DeepSeekAdapter(model="deepseek-chat"), reg, ctx)
    reply = loop.run("用 bash 执行 echo hello，然后告诉我结果。")
    print("模型答复:", reply)
    print("事件序列:", [e["type"] for e in session.events])
    return 0


if __name__ == "__main__":
    sys.exit(main())
