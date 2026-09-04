"""P2-19 验收：loop 侧 runtime-context 投影。

上游对照：packages/core/agent-loop/src/runtime-context.ts +
tests/runtime-context.spec.ts（restore 忽略被 replace 遮蔽的快照并继续向前找、
project('retained', []) 无更新、快照消息带 form:'snapshot' + sections、
cleared 哨兵不带归因）+ agent.ts preStep 接线（assemble →
renderContextSections → project → 默认进入把快照追加在 claimed 之后，
监听器显式 enter 决策整体接管 messages）。运行：python -m unittest discover -s tests -t .
"""
from __future__ import annotations

from collections.abc import Mapping
import unittest

from miniharness.core.agent_loop.agent import AgentLoop
from miniharness.core.agent_loop.runtime_context import (
    CLEARED,
    SOURCE,
    RuntimeContextProjection,
)
from miniharness.core.scope import Context
from miniharness.core.session import Session, create_message, text_block
from miniharness.core.system_prompt import install_system_prompt
from miniharness.core.tools import ToolRegistry
from miniharness.llm import FakeLlmAdapter


def _owned(text: str) -> dict:
    return create_message(
        "user", [text_block(text)], {"kind": "plugin", "plugin": SOURCE})


def _prune_over(session: Session, seq: int, summary: str) -> None:
    """压缩式 replace：以一条 user 检查点消息遮蔽 seq 所在节点（V2：压缩
    检查点是 user/message——assistant/message 内嵌流、禁带 sourceEventSeqs；
    检查点 source 非 runtime-context owned，不影响快照追踪）。"""
    session.append(
        "user/message",
        create_message("user", [text_block(summary)],
                       {"kind": "plugin", "plugin": "compact"}),
        surfaceOp={"op": "replace", "start": seq, "end": seq},
        sourceEventSeqs=[seq],
    )


class RestoreTest(unittest.TestCase):
    """构造期恢复：倒序找最近一条仍在 surface 上的 owned 快照。"""

    def test_no_snapshot_ever_and_empty_current_is_none(self):
        p = RuntimeContextProjection(Session("s1"))
        self.assertIsNone(p.project("", []))

    def test_restored_visible_snapshot_dedups_same_text(self):
        s = Session("s1")
        s.append("user/message", _owned("v1"), surfaceOp="append")
        p = RuntimeContextProjection(s)
        self.assertIsNone(p.project("v1", []))

    def test_restore_skips_shadowed_latest_finds_older_visible(self):
        s = Session("s1")
        s.append("user/message", _owned("v1"), surfaceOp="append")
        s.append("user/message", _owned("v2"), surfaceOp="append")
        _prune_over(s, 1, "压缩摘要")
        p = RuntimeContextProjection(s)
        # 最新 owned(v2) 被遮蔽 → 继续向前找到 v1：同文本去重命中
        self.assertIsNone(p.project("v1", []))

    def test_all_shadowed_is_null_not_never_so_clear_still_cast(self):
        s = Session("s1")
        s.append("user/message", _owned("v1"), surfaceOp="append")
        _prune_over(s, 0, "压缩摘要")
        p = RuntimeContextProjection(s)
        # retained=null（≠ undefined）：空 current 也必须铸 CLEARED
        msg = p.project("", [])
        self.assertIsNotNone(msg)
        self.assertEqual(msg["content"][0]["text"], CLEARED)

    def test_foreign_plugin_messages_do_not_seed_retained(self):
        s = Session("s1")
        s.append("user/message", create_message(
            "user", [text_block("普通输入")], {"kind": "user"}), surfaceOp="append")
        s.append("user/message", create_message(
            "user", [text_block("别的插件")], {"kind": "plugin", "plugin": "other"}),
            surfaceOp="append")
        p = RuntimeContextProjection(s)
        # 从未有过 owned 快照（undefined）+ 空 current → None
        self.assertIsNone(p.project("", []))


class ProjectTest(unittest.TestCase):
    """project()：三态去重 / CLEARED 哨兵 / sections 归因。"""

    def test_first_snapshot_attributes_sections(self):
        p = RuntimeContextProjection(Session("s1"))
        msg = p.project("v1", [{"name": "sandbox:policy", "text": "v1"}])
        self.assertEqual(msg["role"], "user")
        self.assertEqual(msg["content"], [{"type": "text", "text": "v1"}])
        self.assertEqual(msg["source"]["kind"], "plugin")
        self.assertEqual(msg["source"]["plugin"], SOURCE)
        self.assertEqual(msg["source"]["form"], "snapshot")
        self.assertEqual(msg["source"]["sections"],
                         [{"name": "sandbox:policy", "text": "v1"}])

    def test_same_text_dedups_only_after_commit(self):
        """retained 只随权威日志更新（上游"不拥有提交"）：投影未落盘前
        重复 project 仍铸新候选；落盘后同文本命中去重。"""
        s = Session("s1")
        p = RuntimeContextProjection(s)
        first = p.project("v1", [{"name": "a", "text": "v1"}])
        self.assertIsNotNone(p.project("v1", [{"name": "a", "text": "v1"}]))
        s.append("user/message", first, surfaceOp="append")
        self.assertIsNone(p.project("v1", [{"name": "a", "text": "v1"}]))

    def test_cleared_marker_has_plain_source(self):
        s = Session("s1")
        s.append("user/message", _owned("v1"), surfaceOp="append")
        p = RuntimeContextProjection(s)
        msg = p.project("", [])
        self.assertEqual(msg["content"][0]["text"], CLEARED)
        self.assertEqual(msg["source"],
                         {"kind": "plugin", "plugin": SOURCE})

    def test_follow_log_incrementally(self):
        s = Session("s1")
        p = RuntimeContextProjection(s)
        first = p.project("v1", [{"name": "a", "text": "v1"}])
        s.append("user/message", first, surfaceOp="append")   # 模拟 step 内落盘
        self.assertIsNone(p.project("v1", [{"name": "a", "text": "v1"}]))
        # replace 遮蔽 retained → 下次投影转 CLEARED（上游监听体：
        # replacement && sourceEventSeqs 含 retained.seq → null）
        _prune_over(s, s.events[-1]["seq"], "压缩摘要")
        cleared = p.project("", [])
        self.assertEqual(cleared["content"][0]["text"], CLEARED)
        s.append("user/message", cleared, surfaceOp="append")
        # retained.text == CLEARED → 再次为空不重复铸
        self.assertIsNone(p.project("", []))


class LoopIntegrationTest(unittest.TestCase):
    """agent.ts preStep 接线：快照作为 durable user 消息进入对话流。"""

    def _env(self):
        session = Session("rt")
        ctx = Context()
        reg = ToolRegistry(ctx)
        svc = install_system_prompt(ctx)
        state = {"text": ""}
        svc.context("t:state", 10, lambda c: state["text"])
        adapter = FakeLlmAdapter(final_text="好")
        loop = AgentLoop(session, adapter, reg, ctx)
        return session, loop, svc, state

    @staticmethod
    def _snapshots(session):
        return [e for e in session.events if e["type"] == "user/message"
                and isinstance(e["data"].get("source"), Mapping)
                and e["data"]["source"].get("plugin") == SOURCE]

    @staticmethod
    def _plain_user_seqs(session):
        return [e["seq"] for e in session.events if e["type"] == "user/message"
                and e["data"].get("source") == {"kind": "user"}]

    def test_snapshot_appended_after_claimed_input(self):
        session, loop, _, state = self._env()
        state["text"] = "策略 A"
        loop.followup("你好")
        snaps = self._snapshots(session)
        self.assertEqual(len(snaps), 1)
        self.assertLess(self._plain_user_seqs(session)[-1], snaps[0]["seq"])
        src = snaps[0]["data"]["source"]
        self.assertEqual(src["form"], "snapshot")
        self.assertEqual([dict(s) for s in src["sections"]],
                         [{"name": "t:state", "text": "策略 A"}])

    def test_unchanged_context_not_reinjected_next_turn(self):
        session, loop, _, state = self._env()
        state["text"] = "策略 A"
        loop.followup("第一问")
        loop.followup("第二问")
        self.assertEqual(len(self._snapshots(session)), 1)

    def test_changed_context_casts_new_snapshot(self):
        session, loop, _, state = self._env()
        state["text"] = "策略 A"
        loop.followup("第一问")
        state["text"] = "策略 B"
        loop.followup("第二问")
        snaps = self._snapshots(session)
        self.assertEqual(len(snaps), 2)
        texts = [e["data"]["content"][0]["text"] for e in snaps]
        self.assertEqual(texts, ["Current runtime context. "
                                 "This snapshot supersedes earlier "
                                 "runtime-context snapshots.\n\n策略 A",
                                 "Current runtime context. "
                                 "This snapshot supersedes earlier "
                                 "runtime-context snapshots.\n\n策略 B"])

    def test_suppression_casts_cleared_marker(self):
        session, loop, svc, state = self._env()
        state["text"] = "策略 A"
        loop.followup("第一问")
        svc.suppress_runtime_context()   # 保持激活（scope effect 未撤销）
        loop.followup("第二问")
        snaps = self._snapshots(session)
        self.assertEqual(len(snaps), 2)
        second = snaps[1]["data"]
        self.assertEqual(second["content"][0]["text"], CLEARED)
        self.assertEqual(second["source"], {"kind": "plugin", "plugin": SOURCE})

    def test_explicit_enter_decision_takes_over_without_snapshot(self):
        session, loop, _, state = self._env()
        state["text"] = "策略 A"
        loop.ctx.on("agent/pre-step",
                    lambda payload, nxt: {"kind": "enter",
                                          "messages": payload["messages"]})
        loop.followup("你好")
        self.assertEqual(len(self._snapshots(session)), 0)
        self.assertTrue(loop.last_response())


if __name__ == "__main__":
    unittest.main()
