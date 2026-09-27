"""subprocess 接缝服务（M17 功能对标迁移）验收。

对齐上游 `packages/subprocess/subprocess` 的 Service Definition 面：
resolveExecutable / terminalEnvironment / scrubbedParentEnv 收编为
`ctx.subprocess` 服务，复用既有实现（无复制）。
"""
import os
import unittest

from miniharness.core.scope import Context
from miniharness.subprocess import (
    DSH_ENV_PREFIX,
    SENSITIVE_ENV_PATTERN,
    SubprocessExecutableNotFoundError,
    install_subprocess,
    resolve_executable,
    scrubbed_parent_env,
    terminal_environment,
)


class SubprocessSeamTest(unittest.TestCase):
    def test_env_scrub(self):
        env = scrubbed_parent_env({
            "DEEPSEEK_API_KEY": "x", "HOME": "/h", "MY_TOKEN": "t",
            "PATH": "/p", "dsh_private": "secret"})
        self.assertEqual(env, {"HOME": "/h", "PATH": "/p"})

    def test_constants(self):
        self.assertTrue(SENSITIVE_ENV_PATTERN.search("API_KEY"))
        self.assertTrue(SENSITIVE_ENV_PATTERN.search("password"))
        self.assertFalse(SENSITIVE_ENV_PATTERN.search("HOMEDIR"))
        self.assertEqual(DSH_ENV_PREFIX, "DSH_")

    def test_terminal_environment(self):
        env = terminal_environment()
        self.assertIn("platform", env)
        self.assertIn(env["platform"], ("posix", "windows"))

    def test_resolve_executable(self):
        executable = resolve_executable("python" if os.name == "nt" else "sh")
        self.assertTrue(os.path.isfile(executable))

    def test_resolve_relative_rejected(self):
        with self.assertRaises(RuntimeError):
            resolve_executable("rel/path")

    def test_resolve_missing_rejected(self):
        with self.assertRaises(SubprocessExecutableNotFoundError):
            resolve_executable("definitely-not-a-real-command-xyz")

    def test_install_service(self):
        ctx = Context(name="subprocess-seam")
        try:
            svc = install_subprocess(ctx)
            self.assertIs(install_subprocess(ctx), svc)
            self.assertEqual(svc.terminal_environment()["platform"],
                             terminal_environment()["platform"])
            self.assertEqual(svc.scrubbed_parent_env({"X_KEY": "1", "KEEP": "2"}),
                             {"KEEP": "2"})
        finally:
            ctx.dispose()


if __name__ == "__main__":
    unittest.main()