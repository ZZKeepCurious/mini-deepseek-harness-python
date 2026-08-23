"""landlock-run ctypes 执行器与 sandbox_local 接线的测试。

对应上游锚点：native/landlock-run/docs/cli-contract.md（语法/退出码/报告行）、
sandbox-local/src/index.ts（探测仲裁与 grant argv）。真内核 e2e 由
MINIHARNESS_INTEGRATION_LANDLOCK=1 门控（Linux CI）；其余用例跨平台可跑——
非 Linux 宿主上模块必须干净退出 125（fail-closed，绝不 traceback）。
"""

import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from miniharness.seams import sandbox_local
from miniharness.seams.landlock_run import (
    LAUNCHER_FAILURE_EXIT,
    PARTIAL_RUN_LINE,
    PROBE_FULL_LINE,
    PROBE_PARTIAL_LINE,
    TOOL_MAX_ABI,
    file_compatible_bits,
    main,
    negotiated_fs_bits,
    parse_grant_args,
)
from miniharness.seams.sandbox_local import (
    RUNNER_FAILURE_RULES,
    LANDLOCK_LAUNCHER_MODULE,
    LocalSandboxProvider,
    landlock_launcher_prefix,
    probe_landlock,
)

_INTEGRATION = os.environ.get("MINIHARNESS_INTEGRATION_LANDLOCK") == "1"


def _completed(returncode, stdout=b"", stderr=b""):
    proc = subprocess.CompletedProcess([], returncode)
    proc.stdout = stdout
    proc.stderr = stderr
    return proc


class NegotiatedBitsTest(unittest.TestCase):
    def test_abi_monotonic_and_base(self):
        abi1 = negotiated_fs_bits(1)
        for abi in (2, 3, 4):
            self.assertTrue(negotiated_fs_bits(abi) & abi1 == abi1)
            self.assertGreater(negotiated_fs_bits(abi), negotiated_fs_bits(abi - 1))
        # ABI 5 无新 fs 位：与 ABI 4 位集一致（scoping 不属于 handled_access_fs）
        self.assertEqual(negotiated_fs_bits(TOOL_MAX_ABI),
                         negotiated_fs_bits(TOOL_MAX_ABI - 1))

    def test_file_compat_drops_dir_only_ops(self):
        full = negotiated_fs_bits(TOOL_MAX_ABI)
        rw_file = file_compatible_bits(full, read_only=False)
        self.assertNotEqual(rw_file & full, 0)
        # 写文件、截断必须保留；建目录/读目录等仅目录操作必须剔除
        self.assertEqual(rw_file | full, full)   # 子集
        self.assertLess(rw_file, full)
        ro_file = file_compatible_bits(full, read_only=True)
        self.assertLess(ro_file, rw_file)        # 只读更窄


class ParseGrantArgsTest(unittest.TestCase):
    def test_grants_then_separator(self):
        ro, rw, command, probe = parse_grant_args(
            ["--ro", "/", "--rw", "/tmp", "--rw", "/dev/null", "--", "bash", "-c", "x"])
        self.assertEqual(ro, ["/"])
        self.assertEqual(rw, ["/tmp", "/dev/null"])
        self.assertEqual(command, ["bash", "-c", "x"])
        self.assertFalse(probe)

    def test_probe_exclusive(self):
        ro, rw, command, probe = parse_grant_args(["--probe"])
        self.assertTrue(probe)

    def _fatal(self, args):
        with self.assertRaises(SystemExit) as ctx:
            parse_grant_args(args)
        self.assertEqual(ctx.exception.code, LAUNCHER_FAILURE_EXIT)

    def test_missing_separator_fails_closed(self):
        self._fatal(["--ro", "/", "bash"])

    def test_unknown_flag_fails_closed(self):
        self._fatal(["--evil", "--", "true"])

    def test_flag_without_value_fails_closed(self):
        self._fatal(["--ro", "--", "true"])

    def test_probe_with_grants_fails_closed(self):
        self._fatal(["--probe", "--ro", "/", "--", "true"])

    def test_empty_command_fails_closed(self):
        self._fatal(["--"])


class ProbeLandlockMappingTest(unittest.TestCase):
    def _probe_with(self, proc=None, side_effect=None):
        kwargs = {}
        if proc is not None:
            kwargs["return_value"] = proc
        if side_effect is not None:
            kwargs["side_effect"] = side_effect
        with mock.patch.object(sandbox_local.subprocess, "run", **kwargs):
            return probe_landlock(["py", "-m", "landlock_run"], 1000)

    def test_full_line(self):
        self.assertEqual(self._probe_with(_completed(0, PROBE_FULL_LINE.encode())), "full")

    def test_partial_line(self):
        self.assertEqual(
            self._probe_with(_completed(0, PROBE_PARTIAL_LINE.encode() + b"\n")), "partial")

    def test_nonzero_exit_is_unusable(self):
        self.assertEqual(self._probe_with(_completed(125)), "unusable")

    def test_garbage_stdout_is_unusable(self):
        self.assertEqual(self._probe_with(_completed(0, b"hello")), "unusable")

    def test_timeout_is_unusable(self):
        self.assertEqual(
            self._probe_with(side_effect=subprocess.TimeoutExpired([], 1)), "unusable")


class ProviderWiringTest(unittest.TestCase):
    """landlock 梯队从「无 launcher 恒不可用」改为默认 ctypes 前缀真探测。"""

    def _provider(self):
        return LocalSandboxProvider(internals={
            "chain": ["bwrap", "landlock"],
            "probeBwrap": lambda: False,
        })

    def test_default_prefix_is_module_invocation(self):
        prefix = landlock_launcher_prefix()
        self.assertEqual(prefix,
                         [sys.executable, "-m", LANDLOCK_LAUNCHER_MODULE])

    def test_string_override_stays_back_compatible(self):
        prefix = landlock_launcher_prefix({"landlockLauncher": "/fake/landlock-run"})
        self.assertEqual(prefix, ["/fake/landlock-run"])

    def test_list_override_passthrough(self):
        prefix = landlock_launcher_prefix({"landlockLauncher": ["/usr/bin/env", "ll"]})
        self.assertEqual(prefix, ["/usr/bin/env", "ll"])

    def test_chain_falls_through_to_default_probed_landlock(self):
        provider = self._provider()
        with mock.patch.object(sandbox_local, "probe_landlock",
                               return_value="partial") as patched:
            out = provider.confine(["bash", "-c", "echo hi"],
                                   {"mode": "workspace-write", "workspaceRoot": "/ws"})
        self.assertEqual(patched.call_args.args[0],
                         landlock_launcher_prefix(provider.internals))
        self.assertEqual(out["enforcement"], "partial")
        argv = out["argv"]
        head = argv[:argv.index("--")]
        self.assertEqual(head[:3], [sys.executable, "-m", LANDLOCK_LAUNCHER_MODULE])
        self.assertEqual(head[3:], ["--ro", "/", "--rw", "/dev/null",
                                    "--rw", "/tmp", "--rw", "/ws"])
        self.assertEqual(out["runnerFailureRules"], RUNNER_FAILURE_RULES["landlock"])
        self.assertEqual(argv[-4:], ["--", "bash", "-c", "echo hi"])

    def test_injected_probe_hook_still_wins(self):
        provider = LocalSandboxProvider(internals={
            "chain": ["bwrap", "landlock"],
            "probeBwrap": lambda: False,
            "probeLandlock": lambda launcher: "full",
            "landlockLauncher": "/fake/landlock-run",
        })
        out = provider.confine(["true"], {"mode": "read-only", "workspaceRoot": "/"})
        self.assertEqual(out["enforcement"], "full")
        self.assertEqual(out["argv"][0], "/fake/landlock-run")
        self.assertEqual(out["argv"][1:5], ["--ro", "/", "--rw", "/dev/null"])


class CleanFailSmokeTest(unittest.TestCase):
    """真实子进程冒烟：任何宿主上模块要么成功报告、要么干净 125，绝不 traceback。"""

    def test_probe_never_crashes(self):
        proc = subprocess.run(
            [sys.executable, "-m", LANDLOCK_LAUNCHER_MODULE, "--probe"],
            timeout=30, capture_output=True)
        self.assertNotIn(b"Traceback", proc.stderr)
        if proc.returncode == 0:
            line = proc.stdout.decode().strip()
            self.assertIn(line, {PROBE_FULL_LINE, PROBE_PARTIAL_LINE})
        else:
            self.assertEqual(proc.returncode, LAUNCHER_FAILURE_EXIT)
            self.assertTrue(proc.stderr.decode(errors="replace")
                            .startswith("landlock-run: "))


@unittest.skipUnless(sys.platform.startswith("linux") and _INTEGRATION,
                     "requires Linux kernel with Landlock + MINIHARNESS_INTEGRATION_LANDLOCK=1")
class LandlockKernelE2ETest(unittest.TestCase):
    """真内核 e2e：read-only 拒写、workspace-write 工作区可写、未授权路径拒。"""

    def setUp(self):
        self.provider = LocalSandboxProvider()
        policy = {"mode": "workspace-write", "workspaceRoot": tempfile.mkdtemp()}
        confined = self.provider.confine([sys.executable], policy)
        verdict = probe_landlock(landlock_launcher_prefix())
        if verdict == "unusable":
            self.skipTest("kernel cannot enforce Landlock")
        self.policy = policy
        self.confine_argv = confined["argv"]

    def _run_py(self, code: str, mode: str = "workspace-write"):
        self.policy["mode"] = mode
        argv = self.provider.confine([sys.executable, "-c", code],
                                     self.policy)["argv"]
        return subprocess.run(argv, timeout=60, capture_output=True)

    def test_read_only_blocks_write(self):
        proc = self._run_py("open('mini-ll-probe','w')", mode="read-only")
        self.assertNotEqual(proc.returncode, 0)

    def test_workspace_write_allows_root_denies_outside(self):
        root = self.policy["workspaceRoot"]
        ok = self._run_py(f"open({root!r}+'/ok','w').write('x')")
        self.assertEqual(ok.returncode, 0, ok.stderr)
        bad = self._run_py(f"open({root!r}+'/../escape','w')")
        self.assertNotEqual(bad.returncode, 0)


class RestrictAndExecTest(unittest.TestCase):
    """mock ctypes 封装与 grant 打开层，钉死规则构建与 fail-closed 控制流。

    全程不触真实内核也不依赖宿主文件系统（grant fd 注入），保证 Windows 开发机
    与 Linux CI 行为一致；真内核路径由 LandlockKernelE2ETest 单独门控。
    """

    def setUp(self):
        from miniharness.seams import landlock_run as lr

        self.lr = lr

    def _run(self, version, ro, rw, command, isdir=None):
        """注入假 _Landlock / 假 grant fd / 假目录判定后执行 restrict_and_exec。

        version=None 模拟内核不支持 Landlock（kernel_abi 抛 LauncherUnavailable）；
        isdir 按 grant fd 映射目录判定（缺省 101=目录、102=文件）。
        返回 (fake 实例, exec 记录)。
        """
        lr = self.lr
        executed = {}
        grant_fds = iter((101, 102, 103))
        isdir = {101: True, 102: False} if isdir is None else isdir

        class FakeLL:
            def __init__(self):
                self.handled = None
                self.rules = []
                self.restricted = None

            def kernel_abi(self):
                if version is None:
                    raise lr.LauncherUnavailable(
                        "kernel does not support Landlock (errno=38)")
                return version

            def create_ruleset(self, handled_access_fs):
                self.handled = handled_access_fs
                return -1 if handled_access_fs == -1 else 7

            def add_rule(self, ruleset_fd, allowed_access, parent_fd):
                self.rules.append((allowed_access, parent_fd))

            def restrict_self(self, ruleset_fd):
                self.restricted = ruleset_fd

        fake = FakeLL()
        with mock.patch.object(lr, "_Landlock", lambda: fake), \
             mock.patch.object(lr, "_open_grant_root",
                               side_effect=lambda path: next(grant_fds)), \
             mock.patch.object(lr, "_is_dir", side_effect=lambda fd: isdir[fd]), \
             mock.patch.object(lr.os, "close"), \
             mock.patch.object(lr.os, "execvp",
                               side_effect=lambda f, a: executed.update(file=f, argv=a)):
            lr.restrict_and_exec(ro, rw, command)
        return fake, executed

    def test_full_abi_installs_rules_then_execs(self):
        from miniharness.seams.landlock_run import _RO_GRANT_BITS

        full = negotiated_fs_bits(TOOL_MAX_ABI)
        fake, executed = self._run(TOOL_MAX_ABI, ["workspace"], ["data.bin"],
                                   ["bash", "-c", "x"])
        # 协商位集按 ABI 屏蔽（ABI 5 无新 fs 位 → 等于 ABI 4 全集）
        self.assertEqual(fake.handled, negotiated_fs_bits(TOOL_MAX_ABI - 1))
        # ro 目录 grant → RO 位全集；rw 文件 grant → 文件兼容位
        self.assertIn((_RO_GRANT_BITS & full, 101), fake.rules)
        self.assertIn((file_compatible_bits(full, read_only=False) & full, 102),
                      fake.rules)
        self.assertEqual(fake.restricted, 7)
        self.assertEqual(executed, {"file": "bash", "argv": ["bash", "-c", "x"]})

    def test_partial_abi_warns_but_proceeds(self):
        import contextlib
        import io

        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            fake, executed = self._run(2, [], [], ["true"])
        self.assertIn(PARTIAL_RUN_LINE, err.getvalue())
        self.assertIsNotNone(fake.restricted)
        self.assertEqual(executed["file"], "true")

    def test_kernel_without_landlock_fails_closed(self):
        with self.assertRaises(SystemExit) as ctx:
            self._run(None, [], [], ["true"])
        self.assertEqual(ctx.exception.code, LAUNCHER_FAILURE_EXIT)

    def test_create_ruleset_failure_exits_125(self):
        lr = self.lr
        with mock.patch.object(lr, "_Landlock") as factory:
            factory.return_value.kernel_abi.return_value = TOOL_MAX_ABI
            factory.return_value.create_ruleset.return_value = -1
            with self.assertRaises(SystemExit) as ctx:
                lr.restrict_and_exec([], [], ["true"])
        self.assertEqual(ctx.exception.code, LAUNCHER_FAILURE_EXIT)

    def test_unopenable_grant_root_exits_125(self):
        lr = self.lr
        with mock.patch.object(lr, "_Landlock") as factory, \
             mock.patch.object(lr, "_open_grant_root",
                               side_effect=lr.LauncherUnavailable(
                                   "cannot open grant root /missing: ENOENT")):
            factory.return_value.kernel_abi.return_value = TOOL_MAX_ABI
            factory.return_value.create_ruleset.return_value = 7
            with self.assertRaises(SystemExit) as ctx:
                lr.restrict_and_exec(["/missing"], [], ["true"])
        self.assertEqual(ctx.exception.code, LAUNCHER_FAILURE_EXIT)

    def test_exec_failure_exits_125_without_fallback(self):
        lr = self.lr
        with mock.patch.object(lr, "_Landlock") as factory, \
             mock.patch.object(lr.os, "close"), \
             mock.patch.object(lr.os, "execvp",
                               side_effect=OSError(2, "No such file")):
            factory.return_value.kernel_abi.return_value = TOOL_MAX_ABI
            factory.return_value.create_ruleset.return_value = 7
            with self.assertRaises(SystemExit) as ctx:
                lr.restrict_and_exec([], [], ["no-such-binary-xyz"])
        self.assertEqual(ctx.exception.code, LAUNCHER_FAILURE_EXIT)

    def test_main_probe_dispatch(self):
        import contextlib
        import io

        out = io.StringIO()
        with mock.patch.object(self.lr, "_Landlock") as factory, \
             contextlib.redirect_stdout(out):
            factory.return_value.kernel_abi.return_value = TOOL_MAX_ABI
            self.lr.main(["--probe"])
        self.assertEqual(out.getvalue().strip(), PROBE_FULL_LINE)


if __name__ == "__main__":
    unittest.main()
