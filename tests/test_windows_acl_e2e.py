"""windows-acl 后端真 e2e（win32 宿主 + 环境变量门控）。

门控：MINIHARNESS_INTEGRATION_WINDOWS_ACL=1 时才跑（默认跳过）。这些测试在
真实 Windows 宿主上物化 DACL、创建受限令牌、spawn 真子进程——验证 ctypes FFI
移植与真实内核的契约；CI/无 Windows 宿主一律 skip，fail-closed 行为由单元
测试覆盖。

运行：MINIHARNESS_INTEGRATION_WINDOWS_ACL=1 python -m unittest discover -s tests -t .
"""

import os
import subprocess
import sys
import tempfile
import unittest
import uuid

GATE_ENV = "MINIHARNESS_INTEGRATION_WINDOWS_ACL"

if os.environ.get(GATE_ENV) != "1":
    raise unittest.SkipTest(
        f"windows-acl e2e requires {GATE_ENV}=1 on a real win32 host")

from miniharness.seams.sandbox_local import (  # noqa: E402
    LocalSandboxProvider,
    probe_windows_acl,
)
from miniharness.seams.sandbox_windows_acl import (  # noqa: E402
    AclSandbox,
    AclSandboxOptions,
    AclSandboxSpawnOptions,
    temp_write_sid,
    workspace_write_sid,
)


def mkdir_inherit(parent: str | None, prefix: str) -> str:
    """上游 node 夹具（fs.mkdtempSync）语义：继承父目录 DACL。

    tempfile.mkdtemp 的 0700 显式 SD（SYSTEM/Admins/OWNER RIGHTS，无 user ACE）
    会让受限子进程的两遍求值 pass-1 永远失败——OWNER RIGHTS 对 WRITE_RESTRICTED
    受限主体无效——无论能力 ACE 如何授予都拒写。
    """
    path = os.path.join(parent or tempfile.gettempdir(),
                        f"{prefix}{uuid.uuid4().hex[:12]}")
    os.mkdir(path)  # 默认 0777 → 不构造 SD，纯继承
    return os.path.realpath(path)


def decode_console(data: bytes) -> str:
    """cmd 子进程输出解码：优先 UTF-8（runner 诊断行），失败落 ANSI/OEM
    代码页（zh-CN 为 cp936）。UTF-8 模式下 getpreferredencoding 会失真，
    故显式列举。"""
    for enc in ("utf-8", "cp936"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("ascii", errors="replace")


def cmd_args(script: str) -> list[str]:
    return ["cmd", "/d", "/c", script]


class TestWindowsAclE2E(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if sys.platform != "win32":
            raise unittest.SkipTest("windows-acl e2e requires win32")
        cls.workspace = mkdir_inherit(None, "dsh-e2e-ws-")

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.workspace, ignore_errors=True)

    def _provider(self):
        return LocalSandboxProvider(internals={"platform": "win32"})

    # ---------- 探测 ----------

    def test_probe_with_default_runner_invocation(self):
        self.assertTrue(probe_windows_acl(self._provider()._windows_acl_runner_invocation()))

    # ---------- runner：read-only 拒写 ----------

    def test_read_only_blocks_workspace_write(self):
        target = os.path.join(self.workspace, "blocked.txt")
        argv = self._provider().confine(
            cmd_args(f"echo x > {target}"),
            {"mode": "read-only", "workspaceRoot": self.workspace})["argv"]
        proc = subprocess.run(argv, capture_output=True, timeout=30)
        self.assertNotEqual(proc.returncode, 0)
        self.assertFalse(os.path.exists(target))

    # ---------- seam 管理流：workspace-write 允许工作区、拒绝外部 ----------

    def test_seam_managed_write_inside_allowed_outside_denied(self):
        provider = self._provider()
        inside = os.path.join(self.workspace, "allowed.txt")
        outside = os.path.join(mkdir_inherit(None, "dsh-e2e-out-"), "denied.txt")
        try:
            confine = provider.confine(
                # 注意不能以 `exit /b 0` 收尾：那会无条件清零 errorlevel，
                # 掩盖外部写被拒的事实（rc 断言依赖最后一个命令的 errorlevel）。
                cmd_args(f"(echo ok > {inside}) & (echo no > {outside})"),
                {"mode": "workspace-write", "workspaceRoot": self.workspace,
                 "sessionId": "sess-e2e-1"})
            argv = confine["argv"]
            self.assertEqual(confine["enforcement"], "partial")
            proc = subprocess.run(argv, capture_output=True, timeout=60)
            self.assertNotEqual(proc.returncode, 0)   # 外部写被拒 → 整条命令失败
            stderr = decode_console(proc.stderr).lower()
            self.assertTrue(any(sig in stderr for sig in confine["denialSignatures"]),
                            f"denial signature not found in: {stderr!r}")
            self.assertTrue(os.path.exists(inside))
            self.assertFalse(os.path.exists(outside))
        finally:
            provider.revoke_acl_grants()  # 撤销 ACE 后统一删除私有 temp 目录

    # ---------- runner standalone 流：自管私有 temp ----------

    def test_standalone_runner_owns_temp_and_cleans_up(self):
        temp_root = mkdir_inherit(None, "dsh-e2e-tmp-")
        try:
            invocation = self._provider()._windows_acl_runner_invocation()
            marker = os.path.join(self.workspace, "standalone.txt")
            proc = subprocess.run([
                *invocation,
                "--workspace", self.workspace, "--temp", temp_root,
                "--mode", "workspace-write", "--",
                *cmd_args(f"echo hi > %TMP%\\inner.txt & echo hi > {marker}"),
            ], capture_output=True, timeout=60)
            self.assertEqual(proc.returncode, 0, proc.stderr.decode(errors="replace"))
            self.assertTrue(os.path.exists(marker))
            # owned 私有目录已随退出删除（temp root 回到只剩自身）
            leftovers = [name for name in os.listdir(temp_root)]
            self.assertEqual(leftovers, [])
        finally:
            import shutil
            shutil.rmtree(temp_root, ignore_errors=True)

    # ---------- AclSandbox 管道形态：stdout 捕获 ----------

    def test_pipe_child_captures_stdout(self):
        sandbox = AclSandbox(AclSandboxOptions(
            writable_dirs=[self.workspace], mode="read-only", temp_dir=None))
        try:
            sandbox.init()
            import asyncio

            async def scenario():
                child = sandbox.spawn(AclSandboxSpawnOptions(
                    command="cmd", args=["/d", "/c", "echo acl-e2e-ok"]))
                result = await child.wait()
                return result

            result = asyncio.run(scenario())
            self.assertEqual(result.exit_code, 0)
            self.assertIn(b"acl-e2e-ok", result.stdout.replace(b"\r\n", b"\n"))
        finally:
            sandbox.dispose()


if __name__ == "__main__":
    unittest.main()
