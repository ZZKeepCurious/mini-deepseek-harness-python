"""TerminalController：会话作用域、分配限额、shell 解析与沙箱围栏。

对齐 terminal-controller/tests/controller.spec.ts 的确定性面；真实 PTY 走 e2e 冒烟。
"""

import sys
import unittest

from miniharness.core.scope import Context
from miniharness.seams.sandbox_policy import SandboxPolicyService, effective_sandbox_mode
from miniharness.terminal.types import Cancellation
from miniharness.terminal_controller.index import install_terminal_controller
from miniharness.terminal_controller.shells import (
    SubprocessExecutableNotFoundError,
    discover_shells,
    resolve_executable,
    resolve_shell,
)
from miniharness.terminal_controller.types import TerminalLimitReached, TerminalUnavailable

from tests.test_terminal_controller_terminal import FakeBrowserHandle

SHELL = {"path": "/bin/bash", "name": "bash", "args": ["-i"]}


class FakeSession:
    def __init__(self, session_id="session-a", cwd="/workspace"):
        self.session_id = session_id
        self.meta = {"cwd": cwd}
        self.events = []

    def append(self, type_, data, **_kwargs):
        self.events.append({"type": type_, "data": data})


class FakeAgent:
    def __init__(self, ctx, session_id="session-a", cwd="/workspace"):
        self.id = session_id
        self.ctx = ctx
        self.session = FakeSession(session_id, cwd)


class FakeSandbox:
    def __init__(self):
        self.calls = []

    def confine(self, argv, policy, signal=None):
        self.calls.append((list(argv), dict(policy)))
        return {"argv": ["sandbox-runner", *argv]}


class TestTerminalController(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="terminal-controller-test")
        self.addCleanup(self.ctx.dispose)
        self.handles = []
        self.handle = None
        self.spawn_specs = []
        self.shell = dict(SHELL)

    def controller(self, config=None, policy_mode="danger-full-access", **opts):
        self.policy = SandboxPolicyService(self.ctx, {"mode": policy_mode})
        base = {"maxCols": 200, "maxRows": 100, "maxInputBytes": 1000, "scrollback": 100,
                "disposeGraceMs": 100}
        base.update(config or {})
        controller = install_terminal_controller(
            self.ctx, base,
            spawn_terminal=opts.pop("spawn_terminal", self._spawn),
            resolve_shell_fn=opts.pop("resolve_shell_fn", lambda configured, signal=None: self.shell),
            discover_shells_fn=opts.pop("discover_shells_fn", None),
            **opts)
        self.controller_obj = controller
        return controller

    def _spawn(self, spec):
        self.spawn_specs.append(spec)
        self.handle = FakeBrowserHandle()
        self.handles.append(self.handle)
        return self.handle

    def agent(self, session_id="session-a", cwd="/workspace"):
        return FakeAgent(self.ctx, session_id, cwd)

    def test_environment_prefers_the_session_cwd_and_keeps_the_limits(self):
        controller = self.controller()
        agent = self.agent()
        # index.ts:121 `session.header.cwd ?? sandboxPolicy.workspaceRoot`
        self.assertEqual(controller.environment(agent), {"cwd": "/workspace",
                                                         "maxInputBytes": 1000,
                                                         "maxCols": 200, "maxRows": 100,
                                                         "scrollback": 100})
        headless = self.agent(cwd=None)
        self.assertEqual(controller.environment(headless)["cwd"],
                         self.policy.resolve({"session": headless.session})["workspaceRoot"])

    def test_creates_the_shell_and_keeps_an_existing_identity(self):
        controller = self.controller()
        agent = self.agent()
        created = controller.create(agent, {"id": "terminal-1", "cols": 80, "rows": 24})
        self.assertEqual(created["shell"], SHELL)
        self.assertEqual(created["title"], "bash")
        self.assertEqual(self.spawn_specs[0]["argv"], ["/bin/bash", "-i"])
        self.assertEqual(self.spawn_specs[0]["terminalType"], "xterm-256color")
        self.assertEqual(self.spawn_specs[0]["env"], {"DSH_SESSION_ID": "session-a"})
        self.assertIs(controller.create(agent, {"id": "terminal-1", "cols": 80, "rows": 24}),
                      controller.list("session-a")[0])
        self.assertEqual(len(self.spawn_specs), 1)

    def test_scopes_terminals_by_session(self):
        controller = self.controller()
        agent = self.agent()
        controller.create(agent, {"id": "terminal-1", "cols": 80, "rows": 24})
        self.assertEqual(controller.list("other"), [])
        with self.assertRaisesRegex(TerminalUnavailable, "no longer exists"):
            controller.follow(self.agent("other"), "terminal-1", "writer")
        follow = controller.follow(agent, "terminal-1", "writer")
        self.assertEqual(follow.baseline["type"], "snapshot")
        follow.detach()
        controller.close(agent, "terminal-1")
        self.assertEqual(controller.list("session-a"), [])

    def test_close_terminates_and_closes_the_identity_for_future_creation(self):
        controller = self.controller()
        agent = self.agent()
        controller.create(agent, {"id": "terminal-1", "cols": 80, "rows": 24})
        controller.close(agent, "terminal-1")
        self.assertEqual(self.handle.terminate_calls, 1)
        with self.assertRaisesRegex(TerminalUnavailable, "closed in this Session"):
            controller.create(agent, {"id": "terminal-1", "cols": 80, "rows": 24})
        controller.close(agent, "terminal-1")  # 重复关闭成功
        controller.create(agent, {"id": "terminal-2", "cols": 80, "rows": 24})
        self.assertEqual(len(self.spawn_specs), 2)

    def test_counts_terminals_against_the_session_limit(self):
        controller = self.controller(config={"maxTerminals": 1})
        agent = self.agent()
        controller.create(agent, {"id": "terminal-1", "cols": 80, "rows": 24})
        with self.assertRaises(TerminalLimitReached) as caught:
            controller.create(agent, {"id": "terminal-2", "cols": 80, "rows": 24})
        self.assertEqual(caught.exception.details, {"limit": 1})
        self.assertEqual(len(self.spawn_specs), 1)

    def test_rejects_invalid_dimensions_before_allocation_or_resize(self):
        controller = self.controller()
        agent = self.agent()
        for cols, rows in ((1, 24), (201, 24), (80, 0), (80, 101), (1.5, 24)):
            with self.assertRaisesRegex(RuntimeError, "dimensions"):
                controller.create(agent, {"id": "terminal-1", "cols": cols, "rows": rows})
        self.assertEqual(self.spawn_specs, [])
        controller.create(agent, {"id": "terminal-1", "cols": 80, "rows": 24})
        with self.assertRaisesRegex(RuntimeError, "dimensions"):
            controller.resize(agent, "terminal-1", "writer", 201, 24)

    def test_rejects_invalid_wire_identities_and_names(self):
        controller = self.controller()
        agent = self.agent()
        with self.assertRaisesRegex(ValueError, "Invalid terminal identity"):
            controller.create(agent, {"id": "../terminal", "cols": 80, "rows": 24})
        with self.assertRaisesRegex(ValueError, "attachment identity"):
            controller.follow(agent, "terminal-1", "")
        with self.assertRaisesRegex(RuntimeError, "1–120"):
            controller.rename(agent, "terminal-1", "  ")
        with self.assertRaisesRegex(RuntimeError, "1–120"):
            controller.rename(agent, "terminal-1", "x" * 121)

    def test_routes_input_resize_and_trimmed_names_through_the_active_attachment(self):
        controller = self.controller(config={"maxInputBytes": 6})
        agent = self.agent()
        controller.create(agent, {"id": "terminal-1", "cols": 80, "rows": 24})
        follow = controller.follow(agent, "terminal-1", "writer")
        controller.write(agent, "terminal-1", "writer", "终端")
        self.assertEqual(self.handle.written, ["终端"])
        with self.assertRaisesRegex(RuntimeError, "input exceeds"):
            controller.write(agent, "terminal-1", "writer", "终端!")
        controller.resize(agent, "terminal-1", "writer", 200, 100)
        self.assertEqual(self.handle.resized, [(200, 100)])
        controller.rename(agent, "terminal-1", "  server logs  ")
        self.assertEqual(controller.list("session-a")[0]["title"], "server logs")
        follow.detach()

    def test_does_not_confine_the_shell_with_the_session_policy(self):
        # rc.1 index.ts:151：用户终端以执行环境的系统用户权限运行，不套 Agent
        # 沙箱围栏（围栏由 sandboxPolicy 的 mode fence 只挡模式变更）。
        sandbox = FakeSandbox()
        self.ctx.provide("sandbox", sandbox)
        controller = self.controller(policy_mode="workspace-write")
        agent = self.agent()
        controller.create(agent, {"id": "terminal-1", "cols": 80, "rows": 24})
        self.assertEqual(self.spawn_specs[0]["argv"], ["/bin/bash", "-i"])
        self.assertEqual(self.spawn_specs[0]["cwd"], "/workspace")
        self.assertEqual(sandbox.calls, [])

    def test_reports_a_missing_execution_provider(self):
        bare = Context(name="no-execution-provider")
        self.addCleanup(bare.dispose)
        controller = install_terminal_controller(
            bare, {"maxCols": 200, "maxRows": 100, "maxInputBytes": 1000,
                   "scrollback": 100, "disposeGraceMs": 100},
            spawn_terminal=self._spawn,
            resolve_shell_fn=lambda configured, signal=None: self.shell)
        with self.assertRaisesRegex(RuntimeError, "requires subprocess and sandbox policy"):
            controller.create(self.agent(), {"id": "terminal-1", "cols": 80, "rows": 24})
        self.assertEqual(self.spawn_specs, [])

    def test_selects_a_discovered_shell_and_rejects_unlisted_paths(self):
        def discover(configured, candidates, signal=None):
            return [dict(SHELL), {"path": "/bin/zsh", "name": "zsh", "args": ["-i"]}]

        controller = self.controller(discover_shells_fn=discover)
        agent = self.agent()
        created = controller.create(agent, {"id": "terminal-1", "cols": 80, "rows": 24,
                                            "shellPath": "/bin/zsh"})
        self.assertEqual(created["shell"]["path"], "/bin/zsh")
        self.assertEqual(self.spawn_specs[0]["argv"], ["/bin/zsh", "-i"])
        with self.assertRaisesRegex(RuntimeError, "Selected shell is not available"):
            controller.create(agent, {"id": "terminal-2", "cols": 80, "rows": 24,
                                      "shellPath": "/bin/unlisted"})

    def test_blocks_sandbox_mode_changes_only_while_a_terminal_is_retained(self):
        controller = self.controller()
        agent = self.agent()
        session = agent.session
        self.policy.set_mode(session, "danger-full-access")
        controller.create(agent, {"id": "terminal-1", "cols": 80, "rows": 24})
        self.policy.set_mode(session, "danger-full-access")
        with self.assertRaisesRegex(RuntimeError, "Close browser terminals"):
            self.policy.set_mode(session, "workspace-write")
        controller.close(agent, "terminal-1")
        self.policy.set_mode(session, "workspace-write")
        self.assertEqual(effective_sandbox_mode(session.events), "workspace-write")

    def test_retains_failed_allocations_until_their_cleanup_succeeds(self):
        controller = self.controller(config={"maxTerminals": 1})
        agent = self.agent()
        signal = Cancellation()

        def spawn(spec):
            signal.abort(RuntimeError("lost request"))
            handle = self._spawn(spec)
            handle.on_terminate = lambda: (_ for _ in ()).throw(RuntimeError("still alive"))
            return handle

        controller._spawn_terminal = spawn
        with self.assertRaises(Exception):
            controller.create(agent, {"id": "terminal-1", "cols": 80, "rows": 24},
                              signal=signal)
        listed = controller.list("session-a")
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0]["state"], "failed")
        self.handle.on_terminate = None
        controller.close(agent, "terminal-1")
        self.assertEqual(controller.list("session-a"), [])

    def test_terminates_retained_processes_when_the_controller_disposes(self):
        controller = self.controller()
        agent = self.agent()
        controller.create(agent, {"id": "terminal-1", "cols": 80, "rows": 24})
        self.ctx.dispose()
        self.assertEqual(self.handle.terminate_calls, 1)


class TestShellResolution(unittest.TestCase):
    def test_resolve_executable_accepts_an_absolute_executable(self):
        self.assertEqual(resolve_executable(sys.executable), sys.executable)

    def test_resolve_executable_rejects_relative_paths(self):
        with self.assertRaisesRegex(RuntimeError, "relative path"):
            resolve_executable("bin/server")

    def test_resolve_executable_reports_missing_bare_names(self):
        with self.assertRaises(SubprocessExecutableNotFoundError):
            resolve_executable("dsh-command-that-does-not-exist", {"PATH": ""})

    def test_resolve_shell_verifies_the_configured_profile(self):
        configured = {"path": sys.executable, "name": "python", "args": ["-i"]}
        self.assertEqual(resolve_shell(configured), configured)

    def test_discover_shells_keeps_the_configured_shell_first_and_skips_misses(self):
        shells = discover_shells({"path": sys.executable, "name": "python", "args": ["-i"]},
                                 ["dsh-command-that-does-not-exist"], None)
        self.assertEqual([shell["path"] for shell in shells], [sys.executable])


if __name__ == "__main__":
    unittest.main()
