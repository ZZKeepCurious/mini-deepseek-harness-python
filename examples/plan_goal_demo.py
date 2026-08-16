"""plan 模式 + goal 目标的端到端示例（FakeLlmAdapter，无需 API key）。

演示（议题 8）：
  * `/plan` 命令进出 plan mode；模型经 `exit_plan_mode` 工具提交计划，
    userQuestions 审查通道批准后静默退出（下次被接受的 pre-step 提交）。
  * `/goal` 命令 show/create/pause/resume/clear；模型经 `create_goal` 工具建目标；
    GoalDriver.continue_rounds 自动续跑轮次；`update_goal complete` 收尾。

计划审查会交互询问（输入 a/k/回车=取消）；`--approve` 参数则自动批准（无头运行）。
运行：python examples/plan_goal_demo.py [--approve]
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from miniharness import (
    Context,
    Session,
    StreamChunk,
    ToolRegistry,
)
from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.commands import install_commands, route_command
from miniharness.core.system_prompt import install_system_prompt
from miniharness.goal import (
    install_goal_commands,
    install_goal_driver,
    install_goals,
    register_goal_tools,
)
from miniharness.llm import FakeLlmAdapter
from miniharness.plan import APPROVE_LABEL, KEEP_PLANNING_LABEL, install_plan_mode, install_plan_review

PLAN_MARKDOWN = (
    "# 修复 CI 构建\n\n"
    "1. 定位构建脚本失败点\n"
    "2. 补充依赖锁定并重跑流水线\n"
    "3. 冒烟验收\n"
)


class ScriptedAdapter(FakeLlmAdapter):
    """按脚本出牌的假模型：step 依次消费；callable step 用 messages 现算参数。

    step 形如 {"text": ...} / {"tool": name, "args": {...}} /
    {"tool": name, "args_fn": callable(messages) -> {...}}。
    """

    def __init__(self, steps):
        self._steps = list(steps)
        self.calls = 0

    def stream(self, messages, tools):
        self.calls += 1
        step = self._steps.pop(0) if self._steps else {"text": "（脚本结束）"}
        if "text" in step:
            text = step["text"]
            yield StreamChunk("block-start", index=0, blockType="text")
            yield StreamChunk("text-delta", index=0, text=text)
            yield StreamChunk("block-end", index=0, block={"type": "text", "text": text})
            yield StreamChunk("finish", reason={"kind": "stop"})
        else:
            name = step["tool"]
            args = step["args"] if "args" in step else step["args_fn"](messages)
            arguments = json.dumps(args, ensure_ascii=False)
            yield StreamChunk("block-start", index=0, blockType="tool-call")
            yield StreamChunk("tool-call-delta", index=0, id="call_0",
                              name=name, argumentsDelta=arguments)
            yield StreamChunk("block-end", index=0, block={
                "type": "tool-call", "id": "call_0", "name": name, "arguments": arguments,
            })
            yield StreamChunk("finish", reason={"kind": "tool-calls"})


def _created_goal_ref(session):
    """从 create_goal 的 tool/result 文本里取回 {id, revision}（脚本取运行期真值）。"""
    for event in reversed(session.events):
        if event["type"] != "tool/result":
            continue
        block = event["data"]["message"]["content"][0]
        if block.get("type") != "tool-result":
            continue
        try:
            value = json.loads(block["content"][0]["text"])
        except (KeyError, TypeError, ValueError):
            continue
        if isinstance(value, dict) and isinstance(value.get("goal"), dict) \
                and "id" in value["goal"]:
            return {"id": value["goal"]["id"], "revision": value["goal"]["revision"]}
    raise RuntimeError("create_goal 未执行")


class ReviewChannel:
    """审查通道：自动批准（--approve）或交互询问。

    ask() 返回 APPROVE_LABEL / KEEP_PLANNING_LABEL / 反馈文本 / None（取消）。
    """

    def __init__(self, auto_approve=False):
        self._auto = auto_approve

    def ask(self, question, agent):
        if self._auto:
            return APPROVE_LABEL
        print("\n--- 计划审查 ---")
        print(question["detail"])
        print("选项: [Approve] / [Keep planning] / 输入反馈 / 回车=取消")
        try:
            choice = input("→ ").strip()
        except EOFError:
            return None
        if choice == "":
            return None
        if choice.lower().startswith("a"):
            return APPROVE_LABEL
        if choice.lower().startswith("k"):
            return KEEP_PLANNING_LABEL
        return choice


def _build(adapter, auto_approve=False):
    ctx = Context(name="plan-goal-demo")
    install_system_prompt(ctx)
    install_commands(ctx)
    controller = install_plan_mode(ctx, {"section": "Plan first, then act."})
    reg = ToolRegistry(ctx)
    install_plan_review(ctx, controller)
    goals = install_goals(ctx)
    register_goal_tools(reg, goals, ctx)
    driver = install_goal_driver(ctx, goals)
    install_goal_commands(ctx, goals)
    ctx.provide("userQuestions", ReviewChannel(auto_approve))
    loop = AgentLoop(Session("plan-goal-demo"), adapter, reg, ctx)
    return ctx, controller, reg, loop, goals, driver


def main():
    parser = argparse.ArgumentParser(description="plan + goal 端到端示例")
    parser.add_argument("--approve", action="store_true",
                        help="自动批准计划审查（无头运行）")
    args = parser.parse_args()

    script = [
        {"text": PLAN_MARKDOWN},                                     # /plan steer 回合
        {"tool": "exit_plan_mode", "args": {"plan": PLAN_MARKDOWN}},  # 计划审查
        {"text": "计划已批准，开始执行。"},                              # 提交 plan/mode off
        {"tool": "create_goal",                                       # 建目标
         "args": {"objective": "修复 CI 构建失败", "max_goal_rounds": 3}},
        {"text": "目标已创建，开始自动续跑。"},
    ]
    ctx, controller, reg, loop, goals, driver = _build(ScriptedAdapter(script), args.approve)

    print("=== plan 模式：/plan 命令 ===")
    print(route_command("/plan off", loop, ctx))
    print(route_command("/plan 请规划 CI 修复方案", loop, ctx))

    print("\n=== plan 审查：exit_plan_mode 工具 ===")
    print("答复:", loop.run("请把你的计划提交给用户审阅。"))
    print(route_command("/plan off", loop, ctx))

    print("\n=== goal 目标：命令与工具 ===")
    print(route_command("/goal", loop, ctx))
    print("答复:", loop.run("为 CI 构建失败创建一个持久目标并自动续跑。"))
    print(route_command("/goal", loop, ctx))

    print("\n=== goal 自动续跑：GoalDriver.continue_rounds ===")
    ref = _created_goal_ref(loop.session)
    loop.adapter = ScriptedAdapter([
        {"text": "已定位：构建脚本缺少依赖锁定，先补 lockfile。"},   # round 1
        {"tool": "update_goal",                                     # round 2 收尾
         "args": {"goal_id": ref["id"], "revision": ref["revision"], "action": "complete"}},
        {"text": "CI 已修复，目标完成。"},
    ])
    ran = driver.continue_rounds(loop, max_rounds=3)
    print(f"续跑 {ran} 轮")
    print(route_command("/goal", loop, ctx))

    print("\n=== goal 命令生命周期 ===")
    print(route_command("/goal 部署到预发环境", loop, ctx))
    print(route_command("/goal pause", loop, ctx))
    print(route_command("/goal resume", loop, ctx))
    print(route_command("/goal clear", loop, ctx))
    print(route_command("/goal", loop, ctx))

    print(f"\n=== 日志：事件 {len(loop.session.events)} 条，"
          f"goal/change x{sum(1 for e in loop.session.events if e['type'] == 'goal/change')} ===")
    for event in loop.session.events:
        if event["type"] in ("plan/mode", "goal/change"):
            data = event["data"]
            if event["type"] == "plan/mode":
                print("  plan/mode:", data)
            else:
                goal = data.get("goal", {})
                print("  goal/change:", data["operation"], goal.get("phase", ""),
                      goal.get("objective", "") or data.get("cleared", {}).get("id", ""))


if __name__ == "__main__":
    main()
