"""goal round 续跑提示渲染。

上游对照：packages/goal/goal-round-driver/src/prompt.ts（renderGoalRoundPrompt，
逐字一致）。消息经 Agent.followup() 入队，source 携带
{kind:'goal', goalId, revision, round} 供重放校验。
"""
from __future__ import annotations

import json

__all__ = ["render_goal_round_prompt"]


def render_goal_round_prompt(goal: dict, round: int) -> list:
    """渲染单个 text 块：同会话内继续推进当前目标的下一个轮次。"""
    return [{
        "type": "text",
        "text": "<goal_round>\n"
                f"Objective: {json.dumps(goal['objective'])}\n"
                f"Round: {round}/{goal['maxGoalRounds']}\n\n"
                "Continue working toward the objective in this same session. Treat the current workspace, "
                "tool results, and durable session state as authoritative; inspect them instead of assuming "
                "earlier narration is still current. Make concrete progress and verify the result. Before "
                "claiming completion, gather evidence that the whole objective is achieved, read the current "
                "goal, and mark it complete. If work remains, leave the goal active for the next round. Follow "
                "the configured goal-tool policy before reporting a blocker.\n"
                "</goal_round>",
    }]
