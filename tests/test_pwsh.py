"""pwsh 变体（M16）验收。

对齐上游 `packages/shell/pwsh-local/tests/executor.spec.ts` +
`packages/shell/pwsh-sandbox/tests/sandbox.spec.ts` +
`packages/shell/tool-pwsh/tests/tools.spec.ts`。

本机无 pwsh 二进制（win32 下 Windows PowerShell 5.1 兜底候选亦存在），
真实执行用例门控跳过；argv 构造、resolve、编码前导、沙箱装饰、工具 schema/
render 以纯单元路径验收。
"""
import os
import unittest

from miniharness.core.scope import Context
from miniharness.shell import (
    ENCODING_PREAMBLE,
    PwshLocalExecutor,
    SandboxPwshExecutor,
    candidate_pwsh_paths,
    install_pwsh_executor,
    resolve_pwsh_path,
    settled_execution,
)
from miniharness.tool_pwsh import create_pwsh_tool, render_pwsh_result, resolve_config


class PwshLocalTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="pwsh")
        self.executor = PwshLocalExecutor(self.ctx, {"pwshPath": "pwsh"})

    def tearDown(self):
        self.ctx.dispose()

    def test_argv_shape(self):
        argv = self.executor.pwsh_argv("Write-Output hi")
        self.assertEqual(argv[0], "pwsh")
        self.assertEqual(argv[1:4], ["-NoLogo", "-NoProfile", "-NonInteractive"])
        self.assertEqual(argv[4], "-Command")
        self.assertTrue(argv[5].startswith(ENCODING_PREAMBLE))
        self.assertTrue(argv[5].endswith("Write-Output hi"))

    def test_encoding_preamble(self):
        self.assertIn("[Console]::OutputEncoding", ENCODING_PREAMBLE)
        self.assertIn("$OutputEncoding", ENCODING_PREAMBLE)

    def test_resolve(self):
        spec = self.executor.resolve({"command": "Get-Date"})
        self.assertEqual(spec["command"], "Get-Date")
        self.assertEqual(spec["onExpiry"], "kill")
        self.assertIn("timeoutMs", spec)
        self.assertIn("workdir", spec)
        self.assertIn("stdoutMaxBytes", spec)

    def test_resolve_pwsh_path(self):
        self.assertEqual(resolve_pwsh_path("C:/x/pwsh.exe", platform="nt"), "C:/x/pwsh.exe")
        self.assertEqual(resolve_pwsh_path(None, platform="posix"), "pwsh")

    def test_candidate_paths(self):
        candidates = candidate_pwsh_paths({
            "ProgramFiles": r"C:\PF", "PATH": "", "SystemRoot": r"C:\WINDOWS"})
        self.assertEqual(candidates, [
            r"C:\PF\PowerShell\7\pwsh.exe",
            r"C:\WINDOWS\System32\WindowsPowerShell\v1.0\powershell.exe",
        ])

    def test_install_idempotent(self):
        executor = install_pwsh_executor(self.ctx, {"pwshPath": "pwsh"})
        self.assertIs(install_pwsh_executor(self.ctx, {"pwshPath": "pwsh"}), executor)

    def test_execute_spawn_failure_contained(self):
        # 不存在的 pwsh → spawn 失败被包含：done 正常结算、result() 抛错
        from miniharness.core.scope import Context as Ctx
        ctx2 = Ctx(name="pwsh-bad")
        try:
            executor = PwshLocalExecutor(ctx2, {"pwshPath": "definitely-not-a-pwsh"})
            execution = executor.execute(executor.resolve({"command": "$true"}))
            execution.done.result(timeout=10)
            with self.assertRaises(OSError):
                execution.result()
        finally:
            ctx2.dispose()


class PwshSandboxTest(unittest.TestCase):
    def setUp(self):
        self.ctx = Context(name="pwsh-sb")
        self.sandbox_calls = []

        class FakeSandbox:
            def confine(self, argv, policy):
                self.sandbox_calls.append((argv, policy))
                return {"argv": argv, "enforcement": "full",
                        "denialSignatures": ["denied"], "runnerFailureRules": []}

        self.sandbox = FakeSandbox()
        self.sandbox.sandbox_calls = self.sandbox_calls
        self.ctx.provide("sandbox", self.sandbox)

        class FakePolicy:
            default_mode = "read-only"

            def resolve(self, request=None):
                return {"mode": "read-only", "workspaceRoot": os.getcwd()}

        self.ctx.provide("sandboxPolicy", FakePolicy())

    def tearDown(self):
        self.ctx.dispose()

    def test_sandbox_confines_pwsh_argv(self):
        executor = SandboxPwshExecutor(self.ctx, {"pwshPath": "pwsh"})
        spec = executor.resolve({"command": "Get-ChildItem"})
        execution = executor.execute(spec)
        execution.done.result(timeout=10)
        # 尝试 spawn 真实 pwsh 会失败，但 confine 已被调用且 argv 是 pwsh 形状
        self.assertEqual(len(self.sandbox_calls), 1)
        argv, policy = self.sandbox_calls[0]
        self.assertEqual(argv[0], "pwsh")
        self.assertIn("-NoLogo", argv)
        self.assertIn("-Command", argv)
        self.assertEqual(policy["mode"], "read-only")


class ToolPwshTest(unittest.TestCase):
    def test_config_validation(self):
        self.assertEqual(resolve_config(None), {"enableRunInBackground": True,
                                                "promoteOnTimeout": True})
        with self.assertRaises(ValueError):
            resolve_config({"bogus": 1})

    def test_render_foreground_clean(self):
        result = settled_execution(exit_code=0, stdout="hi\n", timeout_ms=5000)
        rendered = render_pwsh_result({
            "exitCode": 0, "signal": None, "timedOut": False, "aborted": False,
            "timeoutMs": 5000,
            "stdout": {"text": "hi\n", "truncated": False},
            "stderr": {"text": "", "truncated": False},
        }, ())
        self.assertEqual(rendered, "hi\n")

    def test_render_foreground_stderr_and_exit(self):
        rendered = render_pwsh_result({
            "exitCode": 2, "signal": None, "timedOut": False, "aborted": False,
            "timeoutMs": 5000,
            "stdout": {"text": "out\n", "truncated": False},
            "stderr": {"text": "err\n", "truncated": False},
        }, ())
        self.assertEqual(rendered, "out\n[stderr]\nerr\n[exit code: 2]")

    def test_render_empty_body(self):
        rendered = render_pwsh_result({
            "exitCode": 0, "signal": None, "timedOut": False, "aborted": False,
            "timeoutMs": 5000,
            "stdout": {"text": "", "truncated": False},
            "stderr": {"text": "", "truncated": False},
        }, ())
        self.assertEqual(rendered, "(no output)")

    def test_render_denied(self):
        rendered = render_pwsh_result({
            "exitCode": 1, "signal": None, "timedOut": False, "aborted": False,
            "timeoutMs": 5000,
            "stdout": {"text": "", "truncated": False},
            "stderr": {"text": "access denied", "truncated": False},
            "sandbox": {"mode": "read-only", "denied": True},
        }, ())
        self.assertIn("[sandbox: file access denied under read-only mode]", rendered)

    def test_render_signal(self):
        rendered = render_pwsh_result({
            "exitCode": None, "signal": "SIGTERM", "timedOut": False, "aborted": False,
            "timeoutMs": 5000,
            "stdout": {"text": "", "truncated": False},
            "stderr": {"text": "", "truncated": False},
        }, ())
        self.assertIn("[killed by signal: SIGTERM]", rendered)

    def test_tool_schema(self):
        from miniharness.llm import FakeLlmAdapter
        from miniharness.shell import install_pwsh_executor
        ctx = Context(name="pwsh-tool")
        try:
            shell = install_pwsh_executor(ctx, {"pwshPath": "pwsh"})
            tool = create_pwsh_tool(ctx, shell)
            self.assertEqual(tool.name, "pwsh")
            self.assertIn("command", tool.parameters["properties"])
            self.assertIn("description", tool.parameters["properties"])
            self.assertEqual(tool.parameters["required"], ["command", "description"])
            self.assertEqual(tool.output["schema"]["oneOf"][0]["properties"]["kind"]["const"],
                             "background")
        finally:
            ctx.dispose()

    def test_tool_schema_no_background(self):
        ctx = Context(name="pwsh-tool-nb")
        try:
            from miniharness.shell import install_pwsh_executor
            shell = install_pwsh_executor(ctx, {"pwshPath": "pwsh"})
            tool = create_pwsh_tool(ctx, shell, {"enableRunInBackground": False})
            self.assertNotIn("run_in_background", tool.parameters["properties"])
        finally:
            ctx.dispose()


if __name__ == "__main__":
    unittest.main()