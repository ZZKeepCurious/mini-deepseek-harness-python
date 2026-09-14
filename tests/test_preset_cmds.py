"""`miniharness presets` 子命令测试。

覆盖 miniharness/cli/preset_cmds.py 全部分支：
_cmd_list / _cmd_show / _cmd_select / _cmd_delete / presets_main
（含错误用法、未知预设、锁定会话、只读预设等路径）。
"""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from miniharness.cli.preset_cmds import (
    _cmd_delete,
    _cmd_list,
    _cmd_select,
    _cmd_show,
    presets_main,
)
from miniharness.preset.presets import (
    PresetLockedError,
    PresetNotWritableError,
    builtin_roster,
    delete_preset,
    project_preset,
)


def _write_preset(root: Path, preset_id: str, manifest: dict) -> None:
    d = root / preset_id
    d.mkdir(parents=True)
    (d / "preset.json").write_text(json.dumps(manifest), encoding="utf-8")


class TestCmdList(unittest.TestCase):
    def test_list_with_presets(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ):
            stdout = io.StringIO()
            _cmd_list(stdout)
            out = stdout.getvalue()
        self.assertIn("standard", out)
        self.assertIn("minimal", out)
        self.assertIn("authorable:", out)

    def test_list_empty(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=mock.Mock(rows=lambda: [], authorable=True),
        ):
            stdout = io.StringIO()
            _cmd_list(stdout)
            self.assertIn("no presets found", stdout.getvalue())

    def test_list_with_broken(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=mock.Mock(
                rows=lambda: [{"id": "ghost", "isDefault": False, "broken": "unloadable", "trust": "user", "name": ""}],
                authorable=True,
            ),
        ):
            stdout = io.StringIO()
            _cmd_list(stdout)
            self.assertIn("broken: unloadable", stdout.getvalue())


class TestCmdShow(unittest.TestCase):
    def test_show_known_preset(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            _cmd_show("minimal", stdout, stderr)
            out = stdout.getvalue()
        self.assertIn("minimal", out)
        self.assertIn("trust=", out)
        self.assertIn("default=", out)
        self.assertIn("name:", out)
        self.assertIn("tools:", out)
        self.assertIn("persona:", out)
        self.assertEqual(stderr.getvalue(), "")

    def test_show_unknown(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as cm:
                _cmd_show("nope", stdout, stderr)
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("error:", stderr.getvalue())
            self.assertIn("not found", stderr.getvalue())
            self.assertEqual(stdout.getvalue(), "")

    def test_show_none_shows_default(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ):
            stdout = io.StringIO()
            _cmd_show(None, stdout, io.StringIO())
            self.assertIn("standard", stdout.getvalue())


class TestCmdSelect(unittest.TestCase):
    def test_select_no_session(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            _cmd_select("minimal", None, stdout, stderr)
            self.assertIn("selected minimal", stdout.getvalue())
            self.assertIn("no session given", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")

    def test_select_no_session_unknown(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as cm:
                _cmd_select("nope", None, stdout, stderr)
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("error:", stderr.getvalue())

    def test_select_with_session_locked(self):
        events = []
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ), mock.patch(
            "miniharness.cli.preset_cmds.sessions_root",
            return_value=Path(tempfile.mkdtemp()),
        ), mock.patch(
            "miniharness.cli.preset_cmds.JsonlPersistence",
        ), mock.patch(
            "miniharness.cli.preset_cmds.repair_and_replay",
            return_value=mock.Mock(events=events),
        ), mock.patch(
            "miniharness.cli.preset_cmds.project_session_agent_preset",
            return_value=None,
        ), mock.patch(
            "miniharness.cli.preset_cmds.select_preset",
            side_effect=PresetLockedError("s-1", "already started"),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as cm:
                _cmd_select("minimal", "s-1", stdout, stderr)
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("already started", stderr.getvalue())

    def test_select_with_session_ok(self):
        events = []
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ), mock.patch(
            "miniharness.cli.preset_cmds.sessions_root",
            return_value=Path(tempfile.mkdtemp()),
        ), mock.patch(
            "miniharness.cli.preset_cmds.JsonlPersistence",
        ), mock.patch(
            "miniharness.cli.preset_cmds.repair_and_replay",
            return_value=mock.Mock(events=events),
        ), mock.patch(
            "miniharness.cli.preset_cmds.project_session_agent_preset",
            return_value=None,
        ), mock.patch(
            "miniharness.cli.preset_cmds.select_preset",
            return_value=project_preset(builtin_roster(), "minimal"),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            _cmd_select("minimal", "s-1", stdout, stderr)
            self.assertIn("selected minimal", stdout.getvalue())
            self.assertEqual(stderr.getvalue(), "")


class TestCmdDelete(unittest.TestCase):
    def test_delete_readonly_shipped(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as cm:
                _cmd_delete("standard", stdout, stderr)
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("ships with the deployment", stderr.getvalue())

    def test_delete_user_preset(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            _write_preset(root, "custom", {"id": "custom", "name": "自定义", "order": 5})
            roster = mock.Mock()
            roster.ids = lambda: ["custom"]
            roster.authorable = True
            with mock.patch(
                "miniharness.cli.preset_cmds._cli_roster",
                return_value=roster,
            ), mock.patch(
                "miniharness.cli.preset_cmds.delete_preset",
                side_effect=lambda r, pid: None,
            ):
                stdout = io.StringIO()
                stderr = io.StringIO()
                _cmd_delete("custom", stdout, stderr)
                self.assertIn("deleted custom", stdout.getvalue())
                self.assertEqual(stderr.getvalue(), "")

    def test_delete_unknown(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as cm:
                _cmd_delete("nope", stdout, stderr)
            self.assertEqual(cm.exception.code, 1)
            self.assertIn("error:", stderr.getvalue())


class TestPresetsMain(unittest.TestCase):
    def test_list_via_main(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=mock.Mock(
                rows=lambda: [{"id": "standard", "isDefault": True, "trust": "system", "name": "标准模式"}],
                authorable=True,
            ),
        ):
            stdout = io.StringIO()
            presets_main(["list"], stdout=stdout)
            self.assertIn("standard", stdout.getvalue())

    def test_show_via_main(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ):
            stdout = io.StringIO()
            presets_main(["show", "minimal"], stdout=stdout)
            self.assertIn("minimal", stdout.getvalue())

    def test_select_via_main_no_session(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ), mock.patch(
            "miniharness.cli.preset_cmds._cmd_select",
        ) as mock_select:
            stdout = io.StringIO()
            presets_main(["select", "minimal"], stdout=stdout)
            mock_select.assert_called_once_with("minimal", None, mock.ANY, mock.ANY)

    def test_delete_via_main(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=mock.Mock(authorable=True),
        ), mock.patch(
            "miniharness.cli.preset_cmds._cmd_delete",
        ) as mock_delete:
            stdout = io.StringIO()
            presets_main(["delete", "custom"], stdout=stdout)
            mock_delete.assert_called_once_with("custom", mock.ANY, mock.ANY)

    def test_unknown_subcommand(self):
        stdout = io.StringIO()
        stderr = io.StringIO()
        with self.assertRaises(SystemExit) as cm:
            presets_main(["bogus"], stdout=stdout, stderr=stderr)
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("unknown presets subcommand", stderr.getvalue())

    def test_empty_argv_defaults_to_list(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=mock.Mock(rows=lambda: [], authorable=True),
        ):
            stdout = io.StringIO()
            presets_main([], stdout=stdout)
            self.assertIn("no presets found", stdout.getvalue())

    def test_show_usage_error(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as cm:
                presets_main(["show", "a", "b"], stdout=stdout, stderr=stderr)
            self.assertEqual(cm.exception.code, 1)

    def test_select_usage_error(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as cm:
                presets_main(["select"], stdout=stdout, stderr=stderr)
            self.assertEqual(cm.exception.code, 1)

    def test_delete_usage_error(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as cm:
                presets_main(["delete"], stdout=stdout, stderr=stderr)
            self.assertEqual(cm.exception.code, 1)

    def test_select_usage_error_too_many_args(self):
        with mock.patch(
            "miniharness.cli.preset_cmds._cli_roster",
            return_value=builtin_roster(),
        ):
            stdout = io.StringIO()
            stderr = io.StringIO()
            with self.assertRaises(SystemExit) as cm:
                presets_main(["select", "a", "b", "c"], stdout=stdout, stderr=stderr)
            self.assertEqual(cm.exception.code, 1)


if __name__ == "__main__":
    unittest.main()
