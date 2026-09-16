"""guard 守卫族单元测试（timeout-policy 常量 + repeat-tool-reminder 链语义）。

运行：python -m unittest tests.test_guard -v
"""
from __future__ import annotations

import json
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.scope import Context
from miniharness.core.session import Session, create_message, text_block
from miniharness.core.tools import Tool, ToolExec, ToolRegistry
from miniharness.llm import FakeLlmAdapter, LlmAdapter, StreamChunk
from miniharness.guard.repeat_tool_reminder import (
    Config,
    _GENTLE_REMINDER,
    _validate_thresholds,
    canonicalize,
    install_repeat_tool_reminder,
    preview_arguments,
    wildcard_to_regexp,
)
from miniharness.core.tool_timeout import TOOL_TIMEOUT


class TestCanonicalize(unittest.TestCase):
    def test_same_order(self):
        self.assertEqual(canonicalize({"b": 1, "a": 2}), '{"a": 2, "b": 1}')

    def test_nested_sort(self):
        self.assertEqual(canonicalize({"c": [3, 1], "a": {"z": 0}}),
                         '{"a": {"z": 0}, "c": [3, 1]}')

    def test_non_object_unchanged(self):
        self.assertEqual(canonicalize("literal"), '"literal"')
        self.assertEqual(canonicalize([3, 1]), '[3, 1]')


class TestPreviewArguments(unittest.TestCase):
    def test_under_cap_unchanged(self):
        s = '{"cmd":"ls"}'
        self.assertEqual(preview_arguments(s, 500), s)

    def test_over_cap_truncated(self):
        s = '{"cmd":"' + "x" * 600 + '"}'
        result = preview_arguments(s, 10)
        self.assertTrue(result.startswith(s[:10]))
        self.assertIn("… (+", result)
        self.assertIn("more chars)", result)


class TestValidateThresholds(unittest.TestCase):
    def test_empty_raises(self):
        with self.assertRaises(ValueError):
            _validate_thresholds([])

    def test_non_int_raises(self):
        with self.assertRaises(ValueError):
            _validate_thresholds([2.5])

    def test_below_two_raises(self):
        with self.assertRaises(ValueError):
            _validate_thresholds([1, 3])

    def test_duplicates_raises(self):
        with self.assertRaises(ValueError):
            _validate_thresholds([3, 3, 5])

    def test_sorted(self):
        self.assertEqual(_validate_thresholds([5, 3, 8]), [3, 5, 8])


class TestWildcardToRegexp(unittest.TestCase):
    def test_exact_match(self):
        pat = wildcard_to_regexp("bash")
        self.assertTrue(pat.match("bash"))
        self.assertIsNone(pat.match("bash_session"))

    def test_star_wildcard(self):
        pat = wildcard_to_regexp("mcp_*")
        self.assertTrue(pat.match("mcp_fetch"))
        self.assertTrue(pat.match("mcp_"))
        self.assertIsNone(pat.match("not_mcp"))

    def test_metachars_escaped(self):
        pat = wildcard_to_regexp("a.b(c)")
        self.assertTrue(pat.match("a.b(c)"))
        self.assertIsNone(pat.match("a_bXc_"))


class _StubAgent:
    pass


def _exec(agent=_StubAgent(), tool_name="bash", args=None):
    e = ToolExec()
    e.agent = agent
    e.name = tool_name
    e.arguments = args if args is not None else {"cmd": "ls"}
    return e


def _post_payload(exec_=None, tool="bash"):
    return {"tool": tool, "result": "ok", "exec": exec_ or _exec()}


def _deny_payload(exec_=None, tool="bash"):
    e = exec_ or _exec()
    return {"tool": tool, "args": e.arguments, "exec": e}


class TestRepeatToolReminder(unittest.IsolatedAsyncioTestCase):

    def setUp(self):
        self.ctx = Context()
        self.agent_a = _StubAgent()
        self.agent_b = _StubAgent()

    def _install(self, **cfg_kw):
        install_repeat_tool_reminder(self.ctx, Config(**cfg_kw) if cfg_kw else Config())

    # --- 基础链：count 1→2→3 触发 gentle --- #

    async def test_no_reminder_below_threshold(self):
        self._install(thresholds=[3])
        self.ctx.on("tools/post-execute", lambda p, nxt: {"kind": "accept"})
        for _ in range(2):
            d = await self.ctx.awaterfall("tools/post-execute",
                                          _post_payload(exec_=_exec(self.agent_a)))
            self.assertFalse((d or {}).get("additionalContexts"))

    async def test_gentle_at_first_threshold(self):
        self._install(thresholds=[3])
        self.ctx.on("tools/post-execute", lambda p, nxt: {"kind": "accept"})
        for _ in range(2):
            await self.ctx.awaterfall("tools/post-execute",
                                      _post_payload(exec_=_exec(self.agent_a)))
        d = await self.ctx.awaterfall("tools/post-execute",
                                      _post_payload(exec_=_exec(self.agent_a)))
        self.assertIsInstance(d, dict)
        self.assertEqual(d.get("kind"), "accept")
        contexts = d.get("additionalContexts", [])
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]["content"][0]["text"], _GENTLE_REMINDER)
        self.assertEqual(contexts[0]["source"]["plugin"], "repeat-tool-reminder")
        self.assertIn("× 3", contexts[0]["source"]["summary"])

    async def test_detailed_after_first_threshold(self):
        self._install(thresholds=[3, 5])
        self.ctx.on("tools/post-execute", lambda p, nxt: {"kind": "accept"})
        for _ in range(4):
            await self.ctx.awaterfall("tools/post-execute",
                                      _post_payload(exec_=_exec(self.agent_a)))
        d = await self.ctx.awaterfall("tools/post-execute",
                                      _post_payload(exec_=_exec(self.agent_a)))
        contexts = d["additionalContexts"]
        text = contexts[0]["content"][0]["text"]
        self.assertIn("bash", text)
        self.assertIn("consecutive_calls: 5", text)
        self.assertIn("Inspect the latest result", text)
        self.assertIn("× 5", contexts[0]["source"]["summary"])

    # --- per-agent 隔离 --- #

    async def test_independent_chains_per_agent(self):
        self._install(thresholds=[3])
        self.ctx.on("tools/post-execute", lambda p, nxt: {"kind": "accept"})
        for _ in range(3):
            await self.ctx.awaterfall("tools/post-execute",
                                      _post_payload(exec_=_exec(self.agent_a)))
        # agent_b: count=1 → no reminder
        d = await self.ctx.awaterfall("tools/post-execute",
                                      _post_payload(exec_=_exec(self.agent_b)))
        self.assertFalse((d or {}).get("additionalContexts"))

    # --- 不同参数重置链 --- #

    async def test_different_args_reset_chain(self):
        self._install(thresholds=[3])
        self.ctx.on("tools/post-execute", lambda p, nxt: {"kind": "accept"})
        await self.ctx.awaterfall("tools/post-execute",
                                  _post_payload(exec_=_exec(self.agent_a, args={"cmd": "ls"})))
        await self.ctx.awaterfall("tools/post-execute",
                                  _post_payload(exec_=_exec(self.agent_a, args={"cmd": "pwd"})))
        # key changed → count=1
        d = await self.ctx.awaterfall("tools/post-execute",
                                      _post_payload(exec_=_exec(self.agent_a, args={"cmd": "pwd"})))
        self.assertFalse((d or {}).get("additionalContexts"))

    # --- fold onto downstream block decision --- #

    async def test_fold_onto_block_decision(self):
        self._install(thresholds=[3])
        # downstream block listener registered AFTER guard → guard's next() calls block listener
        self.ctx.on("tools/post-execute", lambda p, nxt: {"kind": "block", "feedback": "blocked"})
        for _ in range(2):
            await self.ctx.awaterfall("tools/post-execute",
                                      _post_payload(exec_=_exec(self.agent_a)))
        # count=3 → gentle prepended into block
        d = await self.ctx.awaterfall("tools/post-execute",
                                      _post_payload(exec_=_exec(self.agent_a)))
        self.assertEqual(d.get("kind"), "block")
        contexts = d.get("additionalContexts", [])
        self.assertEqual(len(contexts), 1)
        self.assertEqual(contexts[0]["content"][0]["text"], _GENTLE_REMINDER)

    # --- direct exec (no agent) ignored --- #

    async def test_direct_exec_ignored(self):
        self._install(thresholds=[3])
        self.ctx.on("tools/post-execute", lambda p, nxt: {"kind": "accept"})
        for _ in range(3):
            d = await self.ctx.awaterfall("tools/post-execute",
                                          _post_payload(exec_=_exec(agent=None)))
        self.assertFalse((d or {}).get("additionalContexts"))

    # --- include/exclude --- #

    async def test_include_patterns_only_track(self):
        self._install(thresholds=[3], include=["bash"])
        self.ctx.on("tools/post-execute", lambda p, nxt: {"kind": "accept"})
        for _ in range(3):
            d = await self.ctx.awaterfall("tools/post-execute",
                                          _post_payload(exec_=_exec(self.agent_a, tool_name="echo")))
        self.assertFalse((d or {}).get("additionalContexts"))
        for _ in range(3):
            d = await self.ctx.awaterfall("tools/post-execute",
                                          _post_payload(exec_=_exec(self.agent_a)))
        self.assertTrue(d.get("additionalContexts"))

    async def test_exclude_patterns_transparent(self):
        self._install(thresholds=[3], exclude=["mcp_*"])
        self.ctx.on("tools/post-execute", lambda p, nxt: {"kind": "accept"})
        for _ in range(3):
            d = await self.ctx.awaterfall("tools/post-execute",
                                          _post_payload(exec_=_exec(self.agent_a, tool_name="mcp_fetch")))
        self.assertFalse((d or {}).get("additionalContexts"))

    # --- pre-execute deny counting --- #

    async def test_pre_execute_deny_counts(self):
        self._install(thresholds=[3])
        # downstream deny listener registered AFTER guard → guard's next() calls deny
        self.ctx.on("tools/pre-execute", lambda p, nxt: {"kind": "deny"})
        for _ in range(2):
            d = self.ctx.waterfall("tools/pre-execute",
                                   _deny_payload(exec_=_exec(self.agent_a)))
            self.assertEqual(d.get("kind"), "deny")
        # count=3: guard sees deny, attaches reminder to exec.additional_contexts
        exec_ = _exec(self.agent_a)
        d = self.ctx.waterfall("tools/pre-execute", _deny_payload(exec_=exec_))
        self.assertEqual(d.get("kind"), "deny")
        self.assertTrue(exec_.additional_contexts)
        self.assertEqual(exec_.additional_contexts[0]["content"][0]["text"],
                         _GENTLE_REMINDER)

    # --- pre-step user message resets all chains --- #

    async def test_pre_step_user_message_resets_chain(self):
        self._install(thresholds=[3])
        self.ctx.on("tools/post-execute", lambda p, nxt: {"kind": "accept"})
        for _ in range(2):
            await self.ctx.awaterfall("tools/post-execute",
                                      _post_payload(exec_=_exec(self.agent_a)))
        user_msg = create_message("user", [text_block("new instruction")])
        self.ctx.waterfall("agent/pre-step", {"messages": [user_msg]})
        for _ in range(2):
            d = await self.ctx.awaterfall("tools/post-execute",
                                          _post_payload(exec_=_exec(self.agent_a)))
            self.assertFalse((d or {}).get("additionalContexts"))
        d = await self.ctx.awaterfall("tools/post-execute",
                                      _post_payload(exec_=_exec(self.agent_a)))
        self.assertEqual(d["additionalContexts"][0]["content"][0]["text"],
                         _GENTLE_REMINDER)


class _RepeatToolAdapter(LlmAdapter):
    """前 N 次 stream() 产出同名单同上参数的 tool-call，之后产出最终文本。"""

    provider = "fake"
    model = "fake"

    def __init__(self, n, tool_name="bash", final_text="搞定。"):
        self._n = n
        self._tool_name = tool_name
        self._text = final_text
        self.calls = 0

    def resolve_model_info(self):
        return {"provider": "fake", "model": "fake", "input_modalities": ["text"]}

    async def stream(self, messages, tools, signal=None):
        self.calls += 1
        if self.calls <= self._n:
            yield StreamChunk("block-start", index=0, blockType="tool-call")
            yield StreamChunk("tool-call-delta", index=0,
                              id="call_%d" % self.calls, name=self._tool_name,
                              argumentsDelta='{"cmd":"ls"}')
            yield StreamChunk("block-end", index=0, block={
                "type": "tool-call", "id": "call_%d" % self.calls,
                "name": self._tool_name, "arguments": '{"cmd":"ls"}'})
            yield StreamChunk("finish", reason={"kind": "tool-calls"})
        else:
            yield StreamChunk("block-start", index=0, blockType="text")
            yield StreamChunk("text-delta", index=0, text=self._text)
            yield StreamChunk("block-end", index=0,
                              block={"type": "text", "text": self._text})
            yield StreamChunk("finish", reason={"kind": "stop"})


class TestRepeatToolReminderLoop(unittest.IsolatedAsyncioTestCase):
    """循环级集成：守卫安装到 loop.ctx，第三/五连等重复在会话里落 user/message。"""

    def _loop(self, adapter, thresholds=(3, 5, 8), include=None, exclude=None,
              preview=500):
        session = Session("guard-loop")
        ctx = Context()
        reg = ToolRegistry(ctx)
        reg.register(Tool(name="bash", description="d",
                          execute=lambda a, e: {"ok": "ls"}))
        install_repeat_tool_reminder(ctx, Config(thresholds=list(thresholds),
                                                 include=include or [],
                                                 exclude=exclude or [],
                                                 argumentsPreviewChars=preview))
        loop = AgentLoop(session, adapter, reg, ctx)
        return session, loop

    async def test_third_repeat_folds_reminder_into_session(self):
        session, loop = self._loop(_RepeatToolAdapter(n=5), thresholds=[3])
        text = await loop.run_async("干活")
        self.assertEqual(text, "搞定。")
        # 3 次重复后：第 3 次 tool/result 之后紧跟的 user/message = gentle 提醒
        reminders = [e["data"] for e in session.events
                     if e["type"] == "user/message"
                     and (e["data"].get("source") or {}).get("kind") == "plugin"
                     and e["data"]["source"].get("plugin") == "repeat-tool-reminder"]
        self.assertEqual(len(reminders), 1)
        self.assertEqual(reminders[0]["source"]["form"], "notice")
        self.assertIn("bash × 3", reminders[0]["source"]["summary"])
        self.assertEqual(reminders[0]["content"][0]["text"], _GENTLE_REMINDER)
        # 提醒必须位于其触发的 tool/result 之后（顺序语义）
        t3_seq = [e["seq"] for e in session.events
                  if e["type"] == "tool/call"][2]
        rem_seq = [e["seq"] for e in session.events if e is not None
                   and e["type"] == "user/message"
                   and (e["data"].get("source") or {}).get("kind") == "plugin"][0]
        self.assertGreater(rem_seq, t3_seq)
        # 模型不可见的投影事件（tool/result 消息本身）不含提醒文本（模型 1 次只见）
        # ——提醒已固化进日志，下一次请求的派生历史携带它
        derived = [m for m in _derive_messages(session)]
        self.assertTrue(any(_GENTLE_REMINDER in _flatten_text(m) for m in derived))

    async def test_unregistered_guard_no_reminder(self):
        # 未安装 guard：同样脚本，无 user/message 提醒，无行为变化
        session = Session("guard-loop")
        ctx = Context()
        reg = ToolRegistry(ctx)
        reg.register(Tool(name="bash", description="d",
                          execute=lambda a, e: {"ok": "ls"}))
        loop = AgentLoop(session, _RepeatToolAdapter(n=5), reg, ctx)
        text = await loop.run_async("干活")
        self.assertEqual(text, "搞定。")
        reminders = [e for e in session.events
                     if e["type"] == "user/message"
                     and (e["data"].get("source") or {}).get("plugin") == "repeat-tool-reminder"]
        self.assertEqual(reminders, [])


def _derive_messages(session):
    """从会话日志折叠派生模型可见消息（直接复用 session 域的 derive_messages）。"""
    from miniharness.core.session import derive_messages
    return derive_messages(session.events)


def _flatten_text(message):
    """把消息 dict 的内容块拍平成纯文本（测试道具；块可能是冻结 mappingproxy）。"""
    from collections.abc import Mapping
    blocks = message.get("content", ())
    return "\n".join(str(b.get("text", "")) for b in blocks if isinstance(b, Mapping))


class TestTimeoutPolicy(unittest.TestCase):
    def test_constant_value(self):
        self.assertEqual(TOOL_TIMEOUT, "TOOL_TIMEOUT")

    def test_install_noop(self):
        from miniharness.guard.timeout_policy import install_timeout_policy
        install_timeout_policy(Context())


class TestGuardExports(unittest.TestCase):
    def test_importable(self):
        from miniharness.guard import (
            Config,
            TOOL_TIMEOUT,
            install_repeat_tool_reminder,
            install_timeout_policy,
            name,
        )
        self.assertEqual(name, "repeat-tool-reminder")
        self.assertEqual(TOOL_TIMEOUT, "TOOL_TIMEOUT")
        self.assertTrue(callable(install_timeout_policy))


if __name__ == "__main__":
    unittest.main()