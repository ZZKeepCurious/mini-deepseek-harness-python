"""tmux-context：可选的每回合 tmux 方位上下文（对齐 packages/context/tmux-context）。

在 step 1 的符合条件尝试上，经 `ctx.shell` 跑一条只读 `tmux display-message`，确认本进程
确实位于 `$TMUX_PANE` 命名的 pane（比对 pane 的 `#{pane_tty}` 与本进程控制终端），并把
tmux session/window/pane 与 window pane-tree 布局作为持久上下文追加。仅在渲染出的稳定
状态相对上次注入发生变化时重注入；`refreshIntervalMs` 为最小间隔下限。缺 tmux 环境、
仅继承的环境、缺 `ctx.shell`、或查询失败一律 no-op（executor 拒绝 contained + warn）。
"""
from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from ..core.scope import Context
from ..core.session.message import create_message, text_block
from ..session_projection import ProjectionDefinition

__all__ = [
    "NAME",
    "FIELD_SEP",
    "TMUX_FIELDS",
    "apply_tmux_context",
    "install_tmux_context",
    "query_tmux_location",
    "render_reading",
    "render_state",
]

NAME = "tmux-context"

#: 制表分隔的 tmux 格式字段，按查询顺序（index.ts:51-60）。
TMUX_FIELDS = (
    "#{session_name}", "#{window_index}", "#{window_name}", "#{pane_index}",
    "#{pane_id}", "#{window_active}", "#{pane_active}", "#{window_layout}",
)

#: 字段分隔符：tmux 不解释格式里的 C 转义，字面两字符 `\\t` 原样输出并在此切回。
FIELD_SEP = "\\t"

#: 易变 turn/step 前导行前缀（index.ts:75）。
READING_PREFIX = "tmux location (turn "


def query_tmux_location(shell: Any, logger: Any, process_id: int, signal: Any) -> dict | None:
    """经 bash seam 读取本进程的 tmux 方位；不在真实 pane 或失败时返回 None。"""
    fmt = FIELD_SEP.join(TMUX_FIELDS)
    command = "\n".join([
        '[ -n "$TMUX_PANE" ] || exit 1',
        f"self_tty=$(ps -o tty= -p {process_id} | tr -d ' ')",
        '[ -n "$self_tty" ] || exit 1',
        "pane_tty=$(tmux display-message -t \"$TMUX_PANE\" -p '#{pane_tty}') || exit 1",
        '[ "$pane_tty" = "/dev/$self_tty" ] || exit 1',
        f"exec tmux display-message -t \"$TMUX_PANE\" -p '{fmt}'",
    ])
    try:
        result = shell.run(shell.resolve({"command": command, "signal": signal}))
    except Exception as error:  # noqa: BLE001 - 可选上下文：查询失败 only warn
        if logger is not None and hasattr(logger, "warn"):
            logger.warn(f"tmux location query failed: {error}; injecting no location this turn")
        return None
    if result.get("exitCode") != 0:
        return None
    stdout = result.get("stdout") or ""
    line = stdout.split("\n", 1)[0]
    parts = line.split(FIELD_SEP)
    if len(parts) != len(TMUX_FIELDS):
        return None
    (session_name, window_index, window_name, pane_index, pane_id,
     window_active, pane_active, window_layout) = parts
    if pane_id == "":
        return None
    return {"sessionName": session_name, "windowIndex": window_index,
            "windowName": window_name, "paneIndex": pane_index, "paneId": pane_id,
            "windowActive": window_active, "paneActive": pane_active,
            "windowLayout": window_layout}


def render_state(location: dict) -> str:
    """渲染稳定 tmux 状态块（变化抑制比较的部分，不含 turn 前导）。"""
    return (f"session {location['sessionName']}, "
            f"window {location['windowIndex']} "
            f"{json.dumps(location['windowName'], ensure_ascii=False)}, "
            f"pane {location['paneIndex']} {location['paneId']}\n"
            f"window active={location['windowActive']}, "
            f"pane active={location['paneActive']}, "
            f"layout {location['windowLayout']}")


def render_reading(location: dict, turn: int) -> str:
    """渲染完整持久读数（含易变 turn 前导）。"""
    return f"{READING_PREFIX}{turn}):\n{render_state(location)}"


def _validate_refresh(refresh: Any) -> None:
    if refresh is not None and (isinstance(refresh, bool)
                                or not isinstance(refresh, int) or refresh < 0):
        raise TypeError(
            f"tmux-context: refreshIntervalMs must be a non-negative safe integer, got {refresh!r}")


def _init_projection(header: Any = None, inherited: Any = None) -> Any:
    return None


def _apply_projection(state: Any, event: dict) -> Any:
    if event.get("type") != "user/message":
        return state
    data = event.get("data") or {}
    source = data.get("source") or {}
    if source.get("kind") != "plugin" or source.get("plugin") != NAME:
        return state
    content = data.get("content") or []
    block = content[0] if content else None
    if not isinstance(block, Mapping) or block.get("type") != "text":
        return state
    text = block.get("text") or ""
    newline = text.find("\n")
    stable = "" if newline == -1 else text[newline + 1:]
    return {"state": stable, "time": event.get("time")}


def apply_tmux_context(ctx: Context, config: dict | None = None) -> None:
    """注册投影单元与 prepend 的 pre-step 监听器（index.ts:215-264）。"""
    config = dict(config or {})
    refresh_interval = config.get("refreshIntervalMs")
    _validate_refresh(refresh_interval)

    projections = ctx.get("sessionProjections")
    if projections is None:
        raise RuntimeError(
            "tmux-context: the sessionProjections service is required (install M7 registry)")
    projections.register(ProjectionDefinition(
        "tmuxContext", init=_init_projection, apply=_apply_projection, state_version=1))

    async def _on_pre_step(payload: dict, next_fn) -> dict:
        decision = await next_fn()
        if isinstance(decision, dict) and decision.get("kind") == "reject":
            return decision
        signal = payload.get("signal")
        if signal is not None and getattr(signal, "aborted", False):
            return decision
        if payload.get("step") != 1:
            return decision
        shell = ctx.get("shell")
        if shell is None:
            return decision
        agent = payload.get("agent")
        turn = payload.get("turn")
        if agent is None or turn is None:
            return decision
        previous = projections.state_of(agent.session, "tmuxContext")
        if (refresh_interval is not None and refresh_interval > 0
                and previous is not None):
            import time
            now = int(time.time() * 1000)
            if now >= previous["time"] and now - previous["time"] < refresh_interval:
                return decision
        location = query_tmux_location(shell, getattr(ctx, "logger", None),
                                       os.getpid(), signal)
        if location is None:
            return decision
        state = render_state(location)
        if previous is not None and previous.get("state") == state:
            return decision
        text = render_reading(location, turn)
        message = create_message("user", [text_block(text)], {
            "kind": "plugin", "plugin": NAME, "form": "snapshot",
            "sections": [{"name": NAME, "text": text}]})
        messages = decision.get("messages") if isinstance(decision, dict) else None
        existing = list(messages) if isinstance(messages, list) else []
        return {**decision, "messages": [message, *existing]}

    ctx.on("agent/pre-step", _on_pre_step, prepend=True)


def install_tmux_context(ctx: Context, config: dict | None = None) -> None:
    """装配 tmux-context（幂等）。"""
    if getattr(ctx, "_miniharness_tmux_context_installed", False):
        return
    apply_tmux_context(ctx, config)
    ctx._miniharness_tmux_context_installed = True
