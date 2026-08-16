"""第 9 章测试：Agent 干预面 —— steer / inject / cancel / when_idle / run_maintenance。"""
import unittest

from miniharness.core.scope import Context
from miniharness.llm import FakeLlmAdapter
from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.session import Session, derive_messages
from miniharness.core.tools import Tool, ToolRegistry


def make_session():
    return Session("test-intervention")


def make_adapter(tool_call=None):
    return FakeLlmAdapter(tool_call=tool_call, final_text="任务完成。")


def make_tool(name, execute):
    return Tool(name=name, description=f"工具 {name}",
                execute=execute, parameters={"type": "object", "properties": {}})


class TestIntervention(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        self.registry = ToolRegistry(self.ctx)

    def build(self, adapter=None, tools=None):
        for t in (tools or []):
            self.registry.register(t)
        return AgentLoop(make_session(), adapter or make_adapter(), self.registry, self.ctx)

    # ---------- steer ----------

    def test_steer_from_idle_opens_turn(self):
        loop = self.build()
        self.assertTrue(loop.when_idle())
        loop.steer("继续")
        kinds = [e["type"] for e in loop.session.events]
        self.assertIn("turn/start", kinds)
        self.assertIn("turn/end", kinds)
        self.assertTrue(loop.when_idle())

    def test_steer_message_enters_log_after_pre_step(self):
        loop = self.build()
        loop.steer("任务一")
        msgs = [e["data"] for e in loop.session.events if e["type"] == "user/message"]
        self.assertEqual(msgs[0]["content"][0]["text"], "任务一")

    # ---------- inject ----------

    def test_inject_from_idle_does_not_open_turn(self):
        loop = self.build()
        loop.inject("背景信息")
        self.assertEqual(loop.status, "idle")
        self.assertFalse(loop._turn_open)
        self.assertEqual(len(loop.inbox), 1)
        # 无 turn 日志
        kinds = [e["type"] for e in loop.session.events]
        self.assertNotIn("turn/start", kinds)

    def test_inject_then_followup_consumes_fifo(self):
        loop = self.build()
        loop.inject("背景A")
        loop.followup("问题B")
        msgs = [e["data"] for e in loop.session.events if e["type"] == "user/message"]
        texts = [b["text"] for m in msgs for b in m["content"] if b["type"] == "text"]
        self.assertEqual(texts[:2], ["背景A", "问题B"])

    # ---------- cancel ----------

    def test_cancel_idle_noop_when_empty(self):
        loop = self.build()
        loop.cancel()
        self.assertEqual(len(loop.session.events), 0)
        self.assertTrue(loop.when_idle())

    def test_cancel_clears_inbox_but_keeps_with_flag(self):
        loop = self.build()
        loop.inject("排队中")
        loop.cancel()
        self.assertEqual(len(loop.inbox), 0)
        loop.inject("再排队")
        loop.cancel(keep_inbox=True)
        self.assertEqual(len(loop.inbox), 1)

    def test_cancel_from_tool_callback_ends_turn_aborted(self):
        # 工具执行回调里 cancel：当前 step 跑完后不再继续，turn 以 aborted 闭合
        calls = []

        def bash_exec(args, exec_ctx):
            loop.cancel("用户叫停")
            calls.append("ran")
            return "done"

        registry = self.registry
        registry.register(make_tool("bash", bash_exec))
        loop = AgentLoop(make_session(), make_adapter(tool_call={"name": "bash"}),
                         registry, self.ctx)
        loop.run("跑命令")
        self.assertEqual(calls, ["ran"])
        end = [e for e in loop.session.events if e["type"] == "turn/end"][-1]
        # 对齐上游：aborted 带 reason（AgentCancelCause，cause 传原值）
        self.assertEqual(end["data"]["reason"], {"kind": "aborted", "reason": {"kind": "用户叫停"}})
        # 取消后不再继续 step：只有一个 step/start
        starts = [e for e in loop.session.events if e["type"] == "step/start"]
        self.assertEqual(len(starts), 1)

    def test_cancel_before_followup_clears_and_no_turn(self):
        loop = self.build()
        loop.inject("将被清掉")
        loop.cancel()
        loop.followup("正常问题")
        msgs = [e["data"] for e in loop.session.events if e["type"] == "user/message"]
        texts = [b["text"] for m in msgs for b in m["content"] if b["type"] == "text"]
        self.assertEqual(texts, ["正常问题"])

    # ---------- when_idle / run_maintenance ----------

    def test_when_idle_false_during_maintenance(self):
        loop = self.build()
        self.assertTrue(loop.when_idle())
        ran = []

        def task():
            self.assertFalse(loop.when_idle())
            ran.append(1)

        loop.run_maintenance(task)
        self.assertEqual(ran, [1])
        self.assertTrue(loop.when_idle())

    def test_run_maintenance_rejects_when_running(self):
        # 同步模型下 running 只在 step 内存在：工具回调里调用必须被拒绝
        seen = {}

        def bash_exec(args, exec_ctx):
            with self.assertRaises(RuntimeError):
                loop.run_maintenance(lambda: None)
            seen["rejected"] = True
            return "done"

        self.registry.register(make_tool("bash", bash_exec))
        loop = AgentLoop(make_session(), make_adapter(tool_call={"name": "bash"}),
                         self.registry, self.ctx)
        loop.run("跑命令")
        self.assertTrue(seen["rejected"])

    def test_run_maintenance_leaves_no_session_events(self):
        loop = self.build()
        loop.run_maintenance(lambda: 42)
        self.assertEqual(len(loop.session.events), 0)


if __name__ == "__main__":
    unittest.main()