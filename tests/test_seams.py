"""第 6 章验收：进阶接缝（沙箱 / 凭据 / 子 agent）。运行：python -m unittest discover -s tests -t ."""

import os
import unittest

from miniharness.bus import Context
from miniharness.llm import FakeLlmAdapter
from miniharness.loop import AgentLoop
from miniharness.seams import (
    CommandConsumer,
    EnvCredentialProvider,
    InProcessSubAgentProvider,
    PassthroughSandbox,
    ReadOnlySandbox,
)
from miniharness.session import Session
from miniharness.tools import Tool, ToolRegistry


class TestSandboxSeam(unittest.TestCase):
    def test_passthrough_runs(self):
        consumer = CommandConsumer(PassthroughSandbox())
        out = consumer.run("echo hello")
        self.assertIn("hello", out)

    def test_readonly_denies_writes(self):
        consumer = CommandConsumer(ReadOnlySandbox())
        with self.assertRaises(PermissionError):
            consumer.run("rm -rf /tmp/x")
        # 只读命令仍然放行
        out = consumer.run("echo safe")
        self.assertIn("safe", out)

    def test_provider_swap_consumer_unchanged(self):
        # Consumer 代码不变，只换 Provider —— 行为整体迁移
        passthrough = CommandConsumer(PassthroughSandbox())
        readonly = CommandConsumer(ReadOnlySandbox())
        cmd = "echo a > tmp_evil.txt"
        with self.assertRaises(PermissionError):
            readonly.run(cmd)
        # 同一 Consumer 契约在 passthrough 下可执行（写操作演示，勿在真实环境执行）
        self.assertEqual(passthrough.run("echo ok"), "ok")


class TestCredentialsSeam(unittest.TestCase):
    def test_env_based_resolution(self):
        os.environ["MY_TEST_KEY"] = "sk-test-123"
        try:
            creds = EnvCredentialProvider(mapping={"api_key": "MY_TEST_KEY"})
            self.assertEqual(creds.resolve("api_key"), "sk-test-123")
        finally:
            os.environ.pop("MY_TEST_KEY", None)

    def test_missing_credential_fails(self):
        os.environ.pop("SURELY_NOT_SET_KEY", None)
        creds = EnvCredentialProvider(mapping={"api_key": "SURELY_NOT_SET_KEY"})
        with self.assertRaises(KeyError):
            creds.resolve("api_key")


class TestSubAgentSeam(unittest.TestCase):
    def test_in_process_subagent(self):
        def make_loop(system_prompt):
            session = Session(f"sub-{id(system_prompt)}")
            ctx = Context()
            reg = ToolRegistry(ctx)
            reg.register(Tool(name="bash", description="d", execute=lambda a, e: "ok"))
            return AgentLoop(session, FakeLlmAdapter(final_text=f"子任务完成（{system_prompt[:4]}）"), reg, ctx, system_prompt=system_prompt)

        provider = InProcessSubAgentProvider(make_loop)
        sub = provider.spawn("researcher", "你是一个研究员")
        out = sub.run("查一下资料")
        self.assertIn("子任务完成", out)
        # 换 Provider（此处为演示同一接口的另一个实现）—— Consumer 不变
        out2 = provider.spawn("coder", "你是一个程序员").run("写个函数")
        self.assertIn("子任务完成", out2)


if __name__ == "__main__":
    unittest.main()