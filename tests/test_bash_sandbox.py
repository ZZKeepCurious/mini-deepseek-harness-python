"""shell 层测试：本地执行器、沙箱消费归因、工具接线与 headless 装配。

上游对照：packages/shell/{bash-local,bash-sandbox}/src 契约——
danger 直通 / confine 包裹 / 三路归因（runner 失败 > denial > 普通退出）。
"""
from __future__ import annotations

import errno
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from miniharness.cli.default_tools import bash_tool, default_tools
from miniharness.cli.headless import run_headless
from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.core.session.persistence import JsonlPersistence
from miniharness.llm import FakeLlmAdapter
from miniharness.seams.sandbox_local import SandboxUnavailableError
from miniharness.seams.sandbox_policy import SandboxPolicyService, set_sandbox_mode
from miniharness.shell import LocalBashExecutor, SandboxBashExecutor, install_bash_executor
from miniharness.shell.helpers import (
    classify_denial,
    classify_runner_failure,
    is_runner_spawn_failure,
    matches_signature,
)


class _IO:
    def __init__(self):
        self.stdout = []
        self.stderr = []
        self.exit_codes = []

    class _Sink:
        def __init__(self, target):
            self._target = target

        def write(self, chunk):
            self._target.append(chunk)

    @property
    def out(self):
        return self._Sink(self.stdout)

    @property
    def err(self):
        return self._Sink(self.stderr)

    def exit(self, code):
        self.exit_codes.append(code)


def _confine_result(argv=None, enforcement="full",
                    denial_signatures=("operation not permitted",), rules=()):
    return {
        "argv": ["fake-runner", "--", *(argv or [])],
        "enforcement": enforcement,
        "denialSignatures": list(denial_signatures),
        "runnerFailureRules": list(rules),
    }


def _sandbox_ctx(policy_config=None, confine_result=None):
    """装好 sandbox + sandboxPolicy 的 ctx（provider 为可断言 stub）。"""
    ctx = Context(name="t")
    calls = []

    def confine(argv, policy):
        calls.append((list(argv), dict(policy)))
        result = confine_result or _confine_result(argv)
        return result

    provider = SimpleNamespace(confine=confine, calls=calls)
    ctx.provide("sandbox", provider)
    policy = SandboxPolicyService(ctx, policy_config)
    return ctx, provider, policy


class MatchesSignatureTest(unittest.TestCase):
    def test_requires_nonzero_exit(self):
        self.assertFalse(matches_signature(0, "operation not permitted", ["op"]))
        self.assertFalse(matches_signature(None, "operation not permitted", ["op"]))

    def test_case_insensitive_substring(self):
        self.assertTrue(matches_signature(1, "Operation NOT Permitted", ["operation not permitted"]))
        self.assertFalse(matches_signature(1, "all good", ["operation not permitted"]))


class ClassifyDenialTest(unittest.TestCase):
    def test_denial_by_backend_dialect(self):
        result = {"exitCode": 1, "stderr": "bwrap: Can't find source path /x"}
        self.assertTrue(classify_denial(result, ["can't find source path"]))
        self.assertFalse(classify_denial(result, ["denied: operation"]))


class ClassifyRunnerFailureTest(unittest.TestCase):
    def test_zero_or_null_exit_is_never_runner_failure(self):
        rules = [{"fatalSignatures": ["boom"]}]
        self.assertIsNone(classify_runner_failure(0, "boom", rules))
        self.assertIsNone(classify_runner_failure(None, "boom", rules))

    def test_fatal_line_returned_as_detail(self):
        rules = [{"fatalSignatures": ["bwrap: failed to setup"]}]
        hit = classify_runner_failure(
            1, "warning: x\nBWRAP: FAILED TO SETUP namespace\nmore", rules)
        self.assertEqual(hit, {"detail": "BWRAP: FAILED TO SETUP namespace"})

    def test_informational_exact_lines_excluded(self):
        rules = [{"fatalSignatures": ["partial"],
                  "informationalLines": ["landlock-run: partial enforcement (older Landlock ABI)"]}]
        stderr = ("landlock-run: partial enforcement (older Landlock ABI)\n"
                  "landlock-run: partial enforcement (older Landlock ABI) extra")
        hit = classify_runner_failure(126, stderr, rules)
        # 精确信息行被排除；同词但非精确行的命中仍是 fatal 证据
        self.assertIsNotNone(hit)

    def test_allowed_exit_codes_gate(self):
        rules = [{"fatalSignatures": ["boom"], "allowedExitCodes": [125]}]
        self.assertIsNone(classify_runner_failure(1, "boom", rules))
        self.assertIsNotNone(classify_runner_failure(125, "boom", rules))

    def test_blank_signature_never_matches(self):
        rules = [{"fatalSignatures": ["   "]}, {"fatalSignatures": [""]}]
        self.assertIsNone(classify_runner_failure(1, "anything", rules))

    def test_no_evidence_returns_none(self):
        self.assertIsNone(classify_runner_failure(1, "plain command error", []))


class IsRunnerSpawnFailureTest(unittest.TestCase):
    def test_enoent_on_runner_program_attributed(self):
        err = OSError(errno.ENOENT, "No such file", "/usr/bin/bwrap")
        self.assertTrue(is_runner_spawn_failure(err, "/usr/bin/bwrap", os.getcwd()))

    def test_eacces_attributed_other_errno_not(self):
        self.assertTrue(is_runner_spawn_failure(
            OSError(errno.EACCES, "denied", "bwrap"), "bwrap", "."))
        self.assertFalse(is_runner_spawn_failure(
            OSError(errno.EPERM, "nope", "bwrap"), "bwrap", "."))

    def test_mismatched_filename_not_attributed(self):
        err = OSError(errno.ENOENT, "gone", "/other/thing")
        self.assertFalse(is_runner_spawn_failure(err, "bwrap", "."))

    def test_unusable_workdir_blocks_attribution(self):
        err = OSError(errno.ENOENT, "No such file", "bwrap")
        self.assertFalse(is_runner_spawn_failure(err, "bwrap", "Z:/definitely/missing/dir"))

    def test_non_os_error_not_attributed(self):
        self.assertFalse(is_runner_spawn_failure(ValueError("x"), "bwrap", "."))


class LocalBashExecutorTest(unittest.TestCase):
    def test_registers_shell_tag_and_default_program(self):
        ctx = Context(name="t")
        exe = LocalBashExecutor(ctx)
        self.assertIs(ctx.get("shell"), exe)
        self.assertEqual(exe.program, ["bash", "-c"])

    def test_resolve_passthrough(self):
        exe = LocalBashExecutor(Context(name="t"))
        request = {"command": "ls", "workdir": "/tmp"}
        self.assertEqual(exe.resolve(request), request)


class SandboxBashExecutorTest(unittest.TestCase):
    def _exe(self, policy_config=None, confine_result=None):
        ctx, provider, policy = _sandbox_ctx(policy_config, confine_result)
        exe = SandboxBashExecutor(ctx)
        return exe, provider, policy

    def test_requires_sandbox_and_policy_services(self):
        with self.assertRaises(ValueError):
            SandboxBashExecutor(Context(name="bare"))

    def test_mode_advertises_deployment_default(self):
        exe, _, _ = self._exe({"mode": "workspace-write"})
        self.assertEqual(exe.mode, "workspace-write")
        self.assertEqual(exe.sandbox_mode, "workspace-write")

    def test_resolve_fills_deployment_policy_when_absent(self):
        exe, _, policy = self._exe()
        spec = exe.resolve({"command": "ls"})
        self.assertEqual(spec["sandboxPolicy"], policy.resolve())

    def test_resolve_keeps_explicit_policy(self):
        exe, _, _ = self._exe()
        explicit = {"mode": "read-only", "workspaceRoot": "/w"}
        self.assertIs(exe.resolve({"command": "ls", "sandboxPolicy": explicit})["sandboxPolicy"],
                      explicit)

    def test_danger_full_access_passes_through_without_confinement(self):
        exe, provider, _ = self._exe({"mode": "danger-full-access"})
        captured = {}
        exe.spawn_argv = lambda spec, argv: captured.update(spec=spec, argv=argv) or {
            "exitCode": 0, "stdout": "ok", "stderr": ""}
        result = exe.run({"command": "echo hi"})
        self.assertEqual(captured["argv"], ["bash", "-c", "echo hi"])
        self.assertEqual(provider.calls, [])
        self.assertEqual(result["sandbox"], {"mode": "danger-full-access", "denied": False})

    def test_confined_success_reports_enforcement(self):
        exe, provider, _ = self._exe(
            None, _confine_result(enforcement="partial"))
        exe.spawn_argv = lambda spec, argv: {
            "exitCode": 0, "stdout": "", "stderr": ""}
        result = exe.run({"command": "true"})
        argv, policy = provider.calls[0]
        self.assertEqual(argv[:1], ["bash"])
        self.assertEqual(result["sandbox"],
                         {"mode": "read-only", "denied": False, "enforcement": "partial"})

    def test_denial_reported_not_raised(self):
        exe, _, _ = self._exe()
        exe.spawn_argv = lambda spec, argv: {
            "exitCode": 1, "stdout": "", "stderr": "Operation Not Permitted: /etc"}
        result = exe.run({"command": "cat /etc/passwd"})
        self.assertTrue(result["sandbox"]["denied"])

    def test_runner_failure_raises_unavailable_and_outranks_denial(self):
        rules = [{"fatalSignatures": ["bwrap: failed to setup"]}]
        exe, _, _ = self._exe(
            None, _confine_result(denial_signatures=["failed to setup"], rules=rules))
        exe.spawn_argv = lambda spec, argv: {
            "exitCode": 1, "stdout": "",
            "stderr": "bwrap: Failed to setup namespace: Operation not permitted"}
        with self.assertRaises(SandboxUnavailableError) as caught:
            exe.run({"command": "anything"})
        self.assertIn("Failed to setup namespace", str(caught.exception))

    def test_spawn_enoent_attributed_as_unavailable(self):
        exe, _, _ = self._exe()

        def boom(spec, argv):
            raise OSError(errno.ENOENT, "No such file", argv[0])

        exe.spawn_argv = boom
        with self.assertRaises(SandboxUnavailableError) as caught:
            exe.run({"command": "x"})
        self.assertIn("No such file", str(caught.exception))

    def test_unrelated_spawn_oserror_propagates_raw(self):
        exe, _, _ = self._exe()

        def boom(spec, argv):
            raise OSError(errno.EACCES, "denied", "/some/other/file")

        exe.spawn_argv = boom
        with self.assertRaises(OSError) as caught:
            exe.run({"command": "x"})
        self.assertNotIsInstance(caught.exception, SandboxUnavailableError)

    def test_confine_uses_inner_bash_c_shape(self):
        exe, provider, _ = self._exe()
        exe.confine("echo hi", {"mode": "read-only"})
        argv, _ = provider.calls[0]
        self.assertEqual(argv, ["bash", "-c", "echo hi"])


class InstallBashExecutorTest(unittest.TestCase):
    def test_auto_local_without_stack(self):
        exe = install_bash_executor(Context(name="t"))
        self.assertIsInstance(exe, LocalBashExecutor)

    def test_auto_sandboxed_with_full_stack(self):
        ctx, _, _ = _sandbox_ctx()
        self.assertIsInstance(install_bash_executor(ctx), SandboxBashExecutor)

    def test_explicit_flags_override_detection(self):
        self.assertIsInstance(install_bash_executor(Context(name="t"), sandboxed=False),
                              LocalBashExecutor)
        ctx, _, _ = _sandbox_ctx()
        self.assertIsInstance(install_bash_executor(ctx, sandboxed=False),
                              LocalBashExecutor)


class BashToolTest(unittest.TestCase):
    class _FakeShell:
        def __init__(self, result):
            self.result = result
            self.requests = []

        def run(self, request):
            self.requests.append(request)
            return self.result

    def _exec_with_session(self, session):
        agent = SimpleNamespace(session=session)
        return SimpleNamespace(agent=agent)

    def test_formats_stdout_stderr_and_sandbox_facts(self):
        shell = self._FakeShell({
            "exitCode": 0, "stdout": "hi\n", "stderr": "warn\n",
            "sandbox": {"mode": "read-only", "denied": False, "enforcement": "full"}})
        out = bash_tool(shell).execute({"cmd": "echo hi"}, object())
        self.assertEqual(out, "stdout: hi\nstderr: warn\n"
                              "[sandbox mode=read-only enforcement=full denied=false]")

    def test_nonzero_exit_appends_exit_code_without_iserror(self):
        shell = self._FakeShell({"exitCode": 2, "stdout": "", "stderr": "boom"})
        out = bash_tool(shell).execute({"cmd": "x"}, object())
        self.assertIsInstance(out, str)
        self.assertIn("stderr: boom", out)
        self.assertIn("exit code: 2", out)

    def test_denial_returns_tool_error(self):
        shell = self._FakeShell({
            "exitCode": 1, "stdout": "", "stderr": "Operation not permitted",
            "sandbox": {"mode": "read-only", "denied": True, "enforcement": "full"}})
        out = bash_tool(shell).execute({"cmd": "cat /etc/shadow"}, object())
        self.assertIsInstance(out, dict)
        self.assertTrue(out["isError"])
        self.assertIn("sandbox denied command", out["error"])

    def test_resolves_policy_per_call_from_caller_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session("s1", meta={"cwd": tmp})
            set_sandbox_mode(session, "danger-full-access")
            shell = self._FakeShell({"exitCode": 0, "stdout": "", "stderr": ""})
            policy = SandboxPolicyService(Context(name="t"))
            tool = bash_tool(shell, policy)
            tool.execute({"cmd": "x"}, self._exec_with_session(session))
            sent = shell.requests[0]
            self.assertEqual(sent["command"], "x")
            self.assertEqual(sent["sandboxPolicy"]["mode"], "danger-full-access")
            self.assertEqual(Path(sent["sandboxPolicy"]["workspaceRoot"]),
                             Path(os.path.realpath(tmp)))
            self.assertEqual(sent["sandboxPolicy"]["sessionId"], "s1")

    def test_no_session_falls_back_to_deployment_resolution(self):
        shell = self._FakeShell({"exitCode": 0, "stdout": "", "stderr": ""})
        policy = SandboxPolicyService(Context(name="t"), {"mode": "workspace-write"})
        bash_tool(shell, policy).execute({"cmd": "x"}, SimpleNamespace(agent=None))
        self.assertEqual(shell.requests[0]["sandboxPolicy"]["mode"], "workspace-write")

    def test_stub_preserved_without_shell_service(self):
        reg = default_tools(Context(name="t"))
        tool = reg.resolve("bash")
        self.assertEqual(tool.execute({"cmd": "ls"}, None), "stdout: ls")

    def test_real_tool_registered_when_shell_present(self):
        ctx, _, _ = _sandbox_ctx()
        install_bash_executor(ctx)
        reg = default_tools(ctx)
        self.assertIn("bash", reg.names())
        tool = reg.resolve("bash")
        self.assertIsNot(getattr(tool.execute, "__closure__", None), None)
        # 管线外直接驱动：走真执行器路径（stub provider + 立即结算）
        exe = ctx.get("shell")
        exe.spawn_argv = lambda spec, argv: {"exitCode": 0, "stdout": "ok", "stderr": ""}
        out = tool.execute({"cmd": "true"}, self._exec_with_session(Session("s1")))
        self.assertIn("[sandbox mode=read-only", out)


class HeadlessSandboxStackTest(unittest.TestCase):
    """run_headless(sandbox=True) 装配端到端：stub runner + fake spawn，不触宿主。"""

    def test_installs_stack_and_executes_confined_bash(self):
        io = _IO()
        seen = []

        class StubProvider:
            def confine(self, argv, policy):
                return _confine_result(argv)

        persistence = SimpleNamespace(
            append=lambda sid, ev, cwd=None: seen.append(ev), flush=lambda: None)
        run_args = {}
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("miniharness.seams.sandbox_local.LocalSandboxProvider",
                            lambda *a, **k: StubProvider()), \
                 mock.patch("miniharness.shell.bash_local.subprocess.run") as run:
                run.return_value = subprocess.CompletedProcess([], 0, stdout="hi", stderr="")
                ctx = Context(name="headless")
                run_headless(
                    "跑一下",
                    adapter=FakeLlmAdapter(
                        tool_call={"name": "bash", "arguments": {"cmd": "echo hi"}},
                        final_text="完成"),
                    ctx=ctx, persistence=persistence,
                    stdout=io.out, stderr=io.err, exit_fn=io.exit,
                    sandbox={"mode": "read-only"})
                run_args = run.call_args.args

        self.assertEqual(io.exit_codes, [0])
        self.assertEqual(io.stdout, ["完成\n"])
        self.assertIsInstance(ctx.get("shell"), SandboxBashExecutor)
        self.assertEqual(ctx.get("sandboxPolicy").default_mode, "read-only")
        self.assertIsInstance(ctx.get("sandbox"), StubProvider)
        argv = run_args[0]
        self.assertEqual(argv[0], "fake-runner")
        self.assertEqual(argv[-3:], ["bash", "-c", "echo hi"])
        marker = "[sandbox mode=read-only enforcement=full denied=false]"
        self.assertTrue(any(marker in json.dumps(ev, default=str) for ev in seen),
                        f"sandbox facts missing from events: {seen}")

    def test_sandbox_false_keeps_stub_tools(self):
        io = _IO()
        ctx = Context(name="headless")
        run_headless(
            "看看", adapter=FakeLlmAdapter(final_text="ok"),
            ctx=ctx, stdout=io.out, stderr=io.err, exit_fn=io.exit)
        self.assertIsNone(ctx.get("shell"))
        self.assertIsNone(ctx.get("sandboxPolicy"))


if __name__ == "__main__":
    unittest.main()
