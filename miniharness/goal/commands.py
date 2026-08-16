"""`/goal` 人类命令：在持久化目标域上的命令表面。

上游对照：packages/goal/command-goal/src/index.ts（parseGoalCommand / renderGoal /
commandHint / executeGoalCommand，文案逐字对齐）。
"""
from __future__ import annotations

from typing import Any, Callable

from .domain import GoalError

__all__ = ["GOAL_USAGE", "install_goal_commands", "parse_goal_command"]

GOAL_USAGE = "Usage: /goal [<objective>|clear|edit <objective>|pause|resume]"


def parse_goal_command(raw_input: str) -> dict:
    """解析 /goal 文法；任意其它输入视为 objective（上游 parseGoalCommand）。"""
    input_ = (raw_input or "").strip()
    if input_ == "":
        return {"kind": "show"}
    control = input_.lower()
    if control == "clear":
        return {"kind": "clear"}
    if control == "pause":
        return {"kind": "pause"}
    if control == "resume":
        return {"kind": "resume"}
    if control == "edit":
        return {"kind": "invalid-edit"}
    if input_.startswith("edit") and len(input_) > 4 and input_[4].isspace():
        return {"kind": "edit", "objective": input_[4:].strip()}
    return {"kind": "create", "objective": input_}


def _phase_label(phase: str) -> str:
    return {
        "active": "active",
        "paused": "paused",
        "blocked": "blocked",
        "complete": "complete",
    }[phase]


def _command_hint(goal: dict) -> str:
    if goal["phase"] == "active":
        if goal["activation"] == "armed":
            return "/goal edit <objective>, /goal pause, /goal clear"
        return "/goal edit <objective>, /goal resume, /goal clear"
    if goal["phase"] in ("paused", "blocked"):
        return "/goal edit <objective>, /goal resume, /goal clear"
    return "/goal <objective>, /goal clear"


def _render_goal(title: str, goal: dict) -> dict:
    reason = goal.get("blockedReason")
    blocker = [] if reason is None else [f"Blocker: {reason['code']}: {reason['message']}"]
    return {
        "kind": "success",
        "text": "\n".join([
            title,
            f"Status: {_phase_label(goal['phase'])}",
            *blocker,
            f"Objective: {goal['objective']}",
            f"Rounds: {goal['roundsStarted']}/{goal['maxGoalRounds']}",
            f"Activation: {goal['activation']}",
            "",
            f"Commands: {_command_hint(goal)}",
        ]),
    }


def _missing_goal(action: str) -> dict:
    return {
        "kind": "error",
        "text": f"No goal is currently set; /goal {action} requires one. {GOAL_USAGE}",
    }


def _execute_goal_command(goals, agent: Any, raw_input: str) -> dict:
    command = parse_goal_command(raw_input)
    try:
        current = goals.get(agent)
        kind = command["kind"]
        if kind == "show":
            if current is None:
                return {"kind": "success", "text": f"No goal is currently set.\n{GOAL_USAGE}"}
            return _render_goal("Goal", current)
        if kind == "invalid-edit":
            return {"kind": "error",
                    "text": f"Goal editing requires a replacement objective.\n{GOAL_USAGE}"}
        if kind == "create":
            if current is not None and current["phase"] != "complete":
                return {"kind": "error",
                        "text": f"A goal is already {_phase_label(current['phase'])}. "
                                "Use /goal edit <objective> to change it or /goal clear before replacing it."}
            return _render_goal("Goal created",
                                goals.create(agent, {"objective": command["objective"]}))
        if kind == "edit":
            if current is None:
                return _missing_goal("edit")
            if current["phase"] == "complete":
                return _render_goal("Goal created",
                                    goals.create(agent, {"objective": command["objective"]}))
            return _render_goal("Goal updated",
                                goals.edit(agent, {"id": current["id"], "revision": current["revision"]},
                                           {"objective": command["objective"]}))
        if kind == "pause":
            if current is None:
                return _missing_goal("pause")
            return _render_goal("Goal paused",
                                goals.pause(agent, {"id": current["id"], "revision": current["revision"]}))
        if kind == "resume":
            if current is None:
                return _missing_goal("resume")
            return _render_goal("Goal resumed",
                                goals.resume(agent, {"id": current["id"], "revision": current["revision"]}))
        if kind == "clear":
            if current is None:
                return {"kind": "success", "text": "No goal to clear."}
            goals.clear(agent, {"id": current["id"], "revision": current["revision"]})
            return {"kind": "success", "text": "Goal cleared."}
        raise ValueError(f"unknown goal command kind: {kind}")
    except GoalError:
        return {"kind": "error",
                "text": "The goal command is not valid for the current state. Run /goal to view available commands."}


def install_goal_commands(ctx, goals) -> Callable | None:
    """注册 `/goal` 命令（可选：无 commands 服务时返回 None）。

    上游 command-goal inject ['commands', 'goals']；mini 经 ctx.inject 鸭子类型，
    无 commands 服务 = 命令不可用（不注册、不报错）。
    """
    try:
        commands = ctx.inject("commands")
    except KeyError:
        return None
    return commands.register(
        "goal",
        "set or view the goal for a long-running task",
        lambda agent, raw: _execute_goal_command(goals, agent, raw),
        input_hint="[<objective>|clear|edit <objective>|pause|resume]",
    )
