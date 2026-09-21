"""真实 PTY 载体冒烟。

Windows（winpty）：cmd.exe 数据通路与退出 code 断言的确定性验证；bash/pwsh
方言就绪面依赖宿主提供了可镜像的 shell（bash 需非 WindowsApps 存根，pwsh 需
PowerShell 7）。host 缺 shell / 载体不可用 → 跳过（真实面仍受单元层确定性覆盖）。
"""

import os
import sys
import unittest
from types import SimpleNamespace

from miniharness.terminal_bash import (
    BashTerminalBackend,
    LocalPtySession,
    child_environment,
    resolve_config,
    spawn_terminal,
    startup_session,
)

MARKER = "\x1b]133;D;0\x07"
PROMPT = "dsh> "


def owner():
    return SimpleNamespace(id="e2e-owner", session=SimpleNamespace(session_id="e2e-s1", meta={}))


def _winpty_available():
    try:
        import winpty  # noqa: F401
        return True
    except ImportError:
        return False


def _real_bash():
    path = os.environ.get("PATH", "").split(os.pathsep)
    for match in (os.path.join(p, "bash") for p in path if p):
        if os.path.isfile(match):
            return match
    return None


def _pwsh7():
    path = os.environ.get("PATH", "").split(os.pathsep)
    for candidate in ("pwsh", "pwsh.exe"):
        for match in (os.path.join(p, candidate) for p in path if p):
            if os.path.isfile(match):
                return match
    return None


class TestProviderConPty(unittest.TestCase):
    """Windows 真实 ConPTY 载体：字节通路 + 退出 outcome。"""

    @unittest.skipUnless(sys.platform.startswith("win") and _winpty_available(), "winpty unavailable")
    def test_data_path_and_exit_outcome(self):
        handle = spawn_terminal({
            "argv": [r"C:\WINDOWS\system32\cmd.exe", "/d", "/q", "/c",
                     "echo marker-cmd-555 & exit 7"],
            "cwd": None,
            "env": child_environment({"owner": owner(), "sessionId": "pty-e2e-0"}, "pwsh"),
            "rows": 40,
            "cols": 120,
            "terminalType": "dumb",
            "graceMs": 5000,
        })
        try:
            self.assertTrue(handle.output.wait_end(10))
            exited = handle.output.outcome
            self.assertIsNotNone(exited)
            self.assertEqual(exited.exit_code, 7)
        finally:
            handle.terminate()
            handle.wait(1)


@unittest.skipUnless(sys.platform.startswith("win") and _winpty_available(), "winpty unavailable")
class TestBashBackendRealPty(unittest.TestCase):
    """真实 bash 承载的完整 backend 链路（bash 缺失则跳过）。"""

    @unittest.skipUnless(_real_bash(), "no real bash on host")
    def test_spawn_send_read_close(self):
        from miniharness.core.scope import Context
        from miniharness.terminal.service import install_terminals
        ctx = Context(name="e2e")
        install_terminals(ctx)
        backend = BashTerminalBackend(ctx, resolve_config({"shellDialect": "bash"}))
        session = backend.spawn({"owner": owner(), "sessionId": "pty-e2e-1"})
        try:
            self.assertEqual(session.status(), {"kind": "running"})
            self.assertIn(PROMPT, session.motd)
            op = session.start_send({"text": "echo e2e-bash-77", "submit": True})
            session.run_until_settled(op, None)
            self.assertEqual(op.done["waitReason"], "stdin_read")
            self.assertIn("e2e-bash-77", op.done["viewport"])
        finally:
            session.close("e2e end")


@unittest.skipUnless(sys.platform.startswith("win") and _winpty_available(), "winpty unavailable")
class TestPwshBackendRealPty(unittest.TestCase):
    """真实 pwsh7 承载的 pwsh 启动复盘（dsh> 就绪即证明）。"""

    @unittest.skipUnless(_pwsh7(), "no pwsh 7 on host")
    def test_pwsh_startup_and_send(self):
        pwsh = _pwsh7()
        handle = spawn_terminal({
            "argv": [pwsh, "-NoLogo", "-NoProfile"],
            "cwd": None,
            "env": child_environment({"owner": owner(), "sessionId": "pty-e2e-2"}, "pwsh"),
            "rows": 40,
            "cols": 120,
            "terminalType": "dumb",
            "graceMs": 5000,
        })
        config = resolve_config({"shellDialect": "pwsh", "shellPath": pwsh, "shellArgs": ["-NoLogo", "-NoProfile"]})
        session = LocalPtySession(handle, config)
        try:
            startup_session(session, "pwsh", 30_000)
            self.assertIn(PROMPT, session.motd)
            op = session.start_send({"text": '"e2e-pwsh-88"', "submit": True})
            session.run_until_settled(op, None)
            self.assertEqual(op.done["waitReason"], "stdin_read")
            self.assertIn("e2e-pwsh-88", op.done["viewport"])
        finally:
            session.close("e2e end")


if __name__ == "__main__":
    unittest.main()