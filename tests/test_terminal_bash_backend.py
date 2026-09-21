"""BashTerminalBackend 装配/清理与 spawn_argv 的确定性面。

策略决议两类形态（object.resolve / callable）、confine 包装、spawn/startup 失败
的双重清理（TerminalBackendCleanupError）都经注入 seam 全量覆盖；pwsh 启动复盘
以「写时即应答」的 fake 载体驱 LocalPtySession 真状态机。真实 PTY 面留 e2e 冒烟。
"""

import threading
import time
import unittest
from types import SimpleNamespace

from miniharness.core.scope import Context
from miniharness.terminal.types import Cancellation, TerminalBackendCleanupError
from miniharness.terminal_bash import (
    BashTerminalBackend,
    LocalPtySession,
    apply,
    install_terminal_bash,
    resolve_config,
    spawn_argv,
    startup_session,
)
from miniharness.terminal.service import install_terminals

from tests.test_terminal_bash_session import (
    FakeClock,
    FakeTerminalHandle,
    make_config,
)

MARKER = "\x1b]133;D;0\x07"
PROMPT = "dsh> "


def owner():
    return SimpleNamespace(id="owner-abc", session=SimpleNamespace(session_id="s1", meta={}))


class FakePolicy:
    def __init__(self, mode="danger-full-access", workspace_root=None):
        self.mode = mode
        self.workspace_root = workspace_root

    def resolve(self, request=None):
        return {"mode": self.mode, "workspaceRoot": self.workspace_root}


class FakeSandbox:
    def confine(self, argv, policy):
        return {"argv": [f"wrapped-{argv[0]}", *argv[1:]]}


class TestSpawnArgv(unittest.TestCase):
    def test_danger_full_access_bypasses_sandbox(self):
        config = resolve_config({"shellDialect": "bash"})
        argv = spawn_argv(config, {"mode": "danger-full-access"}, None)
        self.assertEqual(argv, ["/bin/bash", "--noprofile", "--norc", "-i"])

    def test_other_modes_require_sandbox(self):
        config = resolve_config({"shellDialect": "bash"})
        self.assertRaisesRegex(
            RuntimeError, "sandbox mode",
            lambda: spawn_argv(config, {"mode": "workspace-write"}, None))
        argv = spawn_argv(config, {"mode": "workspace-write"}, FakeSandbox())
        self.assertEqual(argv, ["wrapped-/bin/bash", "--noprofile", "--norc", "-i"])


class TestBashTerminalBackend(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="bash-backend")
        self.ctx.provide("sandboxPolicy", FakePolicy())

    def _backend(self, **opts):
        return BashTerminalBackend(self.ctx, resolve_config({"backendType": "shell"}), **opts)

    def _green_backend(self, handle, spawn_specs):
        return self._backend(
            spawn_terminal_service=lambda spec: spawn_specs.append(spec) or handle,
            create_session=lambda h, c: LocalPtySession(h, c, clock=FakeClock()))

    def test_green_path_spawns_backend_session(self):
        handle = FakeTerminalHandle()
        handle.autostart(f"welcome\n{MARKER}{PROMPT}", delay_ms=10)
        spawn_specs = []
        backend = self._green_backend(handle, spawn_specs)
        session = backend.spawn({"owner": owner(), "sessionId": "pty-x"})

        spec = spawn_specs[0]
        self.assertEqual(spec["argv"], ["/bin/bash", "--noprofile", "--norc", "-i"])
        self.assertEqual(spec["terminalType"], "dumb")
        self.assertEqual(spec["rows"], 40)
        self.assertEqual(spec["cols"], 160)
        self.assertEqual(spec["cwd"], None)
        env = spec["env"]
        self.assertEqual(env["TERM"], "dumb")
        self.assertEqual(env["DSH_SHELL"], "1")
        self.assertEqual(env["DSH_SESSION_ID"], "owner-abc")
        self.assertEqual(env["DSH_PTY_SESSION_ID"], "pty-x")
        self.assertIn("133;D;", env["PROMPT_COMMAND"])
        self.assertEqual(session.status(), {"kind": "running"})
        self.assertIn(PROMPT, session.motd)
        self.assertIn("welcome", session.motd)

    def test_cwd_from_spec_wins_over_workspace_root(self):
        handle = FakeTerminalHandle()
        handle.autostart(f"{MARKER}{PROMPT}", delay_ms=10)
        spawn_specs = []
        backend = self._green_backend(handle, spawn_specs)
        backend.spawn({"owner": owner(), "cwd": "/home/demo", "sessionId": "pty-x"})
        self.assertEqual(spawn_specs[0]["cwd"], "/home/demo")

    def test_missing_policy_service_raises(self):
        backend = BashTerminalBackend(Context(name="bare"), resolve_config({}))
        self.assertRaisesRegex(
            RuntimeError, "sandboxPolicy service is required",
            lambda: backend.spawn({"owner": owner(), "sessionId": "pty-x"}))

    def test_callable_policy_service_supported(self):
        requests = []

        def policy_service(request):
            requests.append(request)
            return {"mode": "danger-full-access"}

        ctx = Context(name="callable")
        ctx.provide("sandboxPolicy", policy_service)
        handle = FakeTerminalHandle()
        handle.autostart(f"{MARKER}{PROMPT}", delay_ms=10)
        backend = BashTerminalBackend(ctx, resolve_config({}))
        backend._spawn_terminal_service = lambda spec: handle
        backend._create_session = lambda h, c: LocalPtySession(h, c, clock=FakeClock())
        session = backend.spawn({"owner": owner(), "sessionId": "pty-x"})
        self.assertEqual(session.status(), {"kind": "running"})
        self.assertEqual(requests, [{"session": owner().session}])

    def test_spawn_terminal_failure_propagates(self):
        class Boom(RuntimeError):
            pass

        backend = self._backend(spawn_terminal_service=lambda spec: (_ for _ in ()).throw(Boom("pty")))
        self.assertRaises(Boom, lambda: backend.spawn({"owner": owner(), "sessionId": "pty-x"}))

    def test_create_session_failure_cleans_up_handle(self):
        backend = self._backend(
            spawn_terminal_service=lambda spec: FakeTerminalHandle(),
            create_session=lambda handle, cfg: (_ for _ in ()).throw(RuntimeError("no session")))
        self.assertRaises(RuntimeError, lambda: backend.spawn({"owner": owner(), "sessionId": "pty-x"}))

    def test_cleanup_failure_yields_cleanup_error(self):
        class FailingHandle(FakeTerminalHandle):
            def terminate(self):
                raise RuntimeError("terminate broken")

        backend = self._backend(
            spawn_terminal_service=lambda spec: FailingHandle(),
            create_session=lambda handle, cfg: (_ for _ in ()).throw(RuntimeError("no session")))
        with self.assertRaises(TerminalBackendCleanupError) as raised:
            backend.spawn({"owner": owner(), "sessionId": "pty-x"})
        self.assertIn("no session", str(raised.exception.spawn_error))
        self.assertIn("terminate broken", str(raised.exception.cleanup_error))

    def test_startup_failure_closes_session(self):
        closes = []

        class FakeSession:
            motd = ""

            def initialize(self, signal=None):
                raise RuntimeError("startup blew up")

            def close(self, reason):
                closes.append(reason)

        backend = self._backend(
            spawn_terminal_service=lambda spec: FakeTerminalHandle(),
            create_session=lambda handle, cfg: FakeSession())
        self.assertRaisesRegex(
            RuntimeError, "startup blew up",
            lambda: backend.spawn({"owner": owner(), "sessionId": "pty-x"}))
        self.assertEqual(closes, ["PTY startup failed"])

    def test_aborted_signal_blocks_spawn(self):
        signal = Cancellation()
        signal.abort(RuntimeError("cancelled"))
        backend = self._backend()
        self.assertRaises(RuntimeError, lambda: backend.spawn({"owner": owner(), "sessionId": "pty-x", "signal": signal}))


class TestStartupSession(unittest.TestCase):
    def test_bash_startup_uses_initialize(self):
        handle = FakeTerminalHandle()
        session = LocalPtySession(handle, make_config(), clock=FakeClock())
        handle.autostart(f"{MARKER}{PROMPT}", delay_ms=10)
        startup_session(session, "bash", 5_000)
        self.assertEqual(session.status(), {"kind": "running"})
        self.assertIn(PROMPT, session.motd)

    def test_pwsh_setup_sets_prompt_then_settles(self):
        class RespondingHandle(FakeTerminalHandle):
            def write(self, text):
                super().write(text)
                if "function prompt" in text:
                    self.emit(f"{MARKER}{PROMPT}")

        handle = RespondingHandle()
        session = LocalPtySession(handle, make_config(), clock=FakeClock())
        startup_session(session, "pwsh", 5_000)
        self.assertTrue(any("function prompt" in w for w in handle.written))
        self.assertIn(PROMPT, session.motd)

    def test_pwsh_startup_exit_raises(self):
        handle = FakeTerminalHandle()
        session = LocalPtySession(handle, make_config(), clock=FakeClock())

        def quit():
            time.sleep(0.02)
            handle.emit_exit(1)

        threading.Thread(target=quit, daemon=True).start()
        self.assertRaises(RuntimeError, lambda: startup_session(session, "pwsh", 5_000))

    def test_pwsh_startup_timeout_raises(self):
        # 无 prompt 时每次 send 都会 inferred_idle 结算 → 只有操作 deadline 能终结
        # 循环；idleSilenceMs 拉大排除 idle 面，令 timeoutMs 主导。
        handle = FakeTerminalHandle()
        session = LocalPtySession(
            handle, make_config(timeoutMs=200, idleSilenceMs=100_000), clock=FakeClock())
        self.assertRaises(RuntimeError, lambda: startup_session(session, "pwsh", 5_000))


class TestInstall(unittest.TestCase):
    def test_apply_registers_backend(self):
        ctx = Context(name="apply")
        install_terminals(ctx)
        apply(ctx, {"backendType": "shell"})
        self.assertIn("shell", ctx.get("terminals").list_backends())

    def test_install_is_idempotent(self):
        ctx = Context(name="install")
        install_terminal_bash(ctx)
        terminals = ctx.get("terminals")
        install_terminal_bash(ctx)
        self.assertEqual(terminals.list_backends(), ["shell"])

    def test_install_honors_injected_backend(self):
        ctx = Context(name="install-custom")
        backend = BashTerminalBackend(ctx, resolve_config({"backendType": "shell"}))
        installed = install_terminal_bash(ctx, backend=backend)
        self.assertIs(installed, backend)
        self.assertEqual(ctx.get("terminals").list_backends(), ["shell"])


if __name__ == "__main__":
    unittest.main()