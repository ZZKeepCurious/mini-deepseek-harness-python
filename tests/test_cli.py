"""cli.py：launcher 选项 / dump / 组合验证 / sessions 子命令。"""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch as mock_patch

from miniharness import cli


def _run_cli(argv, env=None):
    """运行 main 并捕获 stdout/stderr/退出码（不真的 sys.exit）。"""
    import sys

    out, err, code = [], [], [None]

    def fake_exit(n):
        code[0] = n
        raise SystemExit(n)

    with mock_patch.dict(os.environ, env or {}, clear=False):
        os.environ.pop("DEEPSEEK_API_KEY", None)
        with mock_patch.object(sys, "stdout", _Stream(out)), \
             mock_patch.object(sys, "stderr", _Stream(err)), \
             mock_patch.object(sys, "exit", fake_exit):
            try:
                cli.main(argv)
            except SystemExit:
                pass
    return "".join(out), "".join(err), code[0]


class _Stream:
    def __init__(self, lines):
        self._lines = lines

    def write(self, text):
        self._lines.append(text)


class TestLauncherFlags(unittest.TestCase):
    def test_unknown_option_fails(self):
        out, err, code = _run_cli(["--nope"])
        self.assertEqual(code, 1)
        self.assertIn("unknown option", err)

    def test_profile_missing_value_fails(self):
        out, err, code = _run_cli(["--profile"])
        self.assertEqual(code, 1)
        self.assertIn("requires a value", err)

    def test_unknown_profile_fails(self):
        out, err, code = _run_cli(["--profile", "web", "task"])
        self.assertEqual(code, 1)
        self.assertIn("unknown profile", err)

    def test_help(self):
        out, err, code = _run_cli(["--help"])
        self.assertEqual(code, None)
        self.assertIn("Usage:", out)


class TestDump(unittest.TestCase):
    def test_dump_default_config(self):
        out, err, code = _run_cli(["--dump-default-config"])
        self.assertEqual(code, None)
        self.assertIn("builtin:headless", out)

    def test_dump_default_rejects_patch(self):
        out, err, code = _run_cli(["--dump-default-config", "--patch", "x.yml"])
        self.assertEqual(code, 1)
        self.assertIn("cannot be combined", err)

    def test_dump_default_rejects_config(self):
        out, err, code = _run_cli(["--dump-default-config", "--config", "x.yml"])
        self.assertEqual(code, 1)
        self.assertIn("cannot be combined", err)

    def test_dump_rejects_task_args(self):
        out, err, code = _run_cli(["--dump-config", "some task"])
        self.assertEqual(code, 1)
        self.assertIn("takes no task", err)

    def test_dump_modes_mutually_exclusive(self):
        out, err, code = _run_cli(["--dump-config", "--dump-default-config"])
        self.assertEqual(code, 1)
        self.assertIn("mutually exclusive", err)

    def test_dump_with_patch_and_js_expr(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch = Path(tmp) / "p.yml"
            patch.write_text(
                "- insert:\n"
                "    - id: greeter\n"
                "      module: miniharness.example_plugins\n"
                "      config:\n"
                "        greeting: !!js process.env.MINI_DUMP_G\n",
                encoding="utf-8",
            )
            out, err, code = _run_cli(["--dump-config", "--patch", str(patch)], env={"MINI_DUMP_G": "hi"})
            self.assertEqual(code, None)
            self.assertIn("# == p.yml", out)
            self.assertIn("greeter", out)
            self.assertIn("!!js", out)
            self.assertNotIn("hi", out)  # !!js 原样打印，不求值

    def test_dump_with_config_and_patch_layers(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "c.yml"
            config.write_text(
                "plugins:\n"
                "  - id: a\n"
                "    module: miniharness.example_plugins\n"
                "  - id: b\n"
                "    module: miniharness.example_plugins\n",
                encoding="utf-8",
            )
            patch = Path(tmp) / "p.yml"
            patch.write_text(
                "- replace:\n"
                "    id: a\n"
                "    config:\n"
                "      greeting: hi\n",
                encoding="utf-8",
            )
            out, err, code = _run_cli(["--dump-config", "--config", str(config), "--patch", str(patch)])
            self.assertEqual(code, None)
            self.assertIn("# == c.yml", out)
            self.assertIn("# == p.yml", out)
            self.assertIn("greeting: hi", out)

    def test_dump_skipped_patch_warns(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch = Path(tmp) / "bad.yml"
            patch.write_text(
                "- replace:\n"
                "    id: nope\n"
                "    config: {}\n",
                encoding="utf-8",
            )
            out, err, code = _run_cli(["--dump-config", "--patch", str(patch)])
            self.assertEqual(code, None)
            self.assertIn("被跳过", err)

    def test_dump_bad_patch_file_fails(self):
        out, err, code = _run_cli(["--dump-config", "--patch", "no_such.yml"])
        self.assertEqual(code, 1)
        self.assertIn("failed to read patches", err)


class TestCompositionValidation(unittest.TestCase):
    def test_boot_validation_with_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "c.yml"
            config.write_text(
                "plugins:\n"
                "  - id: greeter\n"
                "    module: miniharness.example_plugins\n"
                "    config:\n"
                "      greeting: 你好\n",
                encoding="utf-8",
            )
            out, err, code = _run_cli(["--config", str(config), "--profile", "headless", "say hi"])
            self.assertIn("composition ok", out)
            self.assertIn("greeter", out)
            self.assertEqual(code, 1)  # 无 API key → headless fail loud

    def test_boot_validation_fails_loud(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = Path(tmp) / "c.yml"
            config.write_text(
                "plugins:\n"
                "  - id: missing\n"
                "    module: miniharness.no_such_module\n",
                encoding="utf-8",
            )
            out, err, code = _run_cli(["--config", str(config), "--profile", "headless", "say hi"])
            self.assertEqual(code, 1)
            self.assertIn("No module named", err)

    def test_patches_over_empty_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            patch = Path(tmp) / "p.yml"
            patch.write_text(
                "- insert:\n"
                "    - id: new1\n"
                "      module: miniharness.example_plugins\n",
                encoding="utf-8",
            )
            out, err, code = _run_cli(["--patch", str(patch), "--profile", "headless", "say hi"])
            self.assertIn("composition ok: 1 entry(ies)", out)


class TestSessions(unittest.TestCase):
    def _make_session(self, root: Path) -> str:
        from miniharness.llm import FakeLlmAdapter
        from miniharness.core.agent_loop.agent import AgentLoop
        from miniharness.core.session import Session
        from miniharness.core.scope import Context
        from miniharness.cli.default_tools import default_tools
        from miniharness.core.session.persistence import JsonlPersistence

        pers = JsonlPersistence(root / "sessions")
        session = Session("sess-test-1")
        loop = AgentLoop(session, FakeLlmAdapter(), default_tools(Context(name="t")), Context(name="t"))
        loop.followup("hello")
        for ev in session.events:
            pers.append(session.session_id, dict(ev))
        pers.flush()
        return session.session_id

    def test_list_shows_balanced_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_session(root)
            out, err, code = _run_cli(["sessions"], env={"MINIHARNESS_HOME": str(root)})
            self.assertEqual(code, None)
            self.assertIn("sess-test-1", out)
            self.assertIn("balanced", out)

    def test_list_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err, code = _run_cli(["sessions"], env={"MINIHARNESS_HOME": str(tmp)})
            self.assertIn("no sessions", out)

    def test_resume_without_task_prints_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_session(root)
            out, err, code = _run_cli(["sessions", "resume", "sess-test-1"], env={"MINIHARNESS_HOME": str(root)})
            self.assertEqual(code, None)
            self.assertIn("session sess-test-1", out)
            self.assertIn("balanced: True", out)

    def test_resume_unknown_session_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err, code = _run_cli(["sessions", "resume", "ghost"], env={"MINIHARNESS_HOME": str(tmp)})
            self.assertEqual(code, 1)
            self.assertIn("not found", err)

    def test_resume_with_task_continues(self):
        from miniharness.llm import FakeLlmAdapter
        from miniharness.cli.session_cmds import sessions_main

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sid = self._make_session(root)
            out, err, code = [], [], [None]

            def fake_exit(n):
                code[0] = n
                raise SystemExit(n)

            with mock_patch.object(cli.sys, "stdout", _Stream(out)), \
                 mock_patch.object(cli.sys, "stderr", _Stream(err)), \
                 mock_patch.object(cli.sys, "exit", fake_exit):
                try:
                    sessions_main(["resume", sid, "keep going"], adapter=FakeLlmAdapter(), root=root / "sessions")
                except SystemExit:
                    pass
            self.assertIn("任务完成", "".join(out))
            from miniharness.core.session.persistence import JsonlPersistence, balanced_after_replay
            pers = JsonlPersistence(root)
            self.assertTrue(balanced_after_replay(pers, sid))

    def test_delete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            sid = self._make_session(root)
            out, err, code = _run_cli(["sessions", "delete", sid], env={"MINIHARNESS_HOME": str(root)})
            self.assertIn("deleted", out)
            self.assertFalse((root / f"{sid}.jsonl").exists())

    def test_delete_missing_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err, code = _run_cli(["sessions", "delete", "ghost"], env={"MINIHARNESS_HOME": str(tmp)})
            self.assertEqual(code, 1)
            self.assertIn("not found", err)

    def test_unknown_subcommand_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            out, err, code = _run_cli(["sessions", "frobnicate"], env={"MINIHARNESS_HOME": str(tmp)})
            self.assertEqual(code, 1)
            self.assertIn("unknown sessions subcommand", err)


if __name__ == "__main__":
    unittest.main()