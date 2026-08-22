"""`miniharness sessions` 子命令：会话列表 / 恢复 / 删除（mini 教学扩展）。

上游无对应 CLI 子命令：会话管理在 web 表层（dsh --profile web 的浏览器 GUI）。
mini 未复现 web 表面，故以子命令提供同等的本地管理入口（契约不变：加载 fail-closed、
崩溃修复只合成 closers、resume 继续对话遵循 headless 的输出与退出码契约）。
"""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ..core.scope import Context
from ..llm.retry import apply_retry_planner
from ..compaction import install_compaction, install_tool_result_pruner
from ..jobs import install_jobs
from ..core.system_prompt import install_system_prompt
from .default_tools import default_tools
from .headless import summarize
from ..llm import DeepSeekAdapter, LlmAdapter, LlmFailure
from ..core.agent_loop.agent import AgentLoop
from ..core.session.persistence import JsonlPersistence, load_events_checked, repair_and_replay
from ..core.session import Session, repair_interrupted_turn, turn_balance
from ..core.session_store import install_sessions


def sessions_root() -> Path:
    home = Path(os.environ.get("MINIHARNESS_HOME", Path.home() / ".miniharness"))
    return home / "sessions"


def _fmt_time(ms: int | None) -> str:
    if ms is None:
        return "-"
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def list_sessions(root: Path) -> list[dict]:
    pers = JsonlPersistence(root)
    out = []
    for header in pers.list_headers():
        sid = header["id"]
        entry = {"id": sid, "events": 0, "last": None, "balanced": None, "error": None}
        try:
            raw = load_events_checked(pers.load(sid, cwd=header.get("cwd")))
        except RuntimeError as e:
            entry["error"] = str(e)
            out.append(entry)
            continue
        repaired = repair_interrupted_turn(raw)
        entry["events"] = len(raw)
        entry["last"] = raw[-1]["time"] if raw else None
        entry["balanced"] = turn_balance(repaired) == 0
        out.append(entry)
    return out


def print_list(root: Path, stdout: Any | None = None) -> None:
    stdout = stdout or sys.stdout
    rows = list_sessions(root)
    if not rows:
        stdout.write(f"no sessions under {root}\n")
        return
    for row in rows:
        if row["error"]:
            stdout.write(f"{row['id']}  ERROR: {row['error']}\n")
            continue
        state = "balanced" if row["balanced"] else "unbalanced"
        stdout.write(
            f"{row['id']}  events={row['events']}  {state}  last={_fmt_time(row['last'])}\n"
        )


def resume_session(
    session_id: str,
    task: str,
    *,
    root: Path,
    adapter: LlmAdapter,
    stdout: Any | None = None,
    stderr: Any | None = None,
    exit_fn: Callable[[int], None] | None = None,
) -> None:
    """恢复会话：fail-closed 加载 → 崩溃修复 → 重放；带任务则继续对话（headless 契约）。"""
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    exit_fn = exit_fn or sys.exit
    pers = JsonlPersistence(root)
    if pers.path_of(session_id) is None:
        stderr.write(f"error: session {session_id!r} not found\n")
        exit_fn(1)
        return
    session = repair_and_replay(pers, session_id, Session(session_id))
    base_count = len(session.events)
    if task.strip() == "":
        stdout.write(
            f"session {session_id}\n"
            f"  events: {base_count} (balanced: {turn_balance(session.events) == 0})\n"
            f"  created: {_fmt_time(session.created_at)}\n"
            f"  last:    {_fmt_time(session.events[-1]['time'] if session.events else None)}\n"
        )
        return

    ctx = Context(name="headless")
    if adapter is None:
        raise ValueError("resume 继续对话需要 adapter")
    apply_retry_planner(ctx)
    install_compaction(ctx)
    install_tool_result_pruner(ctx)
    install_jobs(ctx)
    install_system_prompt(ctx)
    store = install_sessions(ctx)
    # 三段式（对齐 headless / 上游 agent-loop 工厂）：prepare → 构造 loop →
    # publish（enter + announce + agent/session-start；店成员资格归 loop）
    loop = AgentLoop(session, adapter, default_tools(ctx), ctx)
    loop.publish()
    first_seq = session.seq
    try:
        loop.followup(task)
        for ev in session.events[base_count:]:
            pers.append(session_id, dict(ev))
        pers.flush()

        text, reason = summarize(session.events, first_seq)
        stdout.write(text + "\n")
        if reason is not None and reason.get("kind") == "error":
            error = reason.get("error") or {}
            stderr.write(f"dsh: {error.get('code', 'UNKNOWN')}: {error.get('message', '')}\n")
        exit_fn(0 if (reason is not None and reason.get("kind") == "completed") else 1)
    except Exception as error:  # 对齐 headless：意外失败 stderr 一行 + exit(1)
        stderr.write(f"dsh: {error}\n")
        exit_fn(1)
    finally:
        loop.dispose()


def delete_session(session_id: str, root: Path, stdout: Any, stderr: Any) -> None:
    pers = JsonlPersistence(root)
    path = pers.path_of(session_id)
    if path is None:
        stderr.write(f"error: session {session_id!r} not found\n")
        sys.exit(1)
    path.unlink()
    # 清掉可能已空的会话目录（含项目目录）
    sess_dir = path.parent
    if sess_dir.exists() and not any(sess_dir.iterdir()):
        sess_dir.rmdir()
        proj_dir = sess_dir.parent
        if proj_dir != root and proj_dir.exists() and not any(proj_dir.iterdir()):
            proj_dir.rmdir()
    stdout.write(f"deleted {session_id}\n")


def sessions_main(argv: list[str], adapter: LlmAdapter | None = None, root: Path | None = None) -> None:
    root = root or sessions_root()
    if not argv or argv[0] in ("list", "ls"):
        print_list(root, sys.stdout)
        return
    cmd, rest = argv[0], argv[1:]
    if cmd == "resume":
        if not rest:
            sys.stderr.write('error: usage: miniharness sessions resume <id> [task...]\n')
            sys.exit(1)
        session_id = rest[0]
        pers = JsonlPersistence(root)
        if pers.path_of(session_id) is None:
            sys.stderr.write(f"error: session {session_id!r} not found\n")
            sys.exit(1)
        task = " ".join(rest[1:])
        if task.strip() and adapter is None:
            try:
                adapter = DeepSeekAdapter()
            except LlmFailure as e:
                sys.stderr.write(f"dsh: {e.failure['code']}: {e.failure['message']}\n")
                sys.exit(1)
        resume_session(session_id, task, root=root, adapter=adapter)
        return
    if cmd == "delete":
        if not rest or len(rest) != 1:
            sys.stderr.write('error: usage: miniharness sessions delete <id>\n')
            sys.exit(1)
        delete_session(rest[0], root, sys.stdout, sys.stderr)
        return
    sys.stderr.write(f"error: unknown sessions subcommand {cmd!r} (list | resume <id> [task...] | delete <id>)\n")
    sys.exit(1)


if __name__ == "__main__":
    sessions_main(sys.argv[1:])