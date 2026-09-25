"""tmux-context：tmux 方位查询、渲染与变化抑制。

对齐 packages/context/tmux-context（index.ts 的确定性面）。
"""

import os
import unittest

from miniharness.context.tmux_context import (
    FIELD_SEP,
    NAME,
    install_tmux_context,
    query_tmux_location,
    render_reading,
    render_state,
)
from miniharness.core.scope import Context
from miniharness.core.session_store import SessionStore
from miniharness.session_projection import install_session_projections

FIELDS = ["main", "0", "editor", "0", "%1", "1", "1", "layout-abc"]
LINE = FIELD_SEP.join(FIELDS)


class _FakeExecution:
    def __init__(self, result):
        self._result = result

    def result(self):
        result = dict(self._result)
        for channel in ("stdout", "stderr"):
            if isinstance(result.get(channel), str):
                result[channel] = {"text": result[channel], "truncated": False}
        return result


class FakeShell:
    def __init__(self, result=None, error=None):
        self.result = result if result is not None else {"exitCode": 0, "stdout": LINE + "\n",
                                                          "stderr": ""}
        self.error = error

    def resolve(self, request):
        return dict(request)

    def execute(self, spec):
        if self.error is not None:
            raise self.error
        return _FakeExecution(self.result)


class FakeLogger:
    def __init__(self):
        self.warnings = []

    def warn(self, message):
        self.warnings.append(message)


class TestQuery(unittest.TestCase):
    def test_parses_matching_pane(self):
        location = query_tmux_location(FakeShell(), None, 42, None)
        self.assertEqual(location["sessionName"], "main")
        self.assertEqual(location["paneId"], "%1")
        self.assertEqual(location["windowLayout"], "layout-abc")

    def test_nonzero_exit_is_not_in_a_pane(self):
        shell = FakeShell({"exitCode": 1, "stdout": "", "stderr": ""})
        self.assertIsNone(query_tmux_location(shell, None, 42, None))

    def test_wrong_field_count_is_ignored(self):
        shell = FakeShell({"exitCode": 0, "stdout": "main\\t0\n", "stderr": ""})
        self.assertIsNone(query_tmux_location(shell, None, 42, None))

    def test_empty_pane_id_is_ignored(self):
        line = FIELD_SEP.join(["main", "0", "editor", "0", "", "1", "1", "layout"])
        shell = FakeShell({"exitCode": 0, "stdout": line + "\n", "stderr": ""})
        self.assertIsNone(query_tmux_location(shell, None, 42, None))

    def test_executor_rejection_is_contained_and_warned(self):
        logger = FakeLogger()
        self.assertIsNone(query_tmux_location(FakeShell(error=RuntimeError("denied")),
                                              logger, 42, None))
        self.assertTrue(logger.warnings)


class TestRender(unittest.TestCase):
    def test_state_and_reading(self):
        location = {"sessionName": "main", "windowIndex": "0", "windowName": "editor",
                    "paneIndex": "0", "paneId": "%1", "windowActive": "1",
                    "paneActive": "1", "windowLayout": "layout-abc"}
        self.assertEqual(
            render_state(location),
            'session main, window 0 "editor", pane 0 %1\n'
            "window active=1, pane active=1, layout layout-abc")
        self.assertEqual(render_reading(location, 3),
                         'tmux location (turn 3):\n' + render_state(location))


class TmuxContextCase(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="tmux-context-test")
        self.addCleanup(self.ctx.dispose)
        self.projections = install_session_projections(self.ctx)
        self.store = SessionStore(self.ctx)
        self.session = self.store.create("s1", {"meta": {"cwd": os.getcwd()}})
        self.shell = FakeShell()
        self.ctx.provide("shell", self.shell)

    def _pre_step(self, step=1, turn=1):
        from miniharness.core.agent_loop.resident_loop import run_on_resident

        agent = type("Agent", (), {"session": self.session, "id": "s1"})()
        payload = {"messages": [], "agent": agent, "turn": turn, "step": step, "signal": None}
        return run_on_resident(self.ctx.awaterfall("agent/pre-step", payload))

    def test_injects_only_on_first_step_and_prepends(self):
        install_tmux_context(self.ctx, {})
        first = self._pre_step(step=1)
        self.assertEqual(len(first["messages"]), 1)
        message = first["messages"][0]
        self.assertEqual(message["source"]["kind"], NAME)
        self.assertIn("tmux location (turn 1):", message["content"][0]["text"])
        self.assertEqual(self._pre_step(step=2)["messages"], [])

    def test_same_state_is_suppressed_and_change_reinjects(self):
        install_tmux_context(self.ctx, {})
        first = self._pre_step(step=1)
        self.session.append("user/message", first["messages"][0], surfaceOp="append")
        self.assertEqual(self._pre_step(step=1)["messages"], [])
        self.shell.result = {"exitCode": 0,
                             "stdout": FIELD_SEP.join(
                                 ["main", "1", "editor", "0", "%1", "1", "1", "layout-abc"]) + "\n",
                             "stderr": ""}
        reinjected = self._pre_step(step=1)
        self.assertEqual(len(reinjected["messages"]), 1)

    def test_missing_shell_is_a_no_op(self):
        bare = Context(name="no-shell")
        self.addCleanup(bare.dispose)
        install_session_projections(bare)
        install_tmux_context(bare, {})
        session = SessionStore(bare).create("s1", {"meta": {"cwd": os.getcwd()}})
        from miniharness.core.agent_loop.resident_loop import run_on_resident

        agent = type("Agent", (), {"session": session, "id": "s1"})()
        decision = run_on_resident(bare.awaterfall("agent/pre-step", {
            "messages": [], "agent": agent, "turn": 1, "step": 1, "signal": None}))
        self.assertEqual(decision["messages"], [])

    def test_projection_fold_records_stable_state(self):
        install_tmux_context(self.ctx, {})
        first = self._pre_step(step=1)
        self.session.append("user/message", first["messages"][0], surfaceOp="append")
        state = self.projections.state_of(self.session, "tmuxContext")
        self.assertEqual(state["state"], render_state({
            "sessionName": "main", "windowIndex": "0", "windowName": "editor",
            "paneIndex": "0", "paneId": "%1", "windowActive": "1",
            "paneActive": "1", "windowLayout": "layout-abc"}))
        self.assertIsNotNone(state["time"])

    def test_invalid_refresh_interval_fails_loud(self):
        with self.assertRaises(TypeError):
            install_tmux_context(Context(name="bad"), {"refreshIntervalMs": -1})


if __name__ == "__main__":
    unittest.main()
