"""真实 API 集成测试（打 integration 标签，CI 默认跳过）。

运行条件（缺一即跳过）：
  - 环境变量 MINIHARNESS_INTEGRATION=1
  - 环境变量 DEEPSEEK_API_KEY 有效

本地运行：
  $env:MINIHARNESS_INTEGRATION=1; python -m unittest tests.test_real_api -v
"""
import os
import unittest

from miniharness.core.scope import Context
from miniharness.cli.default_tools import default_tools
from miniharness.llm import DeepSeekAdapter, LlmFailure
from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.session import Session

INTEGRATION = os.environ.get("MINIHARNESS_INTEGRATION") == "1"
HAS_KEY = bool(os.environ.get("DEEPSEEK_API_KEY"))


@unittest.skipUnless(INTEGRATION and HAS_KEY, "需要 MINIHARNESS_INTEGRATION=1 与 DEEPSEEK_API_KEY")
class TestRealApi(unittest.TestCase):
    def test_single_turn_completes(self):
        ctx = Context(name="real-api")
        session = Session("real-api-test")
        loop = AgentLoop(session, DeepSeekAdapter(), default_tools(ctx), ctx)
        loop.followup("用一句话回答：2+2 等于几？")
        last = next(
            ev for ev in reversed(session.events)
            if ev["type"] == "turn/end"
        )
        self.assertEqual(last["data"]["reason"]["kind"], "completed")

    def test_turns_balanced(self):
        from miniharness.core.session import turn_balance

        ctx = Context(name="real-api")
        session = Session("real-api-test-2")
        loop = AgentLoop(session, DeepSeekAdapter(), default_tools(ctx), ctx)
        loop.followup("你好，请回复一个字：好")
        self.assertEqual(turn_balance(session.events), 0)


if __name__ == "__main__":
    unittest.main()