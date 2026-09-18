"""语义持久化检查点策略（session-checkpoint-policy）。

对照上游 `tests/session-checkpoint-policy.spec.ts`：模型请求 / 顶层工具 /
步边界三种屏障 + 取消落在检查点窗口 + 嵌套复用 + 生命周期拆解。
"""
import unittest

from miniharness.core.scope import Context
from miniharness.core.session import Session, create_message
from miniharness.core.session_store import (
    SessionCheckpointError,
    SessionStore,
    install_sessions,
)
from miniharness.core.tools import Tool, ToolExec, ToolResult, ToolRegistry, run_pipeline
from miniharness.seams.session_checkpoint import (
    CHECKPOINT_ABORTED_CODE,
    install_checkpoint_policy,
)


class _Agent:
    def __init__(self, session):
        self.session = session


class _FlushRecorder:
    """会话 flush 参与者：记录调用并可按需阻塞/失败。"""

    def __init__(self, ctx, *, gate=None, fail=None):
        self.order = []
        self._gate = gate
        self._fail = fail
        ctx.on("session/flush", self._on_flush)

    def _on_flush(self, payload):
        self.order.append("flush:start")
        if self._fail is not None:
            raise self._fail
        self.order.append("flush:end")


class _AbortSignal:
    def __init__(self, aborted=False):
        self._aborted = aborted

    @property
    def aborted(self):
        return self._aborted

    def is_set(self):
        return self._aborted


def _echo_tool():
    state = {"ran": False}

    def execute(args, exec_):
        state["ran"] = True
        return ToolResult(ok=True, content="done")

    tool = Tool(name="echo", description="echo", parameters={}, execute=execute)
    return tool, state


class CheckpointPolicyTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="root")
        self.store = install_sessions(self.ctx)
        self.session = self.store.create("session")
        self.agent = _Agent(self.session)

    def _install(self):
        return install_checkpoint_policy(self.ctx)

    def test_checkpoint_fails_closed_without_participant(self):
        with self.assertRaises(SessionCheckpointError):
            self.store.checkpoint(self.session)

    def test_request_boundary_checkpoints(self):
        recorder = _FlushRecorder(self.ctx)
        self._install()
        self.ctx.waterfall("agent/checkpoint",
                           {"agent": self.agent, "boundary": "request"})
        self.assertEqual(recorder.order, ["flush:start", "flush:end"])

    def test_request_boundary_failure_propagates(self):
        _FlushRecorder(self.ctx, fail=RuntimeError("disk unavailable"))
        self._install()
        with self.assertRaises(RuntimeError):
            self.ctx.waterfall("agent/checkpoint",
                               {"agent": self.agent, "boundary": "request"})

    def test_pre_step_boundary_checkpoints(self):
        recorder = _FlushRecorder(self.ctx)
        self._install()
        self.ctx.waterfall("agent/pre-step", {"agent": self.agent, "messages": []})
        self.assertEqual(recorder.order, ["flush:start", "flush:end"])

    def test_top_level_tool_checkpoints_before_body(self):
        recorder = _FlushRecorder(self.ctx)
        self._install()
        tool, state = _echo_tool()
        exec_ = ToolExec(agent=self.agent)
        result = run_pipeline(self.ctx, tool, {}, exec_)
        self.assertTrue(state["ran"])
        self.assertFalse(result.is_error)
        self.assertEqual(recorder.order, ["flush:start", "flush:end"])

    def test_cancellation_during_checkpoint_folds_to_aborted_before_dispatch(self):
        _FlushRecorder(self.ctx)
        self._install()
        tool, state = _echo_tool()
        exec_ = ToolExec(agent=self.agent, signal=_AbortSignal(aborted=True))
        result = run_pipeline(self.ctx, tool, {}, exec_)
        self.assertFalse(state["ran"])
        self.assertTrue(result.is_error)
        self.assertEqual(result.error, "Error: tool call aborted before dispatch")
        self.assertEqual(result.error_info, {"name": "AbortError",
                                            "code": CHECKPOINT_ABORTED_CODE})

    def test_rejected_checkpoint_stops_tool_without_running_body(self):
        _FlushRecorder(self.ctx, fail=RuntimeError("disk unavailable"))
        self._install()
        tool, state = _echo_tool()
        exec_ = ToolExec(agent=self.agent)
        with self.assertRaises(RuntimeError):
            run_pipeline(self.ctx, tool, {}, exec_)
        self.assertFalse(state["ran"])

    def test_nested_tool_dispatch_reuses_outer_checkpoint(self):
        recorder = _FlushRecorder(self.ctx)
        self._install()
        tool, state = _echo_tool()
        exec_ = ToolExec(agent=self.agent, parent=object())
        run_pipeline(self.ctx, tool, {}, exec_)
        self.assertTrue(state["ran"])
        self.assertEqual(recorder.order, [])

    def test_agent_less_tool_does_not_checkpoint(self):
        recorder = _FlushRecorder(self.ctx)
        self._install()
        tool, state = _echo_tool()
        run_pipeline(self.ctx, tool, {}, ToolExec())
        self.assertTrue(state["ran"])
        self.assertEqual(recorder.order, [])

    def test_dispose_removes_wrappers(self):
        recorder = _FlushRecorder(self.ctx)
        dispose = self._install()
        self.ctx.waterfall("agent/checkpoint",
                           {"agent": self.agent, "boundary": "request"})
        self.assertEqual(len(recorder.order), 2)
        dispose()
        self.ctx.waterfall("agent/checkpoint",
                           {"agent": self.agent, "boundary": "request"})
        self.assertEqual(len(recorder.order), 2)


class CheckpointStoreTest(unittest.TestCase):
    def test_checkpoint_reports_participation(self):
        ctx = Context(name="root")
        store = install_sessions(ctx)
        session = store.create("s")
        ctx.on("session/flush", lambda payload: None)
        store.checkpoint(session)  # 有参与者：成功

    def test_flush_without_participant_returns_false(self):
        ctx = Context(name="root")
        store = install_sessions(ctx)
        session = store.create("s")
        self.assertFalse(store.flush(session))


if __name__ == "__main__":
    unittest.main()
