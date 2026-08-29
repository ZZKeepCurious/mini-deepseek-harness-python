"""`miniharness presets` 子命令：预设名单 / 投影 / 选择 / 删除（mini 教学扩展）。

上游无对应 CLI 子命令：preset 作为 Remote 服务（list / read / copyPreset /
deletePreset / selectPreset）暴露给浏览器 GUI（packages/preset/agent-presets/
src/index.ts 的 @Remote）。mini 未复现 web 表层，故以子命令提供等价的本地管理
入口。契约不变：
  * 投影 = 分层 root 解析（project_preset）+ 会话投影（project_session_agent_preset）
  * select 落在"已开始"的会话 → PresetLockedError 拒绝（对齐上游 swap 锁定语义）
  * delete 非 user trust（shipped 等）→ PresetNotWritableError（对齐 authoring）

契约标注：
  * mini 本模块不落 'agent-preset/selected'（持久化层归属 web 会话域，见
    core/session/types.py 的 KNOWN_TYPES）；select 只做校验 + 打印投影确认，
    落盘留给会话层。select <id> 带会话 id → 读日志做锁定检查；不带 → 只做
    分层投影（锁定检查等待会话层落地时执行）。
  * 缺省 preset 可用 MINIHARNESS_DEFAULT_PRESET 环境变量覆盖（mini 教学扩展：
    上游缺省由 settings 服务热载，mini 无该表层）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

from ..core.session import Session
from ..core.session.persistence import JsonlPersistence, repair_and_replay
from ..preset.presets import (
    PresetLockedError,
    PresetNotWritableError,
    UnknownPresetError,
    default_roster,
    delete_preset,
    project_preset,
    project_session_agent_preset,
    select_preset,
)
from .session_cmds import sessions_root


def _cli_roster(environ: dict | None = None):
    env = os.environ if environ is None else environ
    default = env.get("MINIHARNESS_DEFAULT_PRESET") or "standard"
    return default_roster(env, default=default)


def _cmd_list(stdout: Any) -> None:
    roster = _cli_roster()
    rows = roster.rows()
    if not rows:
        stdout.write("no presets found\n")
        return
    for row in rows:
        flags = []
        if row.get("isDefault"):
            flags.append("default")
        if row.get("broken"):
            flags.append(f"broken: {row['broken']}")
        line = f"{row['id']}\t{row.get('trust')}\t{row.get('name', '')}"
        if flags:
            line += "\t[" + ", ".join(flags) + "]"
        stdout.write(line.rstrip() + "\n")
    stdout.write(f"authorable: {'yes' if roster.authorable else 'no'}\n")


def _cmd_show(preset_id: str | None, stdout: Any, stderr: Any) -> None:
    roster = _cli_roster()
    try:
        proj = project_preset(roster, preset_id)
    except (UnknownPresetError, ValueError) as e:
        stderr.write(f"error: {e}\n")
        sys.exit(1)
        return
    p = proj.preset
    stdout.write(f"{proj.id}  trust={proj.trust}  default={proj.is_default}\n")
    stdout.write(f"  source: {proj.source_root}\n")
    stdout.write(f"  name: {p.name}\n")
    if p.description:
        stdout.write(f"  description: {p.description}\n")
    stdout.write(f"  tools: {', '.join(p.tools) or '-'}\n")
    pc = p.persona
    stdout.write(
        f"  persona: complete={pc.complete} "
        f"include_runtime_context={pc.include_runtime_context}\n"
    )
    if p.broken:
        stdout.write(f"  broken: {p.broken}\n")


def _cmd_select(preset_id: str, session_id: str | None, stdout: Any, stderr: Any) -> None:
    roster = _cli_roster()
    if session_id is None:
        try:
            proj = project_preset(roster, preset_id)
        except (UnknownPresetError, ValueError) as e:
            stderr.write(f"error: {e}\n")
            sys.exit(1)
            return
        stdout.write(
            f"selected {proj.id} (trust={proj.trust}; "
            f"no session given, blank-check deferred to session layer)\n"
        )
        return
    root = sessions_root()
    pers = JsonlPersistence(root)
    if pers.path_of(session_id) is None:
        stderr.write(f"error: session {session_id!r} not found\n")
        sys.exit(1)
        return
    try:
        session = repair_and_replay(pers, session_id, Session(session_id))
    except RuntimeError as e:
        stderr.write(f"error: {e}\n")
        sys.exit(1)
        return
    current = project_session_agent_preset(session.events)
    try:
        proj = select_preset(roster, session.events, preset_id, session_id=session_id)
    except PresetLockedError as e:
        stderr.write(f"error: {e}\n")
        sys.exit(1)
        return
    except UnknownPresetError as e:
        stderr.write(f"error: {e}\n")
        sys.exit(1)
        return
    if current is None:
        current_note = "none recorded"
    else:
        current_note = current
    stdout.write(
        f"selected {proj.id} (trust={proj.trust}; session {session_id} "
        f"projected {current_note}) — durable append is the session layer's job\n"
    )


def _cmd_delete(preset_id: str, stdout: Any, stderr: Any) -> None:
    roster = _cli_roster()
    try:
        delete_preset(roster, preset_id)
    except UnknownPresetError as e:
        stderr.write(f"error: {e}\n")
        sys.exit(1)
        return
    except PresetNotWritableError as e:
        stderr.write(f"error: {e}\n")
        sys.exit(1)
        return
    stdout.write(f"deleted {preset_id}\n")


def presets_main(argv: list[str], stdout: Any | None = None, stderr: Any | None = None) -> None:
    stdout = stdout or sys.stdout
    stderr = stderr or sys.stderr
    if not argv or argv[0] in ("list", "ls"):
        _cmd_list(stdout)
        return
    cmd, rest = argv[0], argv[1:]
    if cmd == "show":
        if len(rest) > 1:
            stderr.write("error: usage: miniharness presets show [preset-id]\n")
            sys.exit(1)
            return
        _cmd_show(rest[0] if rest else None, stdout, stderr)
        return
    if cmd == "select":
        if not rest or len(rest) > 2:
            stderr.write("error: usage: miniharness presets select <preset-id> [session-id]\n")
            sys.exit(1)
            return
        _cmd_select(rest[0], rest[1] if len(rest) == 2 else None, stdout, stderr)
        return
    if cmd == "delete":
        if len(rest) != 1:
            stderr.write("error: usage: miniharness presets delete <preset-id>\n")
            sys.exit(1)
            return
        _cmd_delete(rest[0], stdout, stderr)
        return
    stderr.write(
        f"error: unknown presets subcommand {cmd!r} "
        f"(list | show [id] | select <id> [session] | delete <id>)\n"
    )
    sys.exit(1)


if __name__ == "__main__":
    presets_main(sys.argv[1:])