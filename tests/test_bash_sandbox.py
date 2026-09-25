"""shell 层测试：执行器句柄、沙箱消费归因、bash 工具接线与 headless 装配。

上游对照：packages/shell/{shell,bash-local,bash-sandbox,tool-bash}/src 契约——
execute 句柄 / onExpiry / observed / 三路归因（runner 失败 > denial > 普通退出）/
promoteOnTimeout 提升后台作业 / stopped / render。
"""
from __future__ import annotations

import asyncio
import errno
import json
import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from miniharness.cli.headless import run_headless
from miniharness.core.scope import Context
from miniharness.core.session import Session
from miniharness.jobs import LocalJobRegistry
from miniharness.llm import FakeLlmAdapter
from miniharness.seams.sandbox_local import SandboxUnavailableError
from miniharness.seams.sandbox_policy import SandboxPolicyService, set_sandbox_mode
from miniharness.shell import (
    LocalBashExecutor,
    SandboxBashExecutor,
    install_bash_executor,
    settled_execution,
)
from miniharness.shell.helpers import (
    classify_denial,
    classify_runner_failure,
    is_runner_spawn_failure,
    matches_signature,
)
from miniharness.shell import install_shell_env
from miniharness.tool_bash import (
    create_bash_tool,
    process_outcome,
    process_sources,
    render_promoted,
    render_result,
    ring_delta,
)
from miniharness.tool_bash.index import _start_job, _wait_on_job


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

    def test_resolve_fills_defaults(self):
        exe = LocalBashExecutor(Context(name="t"), {"cwd": os.getcwd()})
        spec = exe.resolve({"command": "ls"})
        self.assertEqual(spec["command"], "ls")
        self.assertEqual(spec["workdir"], os.getcwd())
        self.assertEqual(spec["timeoutMs"], 120_000)
        self.assertEqual(spec["onExpiry"], "kill")
        self.assertEqual(spec["stdoutMaxBytes"], 64_000)

    def test_resolve_caps_timeout_and_keeps_overrides(self):
        exe = LocalBashExecutor(Context(name="t"), {"maxTimeoutMs": 1_000})
        spec = exe.resolve({"command": "ls", "timeoutMs": 9_999, "onExpiry": "none"})
        self.assertEqual(spec["timeoutMs"], 1_000)
        self.assertEqual(spec["onExpiry"], "none")

    def test_resolve_rejects_non_positive_timeout(self):
        exe = LocalBashExecutor(Context(name="t"))
        with self.assertRaises(ValueError):
            exe.resolve({"command": "ls", "timeoutMs": 0})


class LocalBashExecutionTest(unittest.TestCase):
    """真实 `python -c` 进程上的执行句柄契约（跨平台，不依赖 bash）。"""

    def _executor(self, **config):
        config.setdefault("program", [sys.executable, "-c"])
        return LocalBashExecutor(Context(name="t"), config)

    def test_foreground_result_collects_streams(self):
        exe = self._executor()
        execution = exe.execute(exe.resolve(
            {"command": "import sys; sys.stdout.write('hi')"}))
        result = execution.result()
        self.assertEqual(result["exitCode"], 0)
        self.assertFalse(result["timedOut"])
        self.assertEqual(result["stdout"]["text"], "hi")
        self.assertEqual(result["stdout"]["truncated"], False)

    def test_timeout_kills_and_flags_timed_out(self):
        exe = self._executor()
        execution = exe.execute(exe.resolve(
            {"command": "import time; time.sleep(5)", "timeoutMs": 150}))
        result = execution.result()
        self.assertTrue(result["timedOut"])
        self.assertEqual(execution.status, "killed")

    def test_on_expiry_none_arms_no_deadline(self):
        exe = self._executor()
        execution = exe.execute(exe.resolve(
            {"command": "import sys; sys.stdout.write('ok')",
             "timeoutMs": 150, "onExpiry": "none"}))
        result = execution.result()
        self.assertFalse(result["timedOut"])
        self.assertEqual(result["stdout"]["text"], "ok")

    def test_already_aborted_signal_is_treated_as_fired(self):
        exe = self._executor()
        signal = threading.Event()
        signal.set()
        execution = exe.execute(exe.resolve(
            {"command": "import sys; sys.stdout.write('nope')", "signal": signal}))
        result = execution.result()
        self.assertTrue(result["aborted"])
        self.assertEqual(result["stdout"]["text"], "")

    def test_observed_is_non_consuming_and_read_output_consumes(self):
        exe = self._executor()
        execution = exe.execute(exe.resolve(
            {"command": "import sys; sys.stdout.write('abc'); sys.stdout.flush()"}))
        result = execution.result()
        self.assertEqual(result["stdout"]["text"], "abc")
        # observed 在结算后重复读仍拿到全部字节（非消耗）
        self.assertEqual(execution.observed["stdout"].read_from(0)["text"], "abc")
        self.assertEqual(execution.observed["stdout"].read_from(0)["text"], "abc")
        # readOutput 消费：首次拿增量，二次为空
        self.assertEqual(execution.read_output()["delta"], "abc")
        self.assertEqual(execution.read_output()["delta"], "")

    def test_kill_stops_running_process(self):
        exe = self._executor()
        execution = exe.execute(exe.resolve(
            {"command": "import time; time.sleep(30)", "onExpiry": "none"}))
        self.assertTrue(execution.kill())
        execution.result()
        self.assertEqual(execution.status, "killed")
        self.assertFalse(execution.kill())

    def test_spawn_failure_rejects_result_not_done(self):
        exe = LocalBashExecutor(Context(name="t"),
                                {"program": ["definitely-not-a-real-program-xyz"]})
        execution = exe.execute(exe.resolve({"command": "whatever"}))
        execution.done.result(timeout=5)  # done 正常结算
        with self.assertRaises(OSError):
            execution.result()


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
        exe.spawn_argv = lambda spec, argv, on_settled=None: captured.update(
            spec=spec, argv=argv) or settled_execution(0, "", "")
        result = exe.execute({"command": "echo hi"}).result()
        self.assertEqual(captured["argv"], ["bash", "-c", "echo hi"])
        self.assertEqual(provider.calls, [])
        self.assertEqual(result["sandbox"], {"mode": "danger-full-access", "denied": False})

    def test_confined_success_reports_enforcement(self):
        exe, provider, _ = self._exe(None, _confine_result(enforcement="partial"))
        exe.spawn_argv = lambda spec, argv, on_settled=None: settled_execution(0, "", "")
        result = exe.execute({"command": "true"}).result()
        argv, policy = provider.calls[0]
        self.assertEqual(argv[:1], ["bash"])
        self.assertEqual(result["sandbox"],
                         {"mode": "read-only", "denied": False, "enforcement": "partial"})

    def test_denial_reported_not_raised(self):
        exe, _, _ = self._exe()
        exe.spawn_argv = lambda spec, argv, on_settled=None: settled_execution(
            1, "", "Operation Not Permitted: /etc")
        result = exe.execute({"command": "cat /etc/passwd"}).result()
        self.assertTrue(result["sandbox"]["denied"])

    def test_runner_failure_raises_unavailable_and_outranks_denial(self):
        rules = [{"fatalSignatures": ["bwrap: failed to setup"]}]
        exe, _, _ = self._exe(
            None, _confine_result(denial_signatures=["failed to setup"], rules=rules))
        exe.spawn_argv = lambda spec, argv, on_settled=None: settled_execution(
            1, "", "bwrap: Failed to setup namespace: Operation not permitted")
        with self.assertRaises(SandboxUnavailableError) as caught:
            exe.execute({"command": "anything"}).result()
        self.assertIn("Failed to setup namespace", str(caught.exception))

    def test_spawn_enoent_attributed_as_unavailable(self):
        exe, _, _ = self._exe()

        def fake_spawn(spec, argv, on_settled=None):
            execution = settled_execution(None, "", "")
            execution._spawn_error = OSError(errno.ENOENT, "No such file", argv[0])
            return execution

        exe.spawn_argv = fake_spawn
        with self.assertRaises(SandboxUnavailableError) as caught:
            exe.execute({"command": "x"}).result()
        self.assertIn("No such file", str(caught.exception))

    def test_unrelated_spawn_oserror_propagates_raw(self):
        exe, _, _ = self._exe()

        def fake_spawn(spec, argv, on_settled=None):
            execution = settled_execution(None, "", "")
            execution._spawn_error = OSError(errno.EACCES, "denied", "/some/other/file")
            return execution

        exe.spawn_argv = fake_spawn
        with self.assertRaises(OSError) as caught:
            exe.execute({"command": "x"}).result()
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


class RenderTest(unittest.TestCase):
    def _result(self, **overrides):
        base = {
            "exitCode": 0, "signal": None, "timedOut": False, "aborted": False,
            "timeoutMs": 1000,
            "stdout": {"text": "hi", "truncated": False},
            "stderr": {"text": "", "truncated": False},
        }
        base.update(overrides)
        return base

    def test_stdout_then_stderr_marker(self):
        text = render_result(self._result(
            stdout={"text": "out", "truncated": False},
            stderr={"text": "err", "truncated": False}))
        self.assertEqual(text, "out\n[stderr]\nerr")

    def test_nonzero_exit_marker_and_signal(self):
        self.assertTrue(render_result(self._result(exitCode=2)).endswith("[exit code: 2]"))
        self.assertTrue(render_result(self._result(signal="SIGKILL", exitCode=None))
                        .endswith("[killed by signal: SIGKILL]"))

    def test_timed_out_and_stopped_markers(self):
        text = render_result(self._result(timedOut=True, stopped="human stopped it"))
        self.assertIn("[timed out after 1000ms]", text)
        self.assertIn("[stopped: human stopped it]", text)

    def test_denial_marker(self):
        text = render_result(self._result(
            exitCode=1, sandbox={"mode": "read-only", "denied": True}))
        self.assertIn("[sandbox: file access denied under read-only mode]", text)

    def test_promoted_rendering(self):
        text = render_promoted({"jobId": "bash-3", "timeoutMs": 500, "output": "partial"})
        self.assertIn("partial", text)
        self.assertIn("[still running after 500ms; moved to background job bash-3]", text)


class BackgroundAdaptationTest(unittest.TestCase):
    def test_process_outcome_completed_and_killed(self):
        completed = settled_execution(0, "", "")
        self.assertEqual(process_outcome(completed), {"status": "completed", "detail": "exit code: 0"})
        killed = settled_execution(None, "", "", signal="SIGTERM")
        self.assertEqual(process_outcome(killed),
                         {"status": "killed", "detail": "signal: SIGTERM"})

    def test_process_outcome_appends_sandbox_denial_note(self):
        proc = settled_execution(
            1, "", "", sandbox={"mode": "read-only", "denied": True})
        outcome = process_outcome(proc)
        self.assertIn("file access denied under read-only mode", outcome["detail"])

    def test_process_sources_reads_observed_offsets(self):
        proc = settled_execution(0, "abc", "err")
        sources = {source["channel"]: source for source in process_sources(lambda: proc)}
        first = sources["stdout"]["read"](0)
        self.assertEqual(first["text"], "abc")
        self.assertEqual(sources["stdout"]["read"](0)["text"], "abc")
        self.assertEqual(sources["stderr"]["read"](0)["text"], "err")

    def test_ring_delta_merges_stderr_section(self):
        chunks = [{"channel": "stdout", "text": "out\n"},
                  {"channel": "stderr", "text": "err"},
                  {"channel": "log", "text": "hidden"}]
        # 上游 ringDelta 只把 stderr 分出来；log 与 stdout 同段（bash 源不出 log 块）
        self.assertEqual(ring_delta(chunks), "out\nhidden\n[stderr]\nerr")


class BashToolTest(unittest.TestCase):
    class _FakeShell:
        def __init__(self, execution, spec=None):
            self.execution = execution
            self.spec = spec or {"timeoutMs": 1000, "onExpiry": "kill"}
            self.requests = []

        def resolve(self, request):
            spec = {**self.spec, **request}
            self.requests.append(spec)
            return spec

        def execute(self, spec):
            return self.execution

    def _exec_with_session(self, session):
        agent = SimpleNamespace(session=session, id=session.session_id)
        return SimpleNamespace(agent=agent, signal=None)

    def _tool(self, shell):
        ctx = Context(name="t")
        return create_bash_tool(ctx, shell)

    def test_foreground_renders_stdout_and_exit(self):
        shell = self._FakeShell(settled_execution(2, "boom", ""))
        tool = self._tool(shell)
        value = asyncio.run(tool.execute(
            {"command": "x", "description": "run x"},
            self._exec_with_session(Session("s1"))))
        self.assertEqual(value["kind"], "foreground")
        self.assertEqual(value["exitCode"], 2)
        rendered = tool.render({"command": "x"}, value)[0]["text"]
        self.assertIn("boom", rendered)
        self.assertIn("[exit code: 2]", rendered)

    def test_denial_is_marker_not_tool_error(self):
        shell = self._FakeShell(settled_execution(
            1, "", "Operation not permitted",
            sandbox={"mode": "read-only", "denied": True}))
        tool = self._tool(shell)
        value = asyncio.run(tool.execute(
            {"command": "cat /etc/shadow", "description": "read shadow"},
            self._exec_with_session(Session("s1"))))
        rendered = tool.render({"command": "x"}, value)[0]["text"]
        self.assertIn("[sandbox: file access denied under read-only mode]", rendered)

    def test_resolves_policy_and_workdir_per_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Session("s1", meta={"cwd": tmp})
            set_sandbox_mode(session, "danger-full-access")
            shell = self._FakeShell(settled_execution(0, "", ""))
            ctx = Context(name="t")
            policy = SandboxPolicyService(ctx)
            tool = create_bash_tool(ctx, shell)
            self.assertIs(ctx.get("sandboxPolicy"), policy)
            asyncio.run(tool.execute(
                {"command": "x", "description": "run x"},
                self._exec_with_session(session)))
            sent = shell.requests[0]
            self.assertEqual(sent["command"], "x")
            self.assertEqual(sent["sandboxPolicy"]["mode"], "danger-full-access")
            self.assertEqual(Path(sent["workdir"]), Path(os.path.realpath(tmp)))

    def test_validate_rejects_blank_command(self):
        shell = self._FakeShell(settled_execution(0, "", ""))
        tool = self._tool(shell)
        with self.assertRaises(ValueError):
            asyncio.run(tool.execute({"command": "  ", "description": "x"},
                                     self._exec_with_session(Session("s1"))))


class BashPromotionTest(unittest.TestCase):
    """真实 `python -c` 进程 + 真实 jobs 注册表上的前台提升。"""

    def _registry(self):
        ctx = Context(name="t")
        registry = LocalJobRegistry(ctx)
        registry.attach_controller("test", ctx)
        self.addCleanup(ctx.dispose)
        return registry

    def test_timeout_promotes_to_background_job(self):
        registry = self._registry()
        shell = LocalBashExecutor(Context(name="t"),
                                  {"program": [sys.executable, "-c"]})
        spec = shell.resolve({
            "command": "import time; time.sleep(30); print('done')",
            "timeoutMs": 200, "onExpiry": "none"})
        attached = _start_job(shell, registry, "sleepy", None, spec, ())
        try:
            value = _wait_on_job(registry, attached, None, spec, ())
            self.assertEqual(value["kind"], "promoted")
            self.assertEqual(value["jobId"], attached["id"])
            self.assertIn("moved to background job", render_promoted(value))
        finally:
            registry.kill(attached["id"], None, "test cleanup")
            settled = registry.wait(attached["id"], 10_000, None)
            if settled["status"] not in ("running", "stopping"):
                registry.remove(attached["id"], None)

    def test_fast_foreground_job_settles_foreground(self):
        registry = self._registry()
        shell = LocalBashExecutor(Context(name="t"),
                                  {"program": [sys.executable, "-c"]})
        spec = shell.resolve({
            "command": "import sys; sys.stdout.write('quick')",
            "timeoutMs": 5_000, "onExpiry": "none"})
        attached = _start_job(shell, registry, "quick", None, spec, ())
        value = _wait_on_job(registry, attached, None, spec, ())
        self.assertEqual(value["kind"], "foreground")
        self.assertEqual(value["stdout"]["text"], "quick")


class ShellEnvRegistryTest(unittest.TestCase):
    def test_collects_builtins_and_session_id(self):
        ctx = Context(name="t")
        install_shell_env(ctx)
        registry = ctx.get("shellEnv")
        session = Session("sess-9")
        agent = SimpleNamespace(session=session, id="sess-9")
        values = registry.collect(SimpleNamespace(agent=agent))
        self.assertEqual(values["DSH_SHELL"], "1")
        self.assertEqual(values["DSH_SESSION_ID"], "sess-9")
        self.assertIn("DSH_HOME", values)

    def test_profile_context_populates_reserved_keys(self):
        ctx = Context(name="t")
        ctx.provide("profileContext", {"name": "headless", "dir": "/p/headless"})
        install_shell_env(ctx)
        values = ctx.get("shellEnv").collect(SimpleNamespace(agent=None))
        self.assertEqual(values["DSH_PROFILE"], "headless")
        self.assertEqual(values["DSH_PROFILE_DIR"], "/p/headless")

    def test_reserved_and_duplicate_keys_fail_loud(self):
        ctx = Context(name="t")
        registry = install_shell_env(ctx)
        with self.assertRaises(ValueError):
            registry.register({"name": "bad", "variables": {"DSH_PROFILE": {"description": "x"}},
                               "resolve": lambda _e: {}})
        registry.register({"name": "ok", "variables": {"DSH_CUSTOM": {"description": "x"}},
                           "resolve": lambda _e: {"DSH_CUSTOM": "v"}})
        with self.assertRaises(ValueError):
            registry.register({"name": "ok", "variables": {}, "resolve": lambda _e: {}})
        values = registry.collect(SimpleNamespace(agent=None))
        self.assertEqual(values["DSH_CUSTOM"], "v")


class HeadlessSandboxStackTest(unittest.TestCase):
    """run_headless(sandbox=True) 装配端到端：stub runner + 真实进程。"""

    def test_installs_stack_and_executes_confined_bash(self):
        io = _IO()
        seen = []

        class StubProvider:
            def confine(self, argv, policy):
                # 真实的受限 argv：忽略包装，直接跑一段 Python
                return {
                    "argv": [sys.executable, "-c", "import sys; sys.stdout.write('hi')"],
                    "enforcement": "full",
                    "denialSignatures": [],
                    "runnerFailureRules": [],
                }

        persistence = SimpleNamespace(
            append=lambda sid, ev, cwd=None: seen.append(ev), flush=lambda: None)
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch("miniharness.seams.sandbox_local.LocalSandboxProvider",
                            lambda *a, **k: StubProvider()):
                ctx = Context(name="headless")
                run_headless(
                    "跑一下",
                    adapter=FakeLlmAdapter(
                        tool_call={"name": "bash",
                                   "arguments": {"command": "echo hi",
                                                 "description": "echo hi"}},
                        final_text="完成"),
                    ctx=ctx, persistence=persistence,
                    stdout=io.out, stderr=io.err, exit_fn=io.exit,
                    sandbox={"mode": "read-only"})

        self.assertEqual(io.exit_codes, [0])
        self.assertEqual(io.stdout, ["完成\n"])
        self.assertIsInstance(ctx.get("shell"), SandboxBashExecutor)
        self.assertEqual(ctx.get("sandboxPolicy").default_mode, "read-only")
        self.assertIsInstance(ctx.get("sandbox"), StubProvider)
        self.assertIsNotNone(ctx.get("shellEnv"))
        marker = "[sandbox mode=read-only enforcement=full denied=false]"
        self.assertTrue(any(marker in json.dumps(ev, default=str) for ev in seen)
                        or any("hi" in json.dumps(ev, default=str) for ev in seen),
                        f"sandbox bash output missing from events: {seen}")

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
