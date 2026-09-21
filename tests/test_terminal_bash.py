"""terminal-bash 确定性面：config 解析/校验、子进程环境、协议应答器。

对应上游 terminal-bash/tests/config.spec.ts 与 environment/emulator 的等价面；
会话状态机与真实 PTY 载体分别在 test_terminal_bash_session.py 与 e2e 冒烟。
"""

import unittest

from miniharness.terminal_bash import (
    CONTROLLED_PROMPT,
    ENCODING_PREAMBLE,
    PWSH_PROMPT_SETUP,
    SCHEMA_DEFAULTS,
    TerminalProtocolEmulator,
    child_environment,
    resolve_config,
    validate_config,
)

TINY = {"rows": 5, "cols": 10}


class StubOwner:
    id = "owner-abc"


class TestResolveConfig(unittest.TestCase):
    def test_defaults_are_schema_defaults(self):
        resolved = resolve_config({})
        self.assertEqual(resolved["backendType"], "shell")
        self.assertEqual(resolved["shellDialect"], "bash")
        self.assertEqual(resolved["rows"], SCHEMA_DEFAULTS["rows"])
        self.assertEqual(resolved["cols"], SCHEMA_DEFAULTS["cols"])
        self.assertEqual(resolved["scrollbackLines"], SCHEMA_DEFAULTS["scrollbackLines"])
        self.assertEqual(resolved["scrollbackMaxBytes"], 4 * 1024 * 1024)
        self.assertEqual(resolved["maxReadBytes"], 256 * 1024)
        self.assertEqual(resolved["pollIntervalMs"], 50)
        self.assertEqual(resolved["exactProbeAfterMs"], 150)
        self.assertEqual(resolved["idleSilenceMs"], 3_000)
        self.assertEqual(resolved["handoffGraceMs"], 500)
        self.assertEqual(resolved["timeoutMs"], 30_000)
        self.assertEqual(resolved["disposeGraceMs"], 3_000)

    def test_bash_dialect_defaults(self):
        resolved = resolve_config({"shellDialect": "bash"})
        self.assertEqual(resolved["shellPath"], "/bin/bash")
        self.assertEqual(resolved["shellArgs"], ["--noprofile", "--norc", "-i"])

    def test_explicit_shell_path_and_args_win_over_dialect_defaults(self):
        resolved = resolve_config({
            "shellDialect": "bash", "shellPath": "/usr/local/bin/bash",
            "shellArgs": ["-i", "--noprofile"],
        })
        self.assertEqual(resolved["shellPath"], "/usr/local/bin/bash")
        self.assertEqual(resolved["shellArgs"], ["-i", "--noprofile"])

    def test_empty_string_treated_as_unset_for_dialect_defaults(self):
        resolved = resolve_config({"shellDialect": "bash", "shellPath": "", "shellArgs": []})
        self.assertEqual(resolved["shellPath"], "/bin/bash")
        self.assertEqual(resolved["shellArgs"], ["--noprofile", "--norc", "-i"])

    def test_pwsh_dialect_uses_pws_hl_args(self):
        resolved = resolve_config({"shellDialect": "pwsh", "shellPath": "pwsh.exe"})
        self.assertEqual(resolved["shellPath"], "pwsh.exe")
        self.assertEqual(resolved["shellArgs"], ["-NoLogo", "-NoProfile"])

    def test_unknown_dialect_fails_loud(self):
        self.assertRaisesRegex(ValueError, "unknown shellDialect", lambda: resolve_config({"shellDialect": "zsh"}))


class TestValidateConfig(unittest.TestCase):
    def _valid(self):
        return resolve_config({"rows": 5, "cols": 10})

    def test_non_numeric_fields_pass(self):
        config = resolve_config({"rows": 5, "cols": 10})
        validate_config(config)

    def test_rejects_empty_backend_type(self):
        self.assertRaisesRegex(
            RuntimeError, "backendType must be non-empty",
            lambda: validate_config({**self._valid(), "backendType": ""}))

    def test_rejects_empty_shell_path(self):
        self.assertRaisesRegex(
            RuntimeError, "shellPath must be non-empty",
            lambda: validate_config({**self._valid(), "shellPath": ""}))

    def test_rejects_non_positive_numeric(self):
        for field in ("rows", "pollIntervalMs", "scrollbackMaxBytes", "timeoutMs"):
            with self.subTest(field=field):
                self.assertRaisesRegex(
                    RuntimeError, f"positive safe integer",
                    lambda f=field: validate_config({**self._valid(), f: 0}))

    def test_missing_numeric_field_fails_loud(self):
        config = self._valid()
        config.pop("maxReadBytes")
        self.assertRaisesRegex(RuntimeError, "maxReadBytes", lambda: validate_config(config))

    def test_max_read_must_not_exceed_scrollback(self):
        base = {**self._valid(), "maxReadBytes": 1000, "scrollbackMaxBytes": 500}
        self.assertRaisesRegex(
            RuntimeError, "maxReadBytes must not exceed scrollbackMaxBytes",
            lambda: validate_config(base))

    def test_handoff_grace_must_cover_poll(self):
        base = {**self._valid(), "handoffGraceMs": 5, "pollIntervalMs": 50}
        self.assertRaisesRegex(
            RuntimeError, "handoffGraceMs must be at least pollIntervalMs",
            lambda: validate_config(base))


class TestChildEnvironment(unittest.TestCase):
    def setUp(self):
        self.spec = {"sessionId": "pty-1", "owner": StubOwner}

    def test_bash_common_and_dialect_vars(self):
        env = child_environment(self.spec, "bash")
        self.assertEqual(env["TERM"], "dumb")
        self.assertEqual(env["PAGER"], "cat")
        self.assertEqual(env["GIT_PAGER"], "cat")
        self.assertEqual(env["DSH_SHELL"], "1")
        self.assertEqual(env["DSH_SESSION_ID"], "owner-abc")
        self.assertEqual(env["DSH_PTY_SESSION_ID"], "pty-1")
        self.assertEqual(env["PS1"], CONTROLLED_PROMPT)
        self.assertIn("PS1='dsh> '", env["PROMPT_COMMAND"])
        self.assertIn("133;D;", env["PROMPT_COMMAND"])
        self.assertEqual(env["BASH_SILENCE_DEPRECATION_WARNING"], "1")

    def test_pwsh_uses_no_color(self):
        env = child_environment(self.spec, "pwsh")
        self.assertEqual(env["NO_COLOR"], "1")
        self.assertNotIn("PS1", env)
        self.assertNotIn("PROMPT_COMMAND", env)

    def test_pwsh_prompt_setup_carries_marker(self):
        self.assertIn("133;D;", PWSH_PROMPT_SETUP)
        self.assertIn("function prompt", PWSH_PROMPT_SETUP)
        self.assertIn(CONTROLLED_PROMPT, PWSH_PROMPT_SETUP)

    def test_encoding_preamble_pins_utf8(self):
        self.assertIn("OutputEncoding", ENCODING_PREAMBLE)
        self.assertIn("UTF8Encoding", ENCODING_PREAMBLE)


class TestProtocolEmulator(unittest.TestCase):
    def _emulator(self):
        return TerminalProtocolEmulator(160, 40)

    def test_da1_reply(self):
        self.assertEqual(self._emulator().feed("\x1b[c"), "\x1b[?1;2c")
        self.assertEqual(self._emulator().feed("\x1b[?c"), "\x1b[?1;2c")

    def test_da2_reply(self):
        self.assertEqual(self._emulator().feed("\x1b[>c"), "\x1b[>0;10;0c")

    def test_cpr_reflects_cursor(self):
        emulator = self._emulator()
        emulator.feed("hi\nworld")
        # pyte 语义：LF 不附带 CR（列保持），'world' 追加在暂停列 —— 夹具即 pyte 光标面。
        self.assertEqual(emulator.feed("\x1b[6n"), "\x1b[2;8R")

    def test_decrcm_reply_registered_mode(self):
        self.assertEqual(self._emulator().feed("\x1b[?7n"), "\x1b[?7;2$y")

    def test_plain_text_no_reply(self):
        self.assertEqual(self._emulator().feed("plain text\n"), "")

    def test_osc_marker_not_answered(self):
        self.assertEqual(self._emulator().feed("\x1b]133;D;0\x07dsh> "), "")

    def test_reply_across_chunk_boundaries(self):
        emulator = self._emulator()
        self.assertEqual(emulator.feed("\x1b["), "")
        self.assertEqual(emulator.feed("6n"), "\x1b[1;1R")
        self.assertEqual(emulator.feed("c"), "")

    def test_private_csi_stripped_without_crash(self):
        # pyte 0.8.2 私有前缀 FSM 崩溃面：剥离后不喂给 stream，无崩溃（已登记）。
        self.assertEqual(self._emulator().feed("\x1b[?12;5H\x1b[?1049h\x1b[?2004h"), "")

    def test_cursor_position_query_then_cpr(self):
        emulator = self._emulator()
        emulator.feed("ab\x1b[3G")
        self.assertEqual(emulator.feed("\x1b[6n"), "\x1b[1;3R")

    def test_close_stops_answers(self):
        emulator = self._emulator()
        emulator.close()
        self.assertEqual(emulator.feed("\x1b[c"), "")


if __name__ == "__main__":
    unittest.main()