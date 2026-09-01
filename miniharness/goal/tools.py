"""`get_goal` / `create_goal` / `update_goal` 模型工具：持久化同会话目标域。

上游对照：packages/goal/tool-goal/src/index.ts（工具名、description、参数 schema、
错误码与文案逐字对齐；blocked 阈值策略 authority.kind==='goal-round' 用
"当前 step 在 goal 轮次中"近似判定）。

契约（与上游一致）：
  * 三个工具的 canonical 输出都是紧凑 Native JSON：`{goal: null}` 或
    `{goal: {id, revision, objective, phase, roundsStarted, maxGoalRounds,
    blockedReason?}, activation}`；activation 是进程内观察，不重放。
  * update_goal 是 compare-and-set：goal_id/revision 必须精确命中当前目标
    （GOAL_TOOL_INVALID_UPDATE 提示文案逐字）；action 专属参数校验；
    blocked 带 {code:'model-reported', message}；goal 轮次内未达
    blockedAfterConsecutiveRounds（默认 3）即报 blocked → GOAL_TOOL_BLOCK_THRESHOLD。
  * canonical value + render 分离（对齐上游 GOAL_OUTPUT.schema + output.render，
    index.ts:175-178）：execute 返回结构化 GoalToolValue（dict），render 把
    canonical 值转成模型可见单 text 块（JSON.stringify 等价）。
  * 工具策略以 systemPrompt section 'tool:goal'（order 114）注入。

mini 简化（须在文档中标注）：无 completionAuthority 权威模块（子代理/人类直答
判定未复现），requireDirectHuman 省略；deferContext wrapup 摘要注入未复现。
"""
from __future__ import annotations

import json
from typing import Any

from ..core.scope import Context
from ..core.tools import Tool, ToolRegistry
from .domain import GoalError

__all__ = ["GOAL_POLICY_SECTION", "register_goal_tools"]

GOAL_POLICY_SECTION = "tool:goal"
GOAL_POLICY_ORDER = 114
DEFAULT_BLOCKED_AFTER_ROUNDS = 3

_UPDATE_ACTIONS = ["edit", "pause", "resume", "complete", "blocked"]

CREATE_DESCRIPTION = (
    "Create one persisted same-session completion goal when the current direct human request "
    "is a long-running objective that should continue across autonomous goal rounds. You may "
    "infer that intent without requiring the user to say \"create a goal\". Do not use this for "
    "trivial single-turn work. Execution rejects non-human and subagent authority."
)

GET_DESCRIPTION = (
    "Read the current same-session goal, including its exact id/revision, objective, phase, completed "
    "continuation rounds, round limit, blocker reason when present, and whether another continuation is armed. "
    "Call this before updating a goal."
)

UPDATE_DESCRIPTION = (
    "Update the exact current goal revision. edit, pause, and resume require a direct "
    "top-level human request. During an automatic continuation of the current goal, complete "
    "and blocked are also allowed. blocked is rejected before the configured minimum round count; the model remains "
    "responsible for judging that the same condition persisted across those rounds and must explain it in blocked_reason."
)

_GOAL_VALUE_SCHEMA = {
    "oneOf": [
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {"goal": {"type": "null", "required": True}},
        },
        {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "goal": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": True,
                    "properties": {
                        "id": {"type": "string", "required": True},
                        "revision": {"type": "integer", "required": True},
                        "objective": {"type": "string", "required": True},
                        "phase": {"type": "string", "required": True,
                                  "enum": ["active", "paused", "blocked", "complete"]},
                        "roundsStarted": {"type": "integer", "required": True},
                        "maxGoalRounds": {"type": "integer", "required": True},
                        "blockedReason": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "code": {"type": "string", "required": True},
                                "message": {"type": "string", "required": True},
                            },
                        },
                    },
                },
                "activation": {"type": "string", "required": True, "enum": ["armed", "disarmed"]},
            },
        },
    ],
}


def _guidance(blocked_after: int) -> str:
    return ("Use goal tools for one long-running completion objective in the current session. "
            "create_goal may infer goal intent from a direct human request in any language; do not "
            "create a goal for routine single-turn work. Call get_goal before update_goal and copy its "
            "exact goal_id and revision. After session resume or fork, an active goal is disarmed: when "
            "a human asks to continue or resume in any wording or language, use update_goal action "
            "resume to rearm it. Mark complete only when the objective is actually achieved. Mark "
            f"blocked only after the same blocking condition persists for at least {blocked_after} "
            "consecutive goal rounds, and report that concrete condition in blocked_reason; difficulty, uncertainty, "
            "or useful remaining work is not blocked.")


def goal_value(goal: dict | None) -> dict:
    """规范输出（上游 goalValue）：activation 是观察，非重放状态。"""
    if goal is None:
        return {"goal": None}
    result = {
        "goal": {key: goal[key] for key in
                 ("id", "revision", "objective", "phase", "roundsStarted", "maxGoalRounds")},
        "activation": goal["activation"],
    }
    if goal.get("blockedReason") is not None:
        result["goal"]["blockedReason"] = dict(goal["blockedReason"])
    return result


def render_goal_value(value: dict) -> list[dict]:
    """canonical GoalToolValue → 模型可见单 text 块（对齐上游 output.render）。

    上游 `render: (args, value) => [{type:'text', text: JSON.stringify(value)}]`
    （tool-goal/src/index.ts:177）；mini 以 `json.dumps` 等价，保持既有模型可见输出。
    """
    return [{"type": "text", "text": json.dumps(value)}]


def _require_agent(exec_: Any, name: str) -> Any:
    if getattr(exec_, "agent", None) is None:
        raise GoalError(f"{name} requires a calling agent (no session to update)",
                        "GOAL_TOOL_INVALID_UPDATE")
    return exec_.agent


def _goal_ref(goal_id: Any, revision: Any) -> dict:
    if not isinstance(goal_id, str) or goal_id == "" or goal_id != goal_id.strip() \
            or not isinstance(revision, int) or isinstance(revision, bool) or revision < 1:
        raise GoalError(
            "goal_id must be non-empty and revision must be a positive safe integer",
            "GOAL_TOOL_INVALID_UPDATE")
    return {"id": goal_id, "revision": revision}


def _has_text(value) -> bool:
    return isinstance(value, str) and value != ""


def _has_round_cap(value) -> bool:
    return value is not None and value != 0


def _in_goal_round(agent: Any) -> bool:
    """近似 authority.kind==='goal-round'：最近一条 user 消息是 goal 来源。"""
    for event in reversed(agent.session.events):
        if event["type"] == "user/message":
            return (event["data"].get("source") or {}).get("kind") == "goal"
    return False


def register_goal_tools(reg: ToolRegistry, goals, ctx: Context | None = None,
                        blocked_after: int = DEFAULT_BLOCKED_AFTER_ROUNDS) -> None:
    """注册三个目标工具；ctx 提供 systemPrompt 时挂 'tool:goal' 策略节（order 114）。

    上游 tool-goal inject ['agents','goals','tools','systemPrompt']；mini 经
    ctx.get('systemPrompt') 可选（无 ctx 或缺失该服务则策略节不挂）。
    """
    if blocked_after < 1:
        raise TypeError("blockedAfterConsecutiveRounds must be a positive safe integer")
    if ctx is not None:
        system_prompt = ctx.get("systemPrompt")
        system_prompt.section(GOAL_POLICY_SECTION, GOAL_POLICY_ORDER,
                              lambda _context: _guidance(blocked_after))

    async def get_goal(args: dict, exec_: Any) -> dict:
        agent = _require_agent(exec_, "get_goal")
        return goal_value(goals.get(agent))

    async def create_goal(args: dict, exec_: Any) -> dict:
        agent = _require_agent(exec_, "create_goal")
        request = {"objective": args.get("objective", "")}
        if args.get("max_goal_rounds") is not None:
            request["maxGoalRounds"] = args["max_goal_rounds"]
        return goal_value(goals.create(agent, request))

    async def update_goal(args: dict, exec_: Any) -> dict:
        agent = _require_agent(exec_, "update_goal")
        ref = _goal_ref(args.get("goal_id"), args.get("revision"))
        replacements = {}
        if _has_text(args.get("objective")):
            replacements["objective"] = args["objective"]
        if _has_round_cap(args.get("max_goal_rounds")):
            replacements["maxGoalRounds"] = args["max_goal_rounds"]
        action = args.get("action")
        if action not in _UPDATE_ACTIONS:
            raise GoalError(f"invalid action {action!r}; expected "
                            + " | ".join(_UPDATE_ACTIONS), "GOAL_TOOL_INVALID_UPDATE")
        blocked_reason = args.get("blocked_reason")
        if action == "edit":
            if _has_text(blocked_reason):
                raise GoalError("blocked_reason is valid only with action blocked",
                                "GOAL_TOOL_INVALID_UPDATE")
            goal = goals.edit(agent, ref, replacements)
            return goal_value(goal)
        if action in ("pause", "resume"):
            if _has_text(args.get("objective")) or _has_round_cap(args.get("max_goal_rounds")) \
                    or _has_text(blocked_reason):
                raise GoalError(
                    "objective and max_goal_rounds are valid only with action edit; "
                    "blocked_reason is valid only with action blocked",
                    "GOAL_TOOL_INVALID_UPDATE")
            goal = goals.pause(agent, ref) if action == "pause" else goals.resume(agent, ref)
            return goal_value(goal)
        if _has_text(args.get("objective")) or _has_round_cap(args.get("max_goal_rounds")):
            raise GoalError("objective and max_goal_rounds are valid only with action edit",
                            "GOAL_TOOL_INVALID_UPDATE")
        if action == "complete":
            if _has_text(blocked_reason):
                raise GoalError("blocked_reason is valid only with action blocked",
                                "GOAL_TOOL_INVALID_UPDATE")
            return goal_value(goals.complete(agent, ref))
        # blocked
        if not _has_text(blocked_reason):
            raise GoalError("blocked_reason is required with action blocked",
                            "GOAL_TOOL_INVALID_UPDATE")
        if _in_goal_round(agent):
            view = goals.get(agent)
            if view is not None and view["roundsStarted"] < blocked_after:
                raise GoalError(
                    f"blocked requires at least {blocked_after} consecutive goal rounds; "
                    f"current round is {view['roundsStarted']}",
                    "GOAL_TOOL_BLOCK_THRESHOLD")
        goal = goals.block(agent, ref, {"code": "model-reported", "message": blocked_reason})
        return goal_value(goal)

    reg.register(Tool(
        name="get_goal",
        description=GET_DESCRIPTION,
        parameters={"type": "object", "properties": {}},
        output=_GOAL_VALUE_SCHEMA,
        render=render_goal_value,
        execute=get_goal,
    ))
    reg.register(Tool(
        name="create_goal",
        description=CREATE_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "objective": {
                    "type": "string",
                    "description": "The concrete completion objective inferred from the direct human request.",
                },
                "max_goal_rounds": {
                    "type": "number",
                    "description": "Optional positive safe-integer limit on automatic continuation rounds.",
                },
            },
            "required": ["objective"],
        },
        output=_GOAL_VALUE_SCHEMA,
        render=render_goal_value,
        execute=create_goal,
    ))
    reg.register(Tool(
        name="update_goal",
        description=UPDATE_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "goal_id": {"type": "string", "description": "Exact id returned by get_goal."},
                "revision": {"type": "number", "description": "Exact positive revision returned by get_goal."},
                "action": {"type": "string", "enum": _UPDATE_ACTIONS,
                           "description": "edit | pause | resume | complete | blocked"},
                "objective": {"type": "string", "description": "Replacement objective; valid only with action edit."},
                "max_goal_rounds": {"type": "number", "description": "Replacement cap; valid only with action edit."},
                "blocked_reason": {"type": "string",
                                   "description": "Concrete blocking condition; required only with action blocked."},
            },
            "required": ["goal_id", "revision", "action"],
        },
        output=_GOAL_VALUE_SCHEMA,
        render=render_goal_value,
        execute=update_goal,
    ))
