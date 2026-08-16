"""Plan 审查 UI：`exit_plan_mode` 工具 + `/plan` 命令 + 审查通道契约。

上游对照：packages/plan/plan-mode/src/index.ts:305-393（exit 工具，含 userQuestions
审查与 pendingIntent 排队）与 index.ts:269-303（/plan 命令）。

契约（与上游一致）：
  * `exit_plan_mode` 仅在 plan mode 内可用、plan 必须是带 `# ` 标题的非空 markdown；
    审查经 userQuestions 通道，批准 → 记 silent pending（narrate=False，结果已叙述），
    在下一个被接受的 in-turn pre-step 提交 plan/mode off；keep planning → 失败调用
    带反馈；用户取消（dismiss）→ 提示继续等待。
  * `/plan` 命令：`/plan off` 关闭（四态文案逐字对齐 index.ts:277-291），
    其余输入开启并向模型 steer 一句 user 消息（index.ts:293-300）。

审查通道（上游 userQuestions 的 mini 形态）：宿主提供 ctx 服务 `userQuestions`
（sync 回调对象，.ask(question, agent) -> str | None）：
  返回 APPROVE_LABEL（'Approve'）→ 批准；
  返回 KEEP_PLANNING_LABEL（'Keep planning'）→ 继续规划（无反馈）；
  返回其他字符串 → 继续规划 + 该字符串作为用户反馈；
  返回 None → 用户取消审查（对齐上游 ASK_CANCELLED）。

mini 简化（须在文档中标注）：无 canonical value（execute 直接返回模型可见文本，
即上游 output.render 的 "Plan approved — ..." 文案）；无 presentCall/presentResult
（无 UI 渲染层）；审查为同步回调（上游 async interaction.ask + signal）。
"""
from __future__ import annotations

import re
from typing import Any, Callable

from ..core.scope import Context
from ..core.tools import Tool
from .mode import PlanModeController, fold_plan_mode

__all__ = [
    "APPROVE_LABEL",
    "EXIT_PLAN_MODE",
    "EXIT_PLAN_MODE_DESCRIPTION",
    "KEEP_PLANNING_LABEL",
    "REVIEW_ID",
    "install_plan_review",
]

#: exit_plan_mode 工具名（上游 index.ts:70 同款，非激活时也保持注册——工具目录跨模式稳定）。
EXIT_PLAN_MODE = "exit_plan_mode"
#: 审查问题 id（上游 REVIEW_ID，index.ts:78）。
REVIEW_ID = "plan-review"
#: 审查选项标签（上游 index.ts:79-81）。
APPROVE_LABEL = "Approve"
KEEP_PLANNING_LABEL = "Keep planning"

EXIT_PLAN_MODE_DESCRIPTION = (
    "Use only in plan mode. Present your plan for the user's review and, on approval, "
    "leave plan mode. Send the COMPLETE plan as markdown, starting with a # heading "
    "that names it. The user may approve (carry out the plan from your next step) or "
    "keep planning — their feedback comes back in the tool result; revise and present again."
)

#: 批准后的模型可见输出（上游 output.render，index.ts:319）。
_APPROVED_TEXT = "Plan approved — plan mode exited; carry out the plan starting with your next step."
#: markdown `# ` 标题校验（上游 index.ts:327：/^#\s+\S/）。
_HEADING_PATTERN = re.compile(r"^#\s+\S")


def _exit_plan_mode_error(message: str) -> ValueError:
    """退出工具的错误统一为 ValueError（经工具管线成为 error 结果）。"""
    return ValueError(message)


def _plan_command_handler(controller: PlanModeController) -> Callable:
    """/plan 命令 handler（上游 index.ts:274-301 文案逐字对齐）。"""

    def handler(agent: Any, raw_input: str) -> dict:
        message = raw_input.strip()
        if message == "off":
            outcome = controller.set(agent, False)
            if outcome == "committed":
                return {"kind": "success", "text": "Plan mode off."}
            if outcome == "queued":
                return {"kind": "success", "text": "Leaving plan mode (applies from the next step)."}
            if outcome == "cancelled":
                return {"kind": "success", "text": "Plan mode entry cancelled."}
            # noop：仅真正 inactive 的会话读幂等；排队中的退出重复选择同文案（上游同分支）
            return {
                "kind": "success",
                "text": "Leaving plan mode (applies from the next step)."
                if fold_plan_mode(agent.session.events)
                else "Plan mode is already inactive.",
            }
        outcome = controller.set(agent, True)
        if message != "":
            agent.steer(message)
        return {
            "kind": "success",
            "text": "Plan mode on. Use /plan off to leave."
            if outcome == "committed"
            else "Entering plan mode (applies from the next step). Use /plan off to leave.",
        }

    return handler


def install_plan_review(ctx: Context, controller: PlanModeController) -> None:
    """装配 plan 审查 UI：注册 exit_plan_mode 工具 + /plan 命令（命令可选）。

    要求 ctx 已提供 tools 服务（先 install ToolRegistry；缺失抛 KeyError，fail
    loud）。commands 服务为可选（上游 ctx.get('commands') 语义，缺失则命令不可用）。
    """
    tools = ctx.inject("tools")

    def execute(args: dict, exec: Any) -> str:
        agent = exec.agent
        if agent is None:
            raise _exit_plan_mode_error(
                f"{EXIT_PLAN_MODE} requires a calling agent (no session to switch)")
        if not fold_plan_mode(agent.session.events):
            raise _exit_plan_mode_error(f"{EXIT_PLAN_MODE} is only available in plan mode")
        plan = args.get("plan", "")
        if not _HEADING_PATTERN.match(plan.strip()):
            raise _exit_plan_mode_error(
                f"{EXIT_PLAN_MODE} requires a non-empty markdown plan starting with a # heading")
        try:
            channel = ctx.inject("userQuestions")
        except KeyError:
            raise _exit_plan_mode_error(
                "no user-questions channel is available to review the plan; "
                "ask the user to switch the session mode instead")
        question = {
            "id": REVIEW_ID,
            "header": "Plan review",
            "question": "Approve this plan and leave plan mode?",
            "detail": plan,
            "options": [
                {"label": APPROVE_LABEL, "description": "Leave plan mode; the plan is carried out from the next step."},
                {"label": KEEP_PLANNING_LABEL, "description": "Stay in plan mode; feedback goes back to the model."},
            ],
            "intent": {"kind": "plan-review", "approve": APPROVE_LABEL},
        }
        answer = channel.ask(question, agent)
        if answer is None:
            raise _exit_plan_mode_error(
                "The user dismissed the plan review to speak instead; stay in plan mode, "
                "stop here, and wait for their message.")
        if answer != APPROVE_LABEL:
            if answer == KEEP_PLANNING_LABEL:
                raise _exit_plan_mode_error(
                    "The user chose to keep planning; revise the plan and present it again.")
            raise _exit_plan_mode_error(
                f"The user chose to keep planning; their feedback: {answer}")
        # 批准：排队 silent 选择（narrate=False），下次被接受的 in-turn pre-step 提交
        controller._queue_exit(agent)
        return _APPROVED_TEXT

    tools.register(Tool(
        name=EXIT_PLAN_MODE,
        description=EXIT_PLAN_MODE_DESCRIPTION,
        parameters={
            "type": "object",
            "properties": {
                "plan": {
                    "type": "string",
                    "description": "The complete plan, as markdown, starting with a # heading that names it.",
                },
            },
            "required": ["plan"],
        },
        execute=execute,
    ))

    try:
        commands = ctx.inject("commands")
    except KeyError:
        return
    commands.register(
        "plan",
        "Enter or leave plan mode",
        _plan_command_handler(controller),
        input_hint="[off|message]",
    )
