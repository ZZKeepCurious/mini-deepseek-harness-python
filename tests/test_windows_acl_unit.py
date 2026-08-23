"""windows-acl 后端（Phase C）单元验收：常量布局 / SID 派生 / 路径边界 /
ACL 打包与步行 / 令牌构造 / spawn 契约 / AclSandbox 校验矩阵 / runner argv
契约。全部以同形假 api 对象替换真实 Win32 绑定（上游测试同策略）；真 e2e
在 test_windows_acl_e2e.py（环境变量门控）。

运行：python -m unittest discover -s tests -t .
"""

import asyncio
import ctypes
import os
import struct
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from unittest import mock

from miniharness.seams.sandbox_windows_acl import (
    UNSET,
    AclSandbox,
    AclSandboxOptions,
    AggregateError,
    Win32Error,
    assert_private_temp_disjoint,
    assert_temp_root_outside_workspace,
    temp_write_sid,
    workspace_write_sid,
)
from miniharness.seams.sandbox_windows_acl import acl as acl_mod
from miniharness.seams.sandbox_windows_acl import spawn as spawn_mod
from miniharness.seams.sandbox_windows_acl.runner import (
    RUNNER_FAILURE_EXIT,
    RUNNER_SIGNATURE,
    RunnerFailure,
)
from miniharness.seams.sandbox_windows_acl import token as token_mod
from miniharness.seams.sandbox_windows_acl import win32_abi as abi
from miniharness.seams.sandbox_windows_acl.path_boundary import _contains_directory
from miniharness.seams.sandbox_windows_acl.workspace_sid import _MODULUS


def sid_bytes(subs, revision=1, authority=(0, 0, 0, 0, 0, 5)):
    """构造一个 SID 的原始字节。"""
    return bytes([revision, len(subs), *authority]) + b"".join(
        struct.pack("<I", s) for s in subs)


# ==================== 常量与 ABI 布局 ====================

class TestAbiConstants(unittest.TestCase):
    def test_grant_mask(self):
        # FILE_GENERIC_WRITE|DELETE|FILE_DELETE_CHILD 去 READ_CONTROL = icacls "Modify"
        self.assertEqual(abi.GRANT_MASK, 0x00110156)

    def test_layout_probes(self):
        self.assertEqual((abi.STARTUPINFOW_SIZE, abi.PROCESS_INFORMATION_SIZE), (104, 24))
        self.assertEqual(abi.EXPLICIT_ACCESS_W_SIZE, 48)
        self.assertEqual((abi.JOBOBJECT_EXTENDED_LIMIT_SIZE,
                          abi.JOBOBJECT_EXTENDED_LIMIT_FLAGS_OFFSET), (144, 16))
        self.assertEqual(abi.SID_AND_ATTRIBUTES_SIZE, 16)

    def test_token_flags(self):
        self.assertEqual(
            abi.DISABLE_MAX_PRIVILEGE | abi.LUA_TOKEN | abi.WRITE_RESTRICTED, 0xD)


class TestQuoteArg(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(spawn_mod.quote_arg("foo"), "foo")

    def test_empty(self):
        self.assertEqual(spawn_mod.quote_arg(""), '""')

    def test_space(self):
        self.assertEqual(spawn_mod.quote_arg("a b"), '"a b"')

    def test_trailing_backslashes_double(self):
        self.assertEqual(spawn_mod.quote_arg("a\\"), '"a\\\\"')
        self.assertEqual(spawn_mod.quote_arg("a\\\\"), '"a\\\\\\\\"')

    def test_embedded_quotes(self):
        self.assertEqual(spawn_mod.quote_arg('he said "hi"'), '"he said \\"hi\\""')

    def test_build_command_line(self):
        self.assertEqual(
            spawn_mod.build_command_line("cmd", ["/c", "a b"]),
            'cmd /c "a b"')


# ==================== SID 派生 ====================

class TestWorkspaceSid(unittest.TestCase):
    def test_format_and_determinism(self):
        first = workspace_write_sid("C:\\ws")
        self.assertRegex(first, r"^S-1-4-\d+-\d+$")
        self.assertEqual(first, workspace_write_sid("C:\\ws"))
        # 大小写收敛是调用方（sandbox-policy canonical）职责：派生按拼写原样
        self.assertNotEqual(workspace_write_sid("C:\\ws"), workspace_write_sid("C:\\WS"))

    def test_temp_sid_domain_separator(self):
        temp = temp_write_sid("C:\\Temp\\dsh-x")
        self.assertTrue(temp.endswith("-1"))
        self.assertRegex(temp, r"^S-1-4-\d+-\d+-1$")
        # 与同一字符串派生的工作区 SID 域分离（前缀 salt 不同）
        self.assertNotEqual(temp, workspace_write_sid("C:\\Temp\\dsh-x"))

    def test_subauthorities_within_30_bits(self):
        for path in ("", "\x00", "\U0001f600", "x" * 4096):
            for value in workspace_write_sid(path).split("-")[3:]:
                self.assertTrue(1 <= int(value) <= 2 ** 30 - 1)

    def test_modulus(self):
        self.assertEqual(_MODULUS, 2 ** 30 - 1)


# ==================== 路径边界 ====================

class TestPathBoundary(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.outer = tempfile.mkdtemp(prefix="dsh-acl-outer-")
        cls.inner = tempfile.mkdtemp(prefix="dsh-acl-inner-", dir=cls.outer)

    @classmethod
    def tearDownClass(cls):
        import shutil
        shutil.rmtree(cls.outer, ignore_errors=True)

    def test_contains_true(self):
        self.assertTrue(_contains_directory(self.outer, self.inner))
        self.assertTrue(_contains_directory(self.inner, self.inner))

    def test_contains_false(self):
        other = tempfile.mkdtemp(prefix="dsh-acl-other-")
        try:
            self.assertFalse(_contains_directory(self.outer, other))
            self.assertFalse(_contains_directory(self.inner, self.outer))
        finally:
            os.rmdir(other)

    def test_missing_path_fails_closed(self):
        with self.assertRaises(OSError):
            _contains_directory(self.outer, os.path.join(self.outer, "no-such-dir"))

    def test_cross_drive_is_not_contained(self):
        with mock.patch.object(os.path, "relpath", side_effect=ValueError):
            self.assertFalse(_contains_directory(self.outer, self.inner))

    def test_assert_temp_root_outside_workspace(self):
        assert_temp_root_outside_workspace(self.outer, tempfile.gettempdir())
        with self.assertRaises(ValueError):
            assert_temp_root_outside_workspace(self.outer, self.inner)

    def test_assert_private_temp_disjoint(self):
        sibling = tempfile.mkdtemp(prefix="dsh-acl-sibling-")
        try:
            assert_private_temp_disjoint([self.outer], sibling)
            with self.assertRaises(ValueError):
                assert_private_temp_disjoint([self.outer], self.inner)
            with self.assertRaises(ValueError):
                assert_private_temp_disjoint([self.inner], self.outer)
        finally:
            os.rmdir(sibling)


# ==================== 错误类型 ====================

class TestErrors(unittest.TestCase):
    def test_win32_error_message_shape(self):
        error = Win32Error("CreatePipe", 5)
        self.assertEqual(str(error), "CreatePipe failed (Win32 5)")
        detailed = Win32Error("OpenProcess", 87, "pid 1")
        self.assertEqual(str(detailed), "OpenProcess failed (Win32 87): pid 1")

    def test_aggregate_error_carries_list(self):
        inner = [RuntimeError("a"), ValueError("b")]
        aggregate = AggregateError(inner, "two failures")
        self.assertEqual(len(aggregate.errors), 2)


# ==================== EXPLICIT_ACCESS / ACL 步行 ====================

class FakeLockApi:
    """with_path_lock 所需的最小假 api（锁文件全走通）。"""

    def __init__(self, temp_root="C:\\Temp\\"):
        self._temp_root = temp_root
        self.last_error = 0
        self.closed = []

    def getLastError(self):
        return self.last_error

    def getTempPathW(self, capacity, buffer):
        buffer.value = self._temp_root
        return len(self._temp_root)

    def createFileW(self, *args):
        return 7

    def lockFileEx(self, *args):
        return 1

    def unlockFileEx(self, *args):
        return 1

    def closeHandle(self, handle):
        self.closed.append(handle)
        return 1


class TestExplicitAccess(unittest.TestCase):
    def test_layout(self):
        entry = acl_mod.build_explicit_access(0x1234, abi.GRANT_ACCESS, abi.GRANT_MASK)
        self.assertEqual(len(entry), 48)
        self.assertEqual(struct.unpack_from("<I", entry, 0)[0], abi.GRANT_MASK)
        self.assertEqual(struct.unpack_from("<I", entry, 4)[0], abi.GRANT_ACCESS)
        self.assertEqual(struct.unpack_from("<I", entry, 8)[0],
                         abi.SUB_CONTAINERS_AND_OBJECTS_INHERIT)
        self.assertEqual(struct.unpack_from("<Q", entry, 16)[0], 0)
        self.assertEqual(struct.unpack_from("<I", entry, 24)[0], abi.NO_MULTIPLE_TRUSTEE)
        self.assertEqual(struct.unpack_from("<I", entry, 28)[0], abi.TRUSTEE_IS_SID)
        self.assertEqual(struct.unpack_from("<I", entry, 32)[0], abi.TRUSTEE_IS_UNKNOWN)
        self.assertEqual(struct.unpack_from("<Q", entry, 40)[0], 0x1234)


class CraftedAcl:
    """内存 ACL 构造器：header + ACE 列表，供 _has_exact_grant 步行测试。"""

    def __init__(self):
        self.chunks = []
        self.count = 0

    def add_ace(self, ace_type, ace_flags, mask, sid_raw):
        ace_size = 8 + len(sid_raw)
        header = struct.pack("<BBH", ace_type, ace_flags, ace_size)
        self.chunks.append(header + struct.pack("<I", mask) + sid_raw)
        self.count += 1

    def raw(self):
        body = b"".join(self.chunks)
        return struct.pack("<BBHHH", 2, 0, 8 + len(body), self.count, 0) + body


def acl_in_memory(raw):
    buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
    return buffer, ctypes.addressof(buffer)


class SidInMemory:
    def __init__(self, raw):
        self.raw = raw
        self.buffer = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        self.address = ctypes.addressof(self.buffer)


class ExactGrantApi(FakeLockApi):
    def __init__(self, sid_raw):
        super().__init__()
        self.sid = SidInMemory(sid_raw)

    def getLengthSid(self, address):
        return len(self.sid.raw)


class TestHasExactGrant(unittest.TestCase):
    def setUp(self):
        self.sid_raw = sid_bytes([101])
        self.api = ExactGrantApi(self.sid_raw)

    def test_exact_match(self):
        crafted = CraftedAcl()
        crafted.add_ace(abi.ACCESS_ALLOWED_ACE_TYPE,
                        abi.SUB_CONTAINERS_AND_OBJECTS_INHERIT, abi.GRANT_MASK,
                        self.sid_raw)
        _, address = acl_in_memory(crafted.raw())
        self.assertTrue(acl_mod._has_exact_grant(self.api, address, self.api.sid.address))

    def test_different_mask_not_exact(self):
        crafted = CraftedAcl()
        # 0x20000 = STANDARD_RIGHTS_WRITE, explicitly masked OUT of GRANT_MASK
        crafted.add_ace(abi.ACCESS_ALLOWED_ACE_TYPE,
                        abi.SUB_CONTAINERS_AND_OBJECTS_INHERIT,
                        abi.GRANT_MASK | 0x20000, self.sid_raw)
        _, address = acl_in_memory(crafted.raw())
        self.assertFalse(acl_mod._has_exact_grant(self.api, address, self.api.sid.address))

    def test_deny_or_wrong_flags_not_exact(self):
        crafted = CraftedAcl()
        crafted.add_ace(1, abi.SUB_CONTAINERS_AND_OBJECTS_INHERIT, abi.GRANT_MASK, self.sid_raw)
        crafted.add_ace(abi.ACCESS_ALLOWED_ACE_TYPE, 0, abi.GRANT_MASK, self.sid_raw)
        _, address = acl_in_memory(crafted.raw())
        self.assertFalse(acl_mod._has_exact_grant(self.api, address, self.api.sid.address))

    def test_second_ace_found_after_walk(self):
        other = sid_bytes([202])
        crafted = CraftedAcl()
        crafted.add_ace(abi.ACCESS_ALLOWED_ACE_TYPE, 0, abi.GENERIC_READ, other)
        crafted.add_ace(abi.ACCESS_ALLOWED_ACE_TYPE,
                        abi.SUB_CONTAINERS_AND_OBJECTS_INHERIT, abi.GRANT_MASK,
                        self.sid_raw)
        _, address = acl_in_memory(crafted.raw())
        self.assertTrue(acl_mod._has_exact_grant(self.api, address, self.api.sid.address))

    def test_malformed_header_falls_back(self):
        raw = struct.pack("<BBHHH", 2, 0, 4, 9, 0)  # size < 8、ACE 数荒谬
        _, address = acl_in_memory(raw)
        self.assertFalse(acl_mod._has_exact_grant(self.api, address, self.api.sid.address))


class GrantFlowApi(ExactGrantApi):
    """grant_write / revoke_write 流程假 api（记录调用序列）。"""

    def __init__(self, sid_raw, acl_raw=None):
        super().__init__(sid_raw)
        self.calls = []
        if acl_raw is not None:
            self.acl_buffer, self.acl_address = acl_in_memory(acl_raw)
        else:
            self.acl_buffer = None
            self.acl_address = None
        self.descriptor_address = 0xDEAD

    def getNamedSecurityInfoW(self, path, obj, info, owner, group, dacl, sacl, descriptor):
        self.calls.append(("getNamedSecurityInfoW", path))
        dacl.value = self.acl_address
        # 描述符始终存在（即使无显式 DACL，也含缺省 DACL），始终设置地址
        descriptor.value = self.descriptor_address
        return abi.ERROR_SUCCESS

    def localFree(self, address):
        self.calls.append(("localFree", address))
        return None

    def setEntriesInAclW(self, count, entry, old_acl, new_slot):
        self.calls.append(("setEntriesInAclW", count))
        new_slot.value = 0xBEEF
        return abi.ERROR_SUCCESS

    def setNamedSecurityInfoW(self, *args):
        self.calls.append(("setNamedSecurityInfoW", args[0]))
        return abi.ERROR_SUCCESS


class TestGrantWrite(unittest.TestCase):
    def setUp(self):
        self.sid_raw = sid_bytes([303])

    def test_exact_grant_skips_apply(self):
        crafted = CraftedAcl()
        crafted.add_ace(abi.ACCESS_ALLOWED_ACE_TYPE,
                        abi.SUB_CONTAINERS_AND_OBJECTS_INHERIT, abi.GRANT_MASK,
                        self.sid_raw)
        api = GrantFlowApi(self.sid_raw, crafted.raw())
        acl_mod.grant_write(api, "C:\\ws", api.sid.address)
        names = [name for name, *_ in api.calls]
        # 幂等：精确 ACE 已在位 → 不合并不应用，释放描述符就是全部操作
        self.assertNotIn("setEntriesInAclW", names)
        self.assertNotIn("setNamedSecurityInfoW", names)
        self.assertIn("localFree", names)

    def test_no_dacl_merges_and_applies(self):
        api = GrantFlowApi(self.sid_raw, None)
        acl_mod.grant_write(api, "C:\\ws", api.sid.address)
        names = [name for name, *_ in api.calls]
        # 内存契约顺序：合并 → free 描述符 → 应用 → free 新 ACL
        self.assertLess(names.index("setEntriesInAclW"),
                        names.index("setNamedSecurityInfoW"))
        frees = [address for name, address in api.calls if name == "localFree"]
        self.assertIn(0xDEAD, frees)   # 描述符在应用前释放
        self.assertIn(0xBEEF, frees)   # 新 ACL 用后释放

    def test_revoke_without_dacl_returns_false(self):
        api = GrantFlowApi(self.sid_raw, None)
        api.acl_address = None
        self.assertFalse(acl_mod.revoke_write(api, "C:\\ws", api.sid.address))
        names = [name for name, *_ in api.calls]
        self.assertNotIn("setEntriesInAclW", names)

    def test_lock_failure_propagates(self):
        api = GrantFlowApi(self.sid_raw, None)
        api.createFileW = lambda *args: 0xFFFFFFFFFFFFFFFF
        api.last_error = 33
        with self.assertRaises(Win32Error) as caught:
            acl_mod.grant_write(api, "C:\\ws", api.sid.address)
        self.assertEqual(caught.exception.api, "CreateFileW")


# ==================== 令牌构造 ====================

class TokenApi:
    def __init__(self):
        self.last_error = 0
        self.closed = []
        self.groups_payload = b""
        self.default_dacl_pointer = 0x7777
        self.restricted_args = None
        self.set_token_args = None
        self.freed = []
        self.logon_raw = sid_bytes([5, 7])

    def getLastError(self):
        return self.last_error

    def openProcess(self, access, inherit, pid):
        return 11

    def openProcessToken(self, process, access, slot):
        slot.value = 22
        return 1

    def closeHandle(self, handle):
        self.closed.append(handle)
        return 1

    def getTokenInformation(self, token, info_class, buffer, length, needed_slot):
        if buffer is None:
            needed_slot.value = (8 + len(self.groups_payload)
                                 if info_class == abi.TokenGroups else 8)
            return 0
        if info_class == abi.TokenGroups:
            buffer[:len(self.groups_payload)] = self.groups_payload
        else:
            struct.pack_into("<Q", buffer, 0, self.default_dacl_pointer)
        return 1

    def getLengthSid(self, address):
        return len(self.logon_raw)

    def copySid(self, length, dest, source):
        dest[:length] = self.logon_raw
        return 1

    def createWellKnownSid(self, sid_type, domain, sid, size_slot):
        return 1

    def isValidSid(self, sid):
        return 1

    def setEntriesInAclW(self, count, entry, old_acl, new_slot):
        new_slot.value = 0xABC
        return abi.ERROR_SUCCESS

    def setTokenInformation(self, token, info_class, info, length):
        self.set_token_args = (token, info_class, bytes(info), length)
        return 1

    def localFree(self, address):
        self.freed.append(address)
        return None

    def createRestrictedToken(self, *args):
        self.restricted_args = args
        args[-1].value = 99
        return 1


class TestOpenCurrentProcessToken(unittest.TestCase):
    def test_happy_path_closes_process_handle(self):
        api = TokenApi()
        token = token_mod.open_current_process_token(api)
        self.assertEqual(token, 22)
        self.assertEqual(api.closed, [11])

    def test_token_failure_closes_process_handle_and_raises(self):
        api = TokenApi()
        api.openProcessToken = lambda process, access, slot: 0
        api.last_error = 5
        with self.assertRaises(Win32Error) as caught:
            token_mod.open_current_process_token(api)
        self.assertEqual((caught.exception.api, caught.exception.win32_code),
                         ("OpenProcessToken", 5))
        self.assertEqual(api.closed, [11])


class TestFindLogonSid(unittest.TestCase):
    def make_api_with_groups(self, groups):
        api = TokenApi()
        # TOKEN_GROUPS: DWORD GroupCount @0, 4 bytes padding, Groups[] @8 (TOKEN_GROUPS_OFFSET)
        api.groups_payload = struct.pack("<I4x", len(groups)) + b"".join(
            struct.pack("<QII", addr, attrs, 0) for addr, attrs in groups)
        return api

    def test_selects_logon_group_and_copies(self):
        api = self.make_api_with_groups([
            (0x1111, 0),                       # 非 logon 组
            (0x2222, abi.SE_GROUP_LOGON_ID),   # logon 组
        ])
        result = token_mod.find_logon_sid(api, 22)
        self.assertEqual(bytes(result), api.logon_raw)

    def test_no_logon_sid_raises(self):
        api = self.make_api_with_groups([(0x1111, 0)])
        with self.assertRaisesRegex(RuntimeError, "no logon SID"):
            token_mod.find_logon_sid(api, 22)


class TestCreateRestrictedToken(unittest.TestCase):
    def test_read_only_list(self):
        api = TokenApi()
        logon = SidInMemory(sid_bytes([5, 7]))
        world = SidInMemory(sid_bytes([1], authority=(0, 0, 0, 0, 0, 1)))
        handle = token_mod.create_restricted_token(
            api, 22, logon.buffer, [], token_mod.RestrictingSidSet(world=world.buffer),
            "read-only")
        self.assertEqual(handle, 99)
        flags = api.restricted_args[1]
        count = api.restricted_args[6]
        self.assertEqual(flags, 0xD)
        self.assertEqual(api.restricted_args[2], 0)   # 不禁用任何 SID
        self.assertEqual(api.restricted_args[4], 0)   # 不删除任何特权
        self.assertEqual(count, 2)

    def test_workspace_write_requires_write_sid(self):
        api = TokenApi()
        world = SidInMemory(sid_bytes([1]))
        with self.assertRaisesRegex(RuntimeError, "requires at least one write SID"):
            token_mod.create_restricted_token(
                api, 22, 1, [], token_mod.RestrictingSidSet(world=world.buffer),
                "workspace-write")

    def test_workspace_write_list_counts_sids(self):
        api = TokenApi()
        world = SidInMemory(sid_bytes([1]))
        writes = [SidInMemory(sid_bytes([10])).buffer, SidInMemory(sid_bytes([20])).buffer]
        token_mod.create_restricted_token(
            api, 22, SidInMemory(sid_bytes([9])).buffer, writes,
            token_mod.RestrictingSidSet(world=world.buffer), "workspace-write")
        self.assertEqual(api.restricted_args[6], 4)

    def test_packing_stride(self):
        blob = token_mod._build_restricting_sids([1, 2])
        self.assertEqual(blob, struct.pack("<QII", 1, 0, 0) + struct.pack("<QII", 2, 0, 0))


class TestSetTokenDefaultDaclGrant(unittest.TestCase):
    def test_merges_all_access_ace_and_frees(self):
        api = TokenApi()
        token_mod.set_token_default_dacl_grant(api, 22, 42)
        token_arg, info_class, info_bytes, length = api.set_token_args
        self.assertEqual((token_arg, info_class, length), (22, abi.TokenDefaultDacl, 8))
        self.assertEqual(struct.unpack("<Q", info_bytes)[0], 0xABC)
        self.assertIn(0xABC, api.freed)

    def test_missing_default_dacl_raises(self):
        api = TokenApi()

        def failing_get(token, info_class, buffer, length, needed_slot):
            if buffer is not None:
                struct.pack_into("<Q", buffer, 0, 0)   # 缺省 DACL 为 NULL
                return 1
            needed_slot.value = 8
            return 0

        api.getTokenInformation = failing_get
        with self.assertRaisesRegex(RuntimeError, "no default DACL"):
            token_mod.set_token_default_dacl_grant(api, 22, 42)


if __name__ == "__main__":
    unittest.main()

