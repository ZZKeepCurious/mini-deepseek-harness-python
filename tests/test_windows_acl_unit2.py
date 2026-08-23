"""windows-acl 后端单元验收（二）：spawn 六关闭契约 / 排水 / 退出等待 /
kill-on-close job / inherit 形态失败编排 / AclSandbox 构造校验矩阵 /
runner argv 契约。假 api 对象替换真实绑定（上游测试同策略）。

运行：python -m unittest discover -s tests -t .
"""

import asyncio
import os
import struct
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO

from miniharness.seams.sandbox_windows_acl import (
    UNSET,
    AclSandbox,
    AclSandboxOptions,
    AclSandboxSpawnOptions,
    Win32Error,
)
from miniharness.seams.sandbox_windows_acl import spawn as spawn_mod
from miniharness.seams.sandbox_windows_acl import win32_abi as abi


# ==================== spawn 契约 ====================

class SpawnApiBase:
    """管道句柄计数器 + 关闭记录（stdin(1,2) stdout(3,4) stderr(5,6)）。"""

    def __init__(self):
        self.counter = iter(range(1, 100))
        self.closed = []
        self.last_error = 231

    def getLastError(self):
        return self.last_error

    def createPipe(self, read_slot, write_slot, sec, size):
        read_slot.value = next(self.counter)
        write_slot.value = next(self.counter)
        return 1

    def setHandleInformation(self, *args):
        return 1

    def closeHandle(self, handle):
        self.closed.append(handle)
        return 1


class TestSpawnSandboxed(unittest.TestCase):
    def test_failure_closes_exactly_six_pipe_handles(self):
        class Api(SpawnApiBase):
            def createProcessAsUserW(self, *args):
                return 0

        api = Api()
        with self.assertRaises(Win32Error) as caught:
            spawn_mod.spawn_sandboxed(api, 55, {"command": "cmd", "args": [], "cwd": "C:\\"})
        self.assertEqual((caught.exception.api, caught.exception.win32_code),
                         ("CreateProcessAsUserW", 231))
        # six-close 契约：CreateProcessAsUserW 失败 → 六根管道句柄全部先关
        self.assertEqual(sorted(api.closed), [1, 2, 3, 4, 5, 6])

    def test_success_host_side_close_order(self):
        class Api(SpawnApiBase):
            def __init__(self):
                super().__init__()
                self.startup_fields = None

            def createProcessAsUserW(self, token, application, command_line, pa, ta,
                                     inherit_handles, flags, environment, cwd,
                                     startup_info, process_info):
                self.startup_fields = (command_line, inherit_handles, flags,
                                       startup_info.dwFlags, startup_info.hStdInput,
                                       startup_info.hStdOutput, startup_info.hStdError)
                process_info.hProcess = 100
                process_info.hThread = 200
                process_info.dwProcessId = 4242
                return 1

        api = Api()
        native = spawn_mod.spawn_sandboxed(api, 55, {
            "command": "cmd", "args": ["/c", "echo"], "cwd": "C:\\"})
        self.assertEqual((native.pid, native.process), (4242, 100))
        self.assertEqual((native.stdout_read, native.stderr_read), (3, 5))
        # 宿主侧关序：子进程读端、两个写端、stdin 写端、线程句柄
        self.assertEqual(api.closed, [1, 4, 6, 2, 200])
        command_line, inherit_handles, flags, dwflags, hstdin, hstdout, hstderr = \
            api.startup_fields
        self.assertEqual(command_line, "cmd /c echo")
        self.assertEqual(inherit_handles, 1)
        self.assertEqual(flags, 0)
        self.assertEqual(dwflags, abi.STARTF_USESTDHANDLES)
        self.assertEqual((hstdin, hstdout, hstderr), (1, 4, 6))


class TestDrainPipe(unittest.TestCase):
    def test_broken_pipe_is_clean_eof(self):
        closed = []

        class Api:
            last_error = abi.ERROR_BROKEN_PIPE

            def getLastError(self):
                return self.last_error

            def peekNamedPipe(self, *args):
                return 0

            def closeHandle(self, handle):
                closed.append(handle)
                return 1

        self.assertEqual(asyncio.run(spawn_mod.drain_pipe(Api(), 77)), b"")
        self.assertEqual(closed, [77])

    def test_reads_available_chunks_then_eof(self):
        closed = []

        class Api:
            def __init__(self):
                self.peeks = 0

            def getLastError(self):
                return abi.ERROR_BROKEN_PIPE

            def peekNamedPipe(self, handle, buf, size, read_slot, total_slot, left_slot):
                self.peeks += 1
                if self.peeks == 1:
                    total_slot.value = 3
                    return 1
                return 0  # broken pipe → EOF

            def readFile(self, handle, chunk, length, read_slot, overlapped):
                chunk[:3] = b"abc"
                read_slot.value = 3
                return 1

            def closeHandle(self, handle):
                closed.append(handle)
                return 1

        api = Api()
        self.assertEqual(asyncio.run(spawn_mod.drain_pipe(api, 77)), b"abc")
        self.assertEqual(closed, [77])

    def test_other_peek_errors_raise(self):
        class Api:
            last_error = 6

            def getLastError(self):
                return self.last_error

            def peekNamedPipe(self, *args):
                return 0

        with self.assertRaises(Win32Error) as caught:
            asyncio.run(spawn_mod.drain_pipe(Api(), 77))
        self.assertEqual(caught.exception.api, "PeekNamedPipe")


class WaitExitApi:
    def __init__(self, wait_result, exit_code):
        self.wait_result = wait_result
        self.exit_code = exit_code
        self.closed = []
        self.last_error = 8

    def getLastError(self):
        return self.last_error

    def waitForSingleObject(self, handle, ms):
        return self.wait_result

    def getExitCodeProcess(self, handle, slot):
        slot.value = self.exit_code
        return 1

    def closeHandle(self, handle):
        self.closed.append(handle)
        return 1


class TestWaitForExit(unittest.TestCase):
    def test_mirrors_full_width_exit_code(self):
        api = WaitExitApi(0, 3221225477)  # 0xC0000005 全宽镜像，不截断不掩码
        self.assertEqual(spawn_mod.wait_for_exit(api, 88), 3221225477)
        self.assertEqual(api.closed, [88])

    def test_wait_failed_raises(self):
        # WAIT_FAILED 与 WAIT_TIMEOUT/INFINITE 同值 0xFFFFFFFF：数值判断
        api = WaitExitApi(0xFFFFFFFF, 0)
        with self.assertRaises(Win32Error) as caught:
            spawn_mod.wait_for_exit(api, 88)
        self.assertEqual(caught.exception.api, "WaitForSingleObject")


class JobApi:
    def __init__(self):
        self.job_info = None
        self.closed = []
        self.last_error = 87

    def getLastError(self):
        return self.last_error

    def createJobObjectW(self, attrs, name):
        return 500

    def setInformationJobObject(self, job, info_class, info, size):
        self.job_info = (job, info_class, bytes(info), size)
        return 1

    def closeHandle(self, handle):
        self.closed.append(handle)
        return 1


class TestKillOnCloseJob(unittest.TestCase):
    def test_extended_limit_information_layout(self):
        api = JobApi()
        job = spawn_mod.create_kill_on_close_job(api)
        self.assertEqual(job, 500)
        _, info_class, info, size = api.job_info
        self.assertEqual((info_class, size), (abi.JobObjectExtendedLimitInformation, 144))
        self.assertEqual(info[16:20],
                         int(abi.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE).to_bytes(4, "little"))

    def test_set_information_failure_closes_job(self):
        api = JobApi()
        api.setInformationJobObject = lambda *args: 0
        with self.assertRaises(Win32Error):
            spawn_mod.create_kill_on_close_job(api)
        self.assertEqual(api.closed, [500])


class InheritedApi(SpawnApiBase):
    """inherit 形态：job=800、std 句柄 900-902、可注入失败点。"""

    def __init__(self, fail_at=None):
        super().__init__()
        self.fail_at = fail_at
        self.flags = None
        self.terminated = []
        self.resumed = 0

    def getStdHandle(self, selector):
        return {abi.STD_INPUT_HANDLE: 900, abi.STD_OUTPUT_HANDLE: 901,
                abi.STD_ERROR_HANDLE: 902}[selector]

    def createJobObjectW(self, attrs, name):
        return 800

    def setInformationJobObject(self, job, info_class, info, size):
        return 1

    def createProcessAsUserW(self, token, application, command_line, pa, ta,
                             inherit, flags, env, cwd, startup_info, process_info):
        self.flags = flags
        process_info.hProcess = 100
        process_info.hThread = 200
        process_info.dwProcessId = 4242
        return 1

    def assignProcessToJobObject(self, job, process):
        return 0 if self.fail_at == "assign" else 1

    def resumeThread(self, thread):
        self.resumed += 1
        return 0xFFFFFFFF if self.fail_at == "resume" else 0

    def terminateProcess(self, process, code):
        self.terminated.append((process, code))
        return 1


class TestSpawnInherited(unittest.TestCase):
    OPTIONS = {"command": "cmd", "args": [], "cwd": "C:\\"}

    def test_suspended_launch_and_thread_closed(self):
        api = InheritedApi()
        native = spawn_mod.spawn_sandboxed_inherited(api, 55, dict(self.OPTIONS))
        self.assertEqual(api.flags, abi.CREATE_SUSPENDED)
        self.assertEqual((native.pid, native.process, native.job), (4242, 100, 800))
        self.assertEqual(api.resumed, 1)
        self.assertEqual(api.closed, [200])

    def test_assign_failure_kills_suspended_child(self):
        # 挂起且不在 job 里 → 只关句柄会永远吊着：先终结再抛
        api = InheritedApi(fail_at="assign")
        with self.assertRaises(Win32Error) as caught:
            spawn_mod.spawn_sandboxed_inherited(api, 55, dict(self.OPTIONS))
        self.assertEqual(caught.exception.api, "AssignProcessToJobObject")
        self.assertEqual(api.terminated, [(100, 1)])
        self.assertEqual(sorted(api.closed), [100, 200, 800])

    def test_resume_failure_relies_on_kill_on_close(self):
        # 关 job 即触发 kill-on-close：挂起的子进程随之死亡而不是悬着
        api = InheritedApi(fail_at="resume")
        with self.assertRaises(Win32Error) as caught:
            spawn_mod.spawn_sandboxed_inherited(api, 55, dict(self.OPTIONS))
        self.assertEqual(caught.exception.api, "ResumeThread")
        self.assertEqual(api.terminated, [])
        self.assertEqual(sorted(api.closed), [100, 200, 800])


# ==================== AclSandbox 构造校验矩阵 ====================

class TestAclSandboxValidation(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dir = tempfile.mkdtemp(prefix="dsh-acl-val-")

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.dir, ignore_errors=True)

    def make(self, **overrides):
        options = {
            "writable_dirs": [self.dir], "mode": "read-only",
            "temp_dir": None, "write_sid": None, "temp_write_sid": None,
        }
        options.update(overrides)
        return AclSandbox(AclSandboxOptions(**options))

    def test_read_only_defaults_ok(self):
        sandbox = self.make()
        self.assertTrue(sandbox.manage_dacls)
        self.assertIsNone(sandbox.temp_dir)

    def test_workspace_write_requires_write_sid(self):
        with self.assertRaisesRegex(RuntimeError, "requires a write SID"):
            self.make(mode="workspace-write")

    def test_workspace_write_requires_explicit_temp_decision(self):
        with self.assertRaisesRegex(RuntimeError, "explicit private temp directory or null"):
            self.make(mode="workspace-write", write_sid="S-1-4-1-1", temp_dir=UNSET)

    def test_read_only_rejects_temp(self):
        with self.assertRaisesRegex(RuntimeError, "does not accept a temp directory"):
            self.make(temp_dir=self.dir)

    def test_read_only_rejects_write_sids(self):
        with self.assertRaisesRegex(RuntimeError, "does not accept write SIDs"):
            self.make(write_sid="S-1-4-1-1")

    def test_workspace_write_temp_requires_temp_sid(self):
        with self.assertRaisesRegex(RuntimeError, "temp write SID"):
            self.make(mode="workspace-write", write_sid="S-1-4-1-1",
                      temp_dir=self.dir, temp_write_sid=None)

    def test_null_temp_rejects_temp_sid(self):
        with self.assertRaisesRegex(RuntimeError, "requires a temp directory"):
            self.make(mode="workspace-write", write_sid="S-1-4-1-1",
                      temp_dir=None, temp_write_sid="S-1-4-2-2-1")

    def test_same_sids_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "must be distinct"):
            self.make(mode="workspace-write", write_sid="S-1-4-1-1",
                      temp_dir=self.dir, temp_write_sid="S-1-4-1-1")

    def test_missing_writable_dir_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "writable dir does not exist"):
            self.make(writable_dirs=[os.path.join(self.dir, "missing")])

    def test_uninitialized_spawn_rejected(self):
        sandbox = self.make()
        with self.assertRaisesRegex(RuntimeError, "not initialized"):
            sandbox.spawn(AclSandboxSpawnOptions(command="cmd"))


if __name__ == "__main__":
    unittest.main()
