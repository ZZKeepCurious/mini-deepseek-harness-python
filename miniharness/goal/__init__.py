"""goal 域：持久化同会话目标 + 自动续跑 round 驱动。

上游对照：packages/goal/{goal, goal-round-driver, tool-goal, command-goal}。

装配顺序（示例 examples/plan_goal_demo.py）：
  install_system_prompt(ctx) → install_goals(ctx) → register_goal_tools(reg, goals, ctx)
  → install_goal_driver(ctx, goals) → install_goal_commands(ctx, goals)
"""
from __future__ import annotations

from .commands import GOAL_USAGE, install_goal_commands
from .domain import (
    GOAL_CHANGE_VERSION,
    GoalError,
    apply_goal_change,
    apply_goal_event,
    decode_goal_change,
    fold_goal,
    goal_change_ref,
)
from .driver import GoalDriver, install_goal_driver
from .prompt import render_goal_round_prompt
from .service import DEFAULT_MAX_GOAL_ROUNDS, GoalService, install_goals
from .tools import register_goal_tools

__all__ = [
    "DEFAULT_MAX_GOAL_ROUNDS",
    "GOAL_CHANGE_VERSION",
    "GOAL_USAGE",
    "GoalDriver",
    "GoalError",
    "GoalService",
    "apply_goal_change",
    "apply_goal_event",
    "decode_goal_change",
    "fold_goal",
    "goal_change_ref",
    "install_goal_commands",
    "install_goal_driver",
    "install_goals",
    "render_goal_round_prompt",
    "register_goal_tools",
]
