# terminal_bash shell-activity + TerminalHandle.inspect_activity 确定性面。
#
# 对齐 upstream subprocess-local 的 shell-activity.ts / terminal.ts:
# ShellActivity 记录栅栏与 revision、prepare_shell_activity 的启动门与私有文件、
# TerminalHandle 组合 shell-activity 与前台进程组的判读。真实交互 shell 的行为面
# （sleep/read/后台作业的 idle|busy 迁移）属宿主 e2e，见 test_terminal_bash_e2e。

import os
import tempfile
import unittest

from miniharness.terminal_bash import (
    ShellActivity,
    SubprocessForeground,
    SubprocessTerminalActivity,
    TerminalHandle,
    TerminalOutputChannel,
    prepare_shell_activity,
)


class StubHandle(TerminalHandle):
    """确定性平台载体：前台进程组可控、写/信号/终止可观测。"""

    def __init__(self, pid=100, shell_activity=None):
        super().__init__(pid, TerminalOutputChannel(), shell_activity)
        self.foreground = None
        self.writes = []
        self.signals = []
        self.terminations = 0

    def _write(self, text):
        self.writes.append(text)

    def resize(self, cols, rows):
        pass

    def inspect_foreground(self):
        return self.foreground

    def _signal_foreground(self, signal):
        self.signals.append(signal)
        return self.foreground.process_group_id if self.foreground is not None else self.pid

    def _terminate(self):
        self.terminations += 1


def make_activity():
    directory = tempfile.mkdtemp(prefix="dsh-activity-test-")
    activity = ShellActivity(directory, [], {})
    return directory, activity


class ShellActivityRecordTest(unittest.TestCase):
    def setUp(self):
        self.dir, self.activity = make_activity()
        self.addCleanup(lambda: self.activity.dispose())
        self.state = os.path.join(self.dir, "state")

    def write_state(self, text):
        with open(self.state, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_fresh_missing_record_is_unknown(self):
        self.assertEqual(self.activity.inspect(123).state, "unknown")

    def test_reads_idle_and_holds_revision_until_record_changes(self):
        self.write_state("123:1:idle\n")
        idle = self.activity.inspect(123)
        self.assertEqual(idle.state, "idle")
        self.assertEqual(self.activity.inspect(123).state, "idle")
        self.assertEqual(self.activity.inspect(123).revision, idle.revision)

    def test_input_invalidates_the_last_record(self):
        self.write_state("123:1:idle\n")
        self.activity.inspect(123)
        self.activity.invalidate()
        self.assertEqual(self.activity.inspect(123).state, "unknown")

    def test_record_change_advances_and_foreign_or_malformed_is_unknown(self):
        self.write_state("123:1:idle\n")
        self.activity.inspect(123)
        self.write_state("123:2:busy")
        self.assertEqual(self.activity.inspect(123).state, "busy")
        self.write_state("999:2:idle\n")
        self.assertEqual(self.activity.inspect(123).state, "unknown")
        self.write_state("123:3:id")
        self.assertEqual(self.activity.inspect(123).state, "unknown")

    def test_dispose_removes_directory_and_reports_unknown(self):
        self.write_state("123:1:idle\n")
        self.activity.inspect(123)
        self.activity.dispose()
        self.assertFalse(os.path.exists(self.dir))
        self.assertEqual(self.activity.inspect(123).state, "unknown")


class PrepareShellActivityTest(unittest.TestCase):
    def spec(self, **overrides):
        base = {
            "argv": ["/bin/zsh", "-i"],
            "cwd": "/",
            "rows": 24,
            "cols": 80,
            "terminalType": "xterm-256color",
            "graceMs": 100,
            "shellActivity": True,
        }
        base.update(overrides)
        return base

    def test_unsupported_launches_are_untouched(self):
        self.assertIsNone(prepare_shell_activity(self.spec(argv=["/bin/fish", "-i"]), {}, "darwin"))
        self.assertIsNone(prepare_shell_activity(self.spec(argv=["/bin/zsh", "-l"]), {}, "darwin"))
        self.assertIsNone(prepare_shell_activity(self.spec(argv=["/bin/zsh", "-l", "-i"]), {}, "darwin"))
        self.assertIsNone(prepare_shell_activity(self.spec(argv=["/bin/bash"]), {}, "darwin"))
        self.assertIsNone(prepare_shell_activity(self.spec(shellActivity=False), {}, "darwin"))
        self.assertIsNone(prepare_shell_activity(self.spec(), {}, "win32"))

    def test_zsh_sets_zdotdir_and_restores_missing_original(self):
        activity = prepare_shell_activity(self.spec(), {}, "linux")
        self.addCleanup(activity.dispose)
        self.assertEqual(activity.argv, ["/bin/zsh", "-i"])
        directory = activity.env["ZDOTDIR"]
        self.assertTrue(os.path.isdir(directory))
        with open(os.path.join(directory, ".zshenv"), encoding="utf-8") as fh:
            head = fh.read().split("\n")[:2]
        self.assertEqual(head[0], "unset ZDOTDIR")
        self.assertEqual(
            head[1],
            '[[ ! -r ${ZDOTDIR:-$HOME}/.zshenv ]] || builtin source "${ZDOTDIR:-$HOME}/.zshenv"',
        )

    def test_zsh_restores_a_quoted_original_zdotdir(self):
        env = {"ZDOTDIR": "/shell config/user's $settings", "OTHER": "preserved"}
        activity = prepare_shell_activity(self.spec(), dict(env), "linux")
        self.addCleanup(activity.dispose)
        directory = activity.env["ZDOTDIR"]
        self.assertNotEqual(directory, env["ZDOTDIR"])
        self.assertEqual(activity.env["OTHER"], "preserved")
        with open(os.path.join(directory, ".zshenv"), encoding="utf-8") as fh:
            first = fh.read().split("\n")[0]
        self.assertEqual(first, "ZDOTDIR='/shell config/user'\\''s $settings'")

    def test_bash_rewrites_argv_with_private_rcfile(self):
        activity = prepare_shell_activity(self.spec(argv=["/bin/bash", "-i"]), {}, "darwin")
        self.addCleanup(activity.dispose)
        self.assertEqual(activity.argv[0], "/bin/bash")
        self.assertEqual(activity.argv[1], "--rcfile")
        self.assertEqual(activity.argv[3], "-i")
        self.assertTrue(os.path.isfile(activity.argv[2]))
        with open(activity.argv[2], encoding="utf-8") as fh:
            rc = fh.read()
        self.assertIn("__dsh_shell_idle", rc)
        self.assertIn(">| " + "'" + os.path.join(activity.directory, "state") + "'", rc)


class HandleInspectActivityTest(unittest.TestCase):
    def setUp(self):
        self.dir, self.activity = make_activity()
        self.state = os.path.join(self.dir, "state")

    def tearDown(self):
        self.activity.dispose()

    def write_state(self, text):
        with open(self.state, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_prompt_idle_only_with_matching_foreground_shell(self):
        self.write_state("100:1:idle\n")
        handle = StubHandle(100, self.activity)
        handle.foreground = SubprocessForeground(100, True)
        idle = handle.inspect_activity()
        self.assertEqual(idle.state, "idle")
        self.assertEqual(handle.inspect_activity().state, "idle")
        self.assertEqual(handle.inspect_activity().revision, idle.revision)

    def test_foreground_child_is_busy_and_missing_foreground_unknown(self):
        self.write_state("100:1:idle\n")
        handle = StubHandle(100, self.activity)
        handle.foreground = SubprocessForeground(200, True)
        self.assertEqual(handle.inspect_activity().state, "busy")
        handle.foreground = None
        self.assertEqual(handle.inspect_activity().state, "unknown")

    def test_write_invalidates_prompt_evidence(self):
        self.write_state("100:1:idle\n")
        handle = StubHandle(100, self.activity)
        handle.foreground = SubprocessForeground(100, True)
        self.assertEqual(handle.inspect_activity().state, "idle")
        handle.write("partial")
        self.assertEqual(handle.writes, ["partial"])
        self.assertEqual(handle.inspect_activity().state, "unknown")

    def test_record_change_advances_revision(self):
        self.write_state("100:1:idle\n")
        handle = StubHandle(100, self.activity)
        handle.foreground = SubprocessForeground(100, True)
        first = handle.inspect_activity()
        self.write_state("100:2:busy\n")
        second = handle.inspect_activity()
        self.assertEqual(second.state, "busy")
        self.assertGreater(second.revision, first.revision)

    def test_terminate_is_quiescent_idle_and_disposes_activity(self):
        self.write_state("100:1:idle\n")
        handle = StubHandle(100, self.activity)
        handle.foreground = SubprocessForeground(200, True)
        self.assertEqual(handle.inspect_activity().state, "busy")
        handle.terminate()
        self.assertEqual(handle.terminations, 1)
        self.assertFalse(os.path.exists(self.dir))
        self.assertEqual(handle.inspect_activity().state, "idle")

    def test_without_shell_activity_the_foreground_group_still_decides(self):
        handle = StubHandle(100, None)
        handle.foreground = SubprocessForeground(100, True)
        unknown = handle.inspect_activity()
        self.assertEqual(unknown.state, "unknown")
        self.assertIsInstance(unknown, SubprocessTerminalActivity)
        handle.foreground = SubprocessForeground(200, True)
        self.assertEqual(handle.inspect_activity().state, "busy")


if __name__ == "__main__":
    unittest.main()
