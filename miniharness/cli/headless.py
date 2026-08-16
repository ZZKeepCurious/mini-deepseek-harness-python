"""headless 一次性任务入口：对齐上游 `@deepseek-ai/dsh-headless`（bundle/headless）。

上游语义（`packages/bundle/headless/src/index.ts` + `startup.ts`，已核实）：

1. **任务文本即命令行**：`dsh --profile headless "task"` 的位置参数 join 空格；
   缺失或空白任务在 runner 激活前以 usage error 拒绝。
2. **全新持久化 Agent**：session id 随机（`session-<uuid>`），任务作为普通用户消息提交。
3. **停稳后 flush**，再汇总 runner 持有区间的事件（firstSeq 起，turn/start 之后开始收集）：
   只拼接 `text` 块，**最后一条非空** assistant 文本胜出；`turn/end` 的 reason 被记录。
4. **进程级契约**：stdout 写 `text + '\\n'`；最终 reason 为 `{kind:'error'}` 时 stderr 写
   `dsh: <code>: <message>`；退出码 = reason.kind == 'completed' ? 0 : 1。
5. **不开监听端口**；`ctx.appExit` 由启动器持有（mini 由 CLI 提供 exit 函数）。

简化说明（有意保留）：上游 runner 经 Cordis 服务（agents / sessions / agentDefaultModel）
创建 Agent；mini 直接构造 Session + AgentLoop（载体差异，契约不变）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any, Callable

from ..core.scope import Context
from ..llm import DeepSeekAdapter, FakeLlmAdapter, LlmAdapter, LlmFailure
from ..llm.retry import apply_retry_planner
from ..core.agent_loop.agent import AgentLoop
from ..core.session.persistence import JsonlPersistence
from ..core.session import Session
from ..core.tools import ToolRegistry
from .default_tools import default_tools


def summarize(events: list | tuple, first_seq: int) -> tuple[str, dict | None]:
    """汇总 runner 持有区间：最后一条非空 assistant 文本 + 最终 turn/end reason。

    与上游 `summarize`（index.ts:61）一致：firstSeq 起；turn/start 后才开始收集；
    只拼接 text 块；空文本不覆盖已有结果；turn/end 记录 reason。
    """
    started = False
    text = ""
    reason = None
    for ev in events:
        if ev["seq"] < first_seq:
            continue
        if ev["type"] == "turn/start":
            started = True
            continue
        if not started:
            continue
        if ev["type"] == "assistant/message":
            joined = "".join(
                b["text"] for b in ev["data"]["message"]["content"]
                if b.get("type") == "text" and b.get("text") is not None
            )
            if joined != "":
                text = joined
        if ev["type"] == "turn/end":
            reason = ev["data"]["reason"]
    return text, reason


def run_headless(
    task: str,
    *,
    adapter: LlmAdapter,
    tools: ToolRegistry | None = None,
    ctx: Context | None = None,
    persistence: JsonlPersistence | None = None,
    session_id: str | None = None,
    stdout: Any | None = None,
    stderr: Any | None = None,
    exit_fn: Callable[[int], None] | None = None,
) -> None:
    """跑一个一次性任务并请求进程退出（对齐上游 headless-runner 的 run()）。

    返回前必定：flush（若给了 persistence）→ stdout 写最终文本 →
    按 reason 写 stderr → exit_fn(reason.kind == 'completed' ? 0 : 1)。
    """
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if exit_fn is None:
        # 对齐上游（bundle/headless/src/index.ts）：ctx.appExit 由启动器持有，
        # 缺失即宿主错误 → fail loud（B8 边界）
        raise ValueError("appExit is required: host must provide the exit hook")
    if task.strip() == "":
        raise ValueError("task is required")
    ctx = ctx or Context(name="headless")
    apply_retry_planner(ctx)
    tools = tools or default_tools(ctx)
    session = Session(session_id or f"session-{os.urandom(8).hex()[:12]}")
    loop = AgentLoop(session, adapter, tools, ctx)
    first_seq = session.seq
    try:
        loop.followup(task)
    except Exception as error:  # 上游 run().catch(fail)：意外失败 stderr 一行 + exit(1)
        stderr.write(f"dsh: {error}\n")
        exit_fn(1)
        return
    if persistence is not None:
        for ev in session.events:
            persistence.append(session.session_id, dict(ev))
        persistence.flush()

    text, reason = summarize(session.events, first_seq)
    stdout.write(text + "\n")
    if reason is not None and reason.get("kind") == "error":
        error = reason.get("error") or {}
        stderr.write(f"dsh: {error.get('code', 'UNKNOWN')}: {error.get('message', '')}\n")
    exit_fn(0 if (reason is not None and reason.get("kind") == "completed") else 1)


def headless_main(task: str) -> None:
    """headless 模式组装：默认 DeepSeek 适配器 + JSONL 持久化到 MINIHARNESS_HOME。

    无 DEEPSEEK_API_KEY 时 fail loud（上游 headless 走真实模型，无内置回退）。
    """
    ctx = Context(name="headless")
    try:
        adapter: LlmAdapter = DeepSeekAdapter()
    except LlmFailure as e:
        sys.stderr.write(f"dsh: {e.failure['code']}: {e.failure['message']}\n")
        sys.exit(1)
    home = Path(os.environ.get("MINIHARNESS_HOME", Path.home() / ".miniharness"))
    persistence = JsonlPersistence(home / "sessions")
    run_headless(task, adapter=adapter, ctx=ctx, persistence=persistence, exit_fn=sys.exit)


if __name__ == "__main__":
    # 直接运行：python -m miniharness.cli.headless "task"
    task = " ".join(sys.argv[1:])
    if task.strip() == "":
        sys.stderr.write('error: a task is required, for example: python -m miniharness.cli.headless "run the tests"\n')
        sys.exit(1)
    headless_main(task)
