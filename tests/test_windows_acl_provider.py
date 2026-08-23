"""windows-acl runner argv 契约 + provider ACL 授权物化单元验收。

runner：parse_args 矩阵与失败签名行（exit 127 契约）。
provider：sessionId+workspace-write 的授权物化/复用/撤销（假 AclWriteGrant，
patch sandbox_local.AclWriteGrant —— 与上游测试替换 seam 内部对象同策略）。

运行：python -m unittest discover -s tests -t .
"""

import os
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest import mock

from miniharness.seams import sandbox_local
from miniharness.seams.sandbox_local import LocalSandboxProvider
from miniharness.seams.sandbox_windows_acl import (
    Win32Error,
    temp_write_sid,
    workspace_write_sid,
)
from miniharness.seams.sandbox_windows_acl import runner as runner_mod
from miniharness.seams.sandbox_windows_acl.runner import (
    RUNNER_FAILURE_EXIT,
    RUNNER_SIGNATURE,
    RunnerFailure,
)


# ==================== runner argv 契约 ====================

class TestRunnerParseArgs(unittest.TestCase):
    def parse(self, argv):
        return runner_mod.parse_args(argv)

    def test_basic_read_only(self):
        parsed = self.parse(["--workspace", "C:\\ws", "--temp", "C:\\Temp",
                             "--mode", "read-only", "--", "cmd", "/c", "exit"])
        self.assertEqual(parsed.workspace, "C:\\ws")
        self.assertEqual(parsed.mode, "read-only")
        self.assertIsNone(parsed.write_sid)
        self.assertIsNone(parsed.temp_write_sid)
        self.assertEqual(parsed.command, "cmd")
        self.assertEqual(parsed.args, ["/c", "exit"])

    def test_managed_pair(self):
        parsed = self.parse(["--workspace", "C:\\ws", "--temp", "C:\\Temp\\t",
                             "--mode", "workspace-write",
                             "--write-sid", "S-1-4-1-1", "--temp-write-sid", "S-1-4-2-2-1",
                             "--", "cmd"])
        self.assertEqual(parsed.write_sid, "S-1-4-1-1")
        self.assertEqual(parsed.temp_write_sid, "S-1-4-2-2-1")

    def test_failures_emit_signature_line(self):
        cases = [
            ["--mode", "read-only", "--"],                                    # 缺 --workspace
            ["--workspace", "C:\\ws", "--mode", "read-only", "--", "cmd"],    # 缺 --temp
            ["--workspace", "C:\\ws", "--temp", "C:\\T", "--mode", "bogus",
             "--", "cmd"],                                                    # 未知 mode
            ["--workspace", "C:\\ws", "--temp", "C:\\T", "--mode", "read-only"],  # 缺命令
            ["--workspace", "C:\\ws", "--temp", "C:\\T", "--mode", "read-only",
             "--"],                                                           # 空 argv
            ["--bogus", "x", "--workspace", "C:\\ws", "--temp", "C:\\T",
             "--mode", "read-only", "--", "cmd"],                             # 未知旗标
            ["--workspace", "--temp", "C:\\T", "--mode", "read-only",
             "--", "cmd"],                                                    # 缺值
        ]
        for argv in cases:
            stderr = StringIO()
            with redirect_stderr(stderr), self.assertRaises(RunnerFailure):
                self.parse(argv)
            self.assertTrue(stderr.getvalue().startswith(f"{RUNNER_SIGNATURE}: "), argv)

    def test_main_bad_args_exit_127(self):
        self.assertEqual(runner_mod.main(["--mode", "nope"]), RUNNER_FAILURE_EXIT)

    def test_runner_failure_constants(self):
        self.assertEqual(RUNNER_SIGNATURE, "windows-acl-run")
        self.assertEqual(RUNNER_FAILURE_EXIT, 127)
        self.assertEqual(sandbox_local.WINDOWS_ACL_RUNNER_MODULE,
                         "miniharness.seams.sandbox_windows_acl.runner")


# ==================== provider ACL 授权物化 ====================

class FakeGrant:
    """AclWriteGrant 同形替身：记录 add 序列与 dispose。"""

    def __init__(self, api, sid_addr, write_sid):
        self.write_sid = write_sid
        self.added = []
        self.disposed = False

    @classmethod
    def create(cls, write_sid, api=None):
        return cls(None, 1, write_sid)

    def add(self, path, standing=False):
        if getattr(self, "explode_on_temp", False) and standing is False and self.added:
            raise Win32Error("SetNamedSecurityInfoW", 5, path)
        self.added.append((path, standing))

    def dispose(self):
        self.disposed = True


class TestProviderAclGrants(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workspace = os.path.realpath(tempfile.mkdtemp(prefix="dsh-acl-provider-ws-"))

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.workspace, ignore_errors=True)

    def policy(self, session_id="sess-1"):
        return {"mode": "workspace-write", "workspaceRoot": self.workspace,
                "sessionId": session_id}

    def confine(self, provider):
        return provider.confine(["cmd", "/c", "echo"], self.policy())

    def test_materializes_grant_and_argv_pair(self):
        fake_temp = os.path.join(os.path.dirname(self.workspace), "dsh-fake-temp")
        with mock.patch.object(sandbox_local, "_create_session_temp_dir",
                               return_value=fake_temp), \
                mock.patch.object(sandbox_local, "AclWriteGrant", FakeGrant), \
                mock.patch.object(sandbox_local, "assert_temp_root_outside_workspace"):
            provider = LocalSandboxProvider(internals={"platform": "win32"})
            out = self.confine(provider)
        grant = provider._acl_grants["sess-1"]["grant"]
        self.assertTrue(grant.disposed is False)
        # workspace ACE 常驻、temp ACE 可撤销
        self.assertEqual(grant.added[0], (self.workspace, True))
        self.assertEqual(grant.added[1], (fake_temp, False))
        argv = out["argv"]
        self.assertEqual(argv[argv.index("--write-sid") + 1],
                         workspace_write_sid(self.workspace))
        self.assertEqual(argv[argv.index("--temp") + 1], fake_temp)
        self.assertEqual(argv[argv.index("--temp-write-sid") + 1],
                         temp_write_sid(fake_temp))
        self.assertEqual(out["enforcement"], "partial")

    def test_session_cache_reuses_grant(self):
        with mock.patch.object(sandbox_local, "AclWriteGrant", FakeGrant), \
                mock.patch.object(sandbox_local, "assert_temp_root_outside_workspace"):
            provider = LocalSandboxProvider(internals={"platform": "win32"})
            self.confine(provider)
            self.confine(provider)
        self.assertEqual(len(provider._acl_grants), 1)

    def test_read_only_never_touches_grants(self):
        with mock.patch.object(sandbox_local, "AclWriteGrant", FakeGrant):
            provider = LocalSandboxProvider(internals={"platform": "win32"})
            out = provider.confine(["cmd"], {"mode": "read-only",
                                             "workspaceRoot": self.workspace})
            self.assertNotIn("--write-sid", out["argv"])
            self.assertEqual(provider._acl_grants, {})

    def test_agentless_workspace_write_stays_basic(self):
        with mock.patch.object(sandbox_local, "AclWriteGrant", FakeGrant):
            provider = LocalSandboxProvider(internals={"platform": "win32"})
            out = provider.confine(["cmd"], {"mode": "workspace-write",
                                             "workspaceRoot": self.workspace})
            self.assertNotIn("--write-sid", out["argv"])
            self.assertEqual(provider._acl_grants, {})

    def test_materialize_failure_disposes_and_does_not_cache(self):
        created = []

        class Exploding(FakeGrant):
            @classmethod
            def create(cls, write_sid, api=None):
                grant = cls.__mro__[1].create(write_sid, api=api)
                grant.explode_on_temp = True
                created.append(grant)
                return grant

        with mock.patch.object(sandbox_local, "_create_session_temp_dir",
                               return_value="X"), \
                mock.patch.object(sandbox_local, "AclWriteGrant", Exploding), \
                mock.patch.object(sandbox_local, "assert_temp_root_outside_workspace"):
            provider = LocalSandboxProvider(internals={"platform": "win32"})
            with self.assertRaises(Win32Error):
                self.confine(provider)
        # fail-closed：temp 授权物化失败 → dispose 已授予的 workspace 路径、不缓存
        self.assertTrue(created[0].disposed)
        self.assertEqual(provider._acl_grants, {})

    def test_remove_temp_dir_hook_and_unknown_session(self):
        removed = []
        with mock.patch.object(sandbox_local, "AclWriteGrant", FakeGrant), \
                mock.patch.object(sandbox_local, "assert_temp_root_outside_workspace"):
            provider = LocalSandboxProvider(internals={
                "platform": "win32",
                "rmTempDir": lambda path: removed.append(path)})
            record = provider._materialize_acl_grant("sess-1", self.workspace)
            provider.remove_temp_dir("sess-1")
            provider.remove_temp_dir("missing")  # 静默返回
        self.assertEqual(removed, [record["temp_dir"]])

    def test_revoke_acl_grants_disposes_everything(self):
        with mock.patch.object(sandbox_local, "AclWriteGrant", FakeGrant), \
                mock.patch.object(sandbox_local, "assert_temp_root_outside_workspace"):
            provider = LocalSandboxProvider(internals={"platform": "win32"})
            provider._materialize_acl_grant("s1", self.workspace)
            provider._materialize_acl_grant("s2", self.workspace)
            grants = [r["grant"] for r in provider._acl_grants.values()]
            provider.revoke_acl_grants()
        self.assertTrue(all(g.disposed for g in grants))
        self.assertEqual(provider._acl_grants, {})


if __name__ == "__main__":
    unittest.main()
