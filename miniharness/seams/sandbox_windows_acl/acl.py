"""ACL 编辑助手：经 SetEntriesInAclW + SetNamedSecurityInfoW 在目录上授予/
撤销能力 SID 的 Write ACE（上游 acl.ts 对应物——与 POC 相同的两个调用，补上
POC 缺失的失败处理）。每个 API 调用都检查，失败一律带 API 名、精确 Win32
错误码、格式化系统文本与受影响路径上报。

并发：授权是对目录**当前** DACL 的读-合并-写，整段 get-merge-set 序列在
每路径排他 LockFileEx 锁下运行（with_path_lock），并发沙箱实例不会互相
覆盖 ACE。

内存契约（上游 readCurrentDacl 注释原样保留）：GetNamedSecurityInfoW 返回的
ACL 指针位于安全描述符分配**内部**——只能 LocalFree 描述符本身，且必须在
SetEntriesInAclW 消费完 ACL 之后；单独 free ACL 指针会破坏堆（上游实证）。
"""
from __future__ import annotations

import ctypes
import hashlib
import os
import struct

from .errors import Win32Error
from .ffi import (alloc_overlapped, alloc_ptr_slot, decode_ptr, decode_uint16_at,
                  decode_uint8_at, decode_uint32_at, get_temp_path, is_invalid_handle,
                  is_null_ptr, ptr_address, throw_last_error, throw_win32)
from . import win32_abi as abi


def build_explicit_access(sid_addr: int, mode: int, permissions: int) -> bytes:
    """打包一个 EXPLICIT_ACCESS_W（48 字节，布局经上游 abi-probe.cpp 验证）：
    perms@0, mode@4, inheritance@8, Trustee@16 { pMultipleTrustee@16(空),
    MultipleTrusteeOperation@24, TrusteeForm@28, TrusteeType@32,
    ptstrName@40 }。``permissions`` 是访问掩码；REVOKE_ACCESS 时传 0，
    SetEntriesInAclW 会移除该 trustee 的全部 ACE。返回不可变 bytes——
    ctypes 的 c_void_p 形参只接受 bytes/ctypes 缓冲，不接受 bytearray。"""
    entry = bytearray(abi.EXPLICIT_ACCESS_W_SIZE)
    struct.pack_into("<I", entry, 0, permissions)          # grfAccessPermissions
    struct.pack_into("<I", entry, 4, mode)                 # grfAccessMode
    struct.pack_into("<I", entry, 8, abi.SUB_CONTAINERS_AND_OBJECTS_INHERIT)  # grfInheritance: OI|CI
    # pMultipleTrustee（16..23）保持空零
    struct.pack_into("<I", entry, 24, abi.NO_MULTIPLE_TRUSTEE)   # Trustee.MultipleTrusteeOperation
    struct.pack_into("<I", entry, 28, abi.TRUSTEE_IS_SID)        # Trustee.TrusteeForm
    struct.pack_into("<I", entry, 32, abi.TRUSTEE_IS_UNKNOWN)    # Trustee.TrusteeType
    struct.pack_into("<Q", entry, 40, sid_addr)                  # Trustee.ptstrName = 能力 SID
    return bytes(entry)


def lock_file_path(api, path: str) -> str:
    r"""每个受保护路径一把锁文件：``<GetTempPathW()>\dsh-acl-locks\<sha256(
    小写路径) 前 16 hex>.lock``。锁根只从 GetTempPathW 派生（绝不用 runner
    argv 或 DSH_HOME）；小写化把 Windows 大小写不敏感的路径拼写映射到同一把锁。"""
    digest = hashlib.sha256(path.lower().encode("utf-8")).hexdigest()[:16]
    return os.path.join(get_temp_path(api), "dsh-acl-locks", f"{digest}.lock")


def with_path_lock(api, path: str, action):
    """持每路径排他锁执行 ``action``：CreateFileW（OPEN_ALWAYS，共享读/写但
    **不**共享删除——可删的锁文件会被删掉重建，两个进程就能各持"同一把锁"），
    然后一字节 LockFileEx（LOCKFILE_EXCLUSIVE_LOCK + 全零 OVERLAPPED = 同步
    句柄上从偏移 0 锁起），最后 UnlockFileEx + CloseHandle。fail-closed：
    open/lock/unlock/close 失败照常抛；action 失败仍 best-effort 解锁并重抛
    原错误。"""
    lock_path = lock_file_path(api, path)
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    handle = api.createFileW(
        lock_path,
        abi.GENERIC_READ | abi.GENERIC_WRITE,
        abi.FILE_SHARE_READ | abi.FILE_SHARE_WRITE,
        None, abi.OPEN_ALWAYS, 0, None,
    )
    if is_invalid_handle(handle):
        throw_last_error(api, "CreateFileW", lock_path)
    overlapped = alloc_overlapped()  # 保持全零：offset 0，hEvent NULL
    if api.lockFileEx(handle, abi.LOCKFILE_EXCLUSIVE_LOCK, 0, 1, 0, overlapped) == 0:
        win32_code = api.getLastError()
        api.closeHandle(handle)  # 锁失败路径 best-effort
        throw_win32(api, "LockFileEx", win32_code, lock_path)

    try:
        result = action()
    except BaseException:
        # action 失败路径 best-effort 释放：清理失败不得掩盖 action 的错误
        api.unlockFileEx(handle, 0, 1, 0, overlapped)
        api.closeHandle(handle)
        raise
    if api.unlockFileEx(handle, 0, 1, 0, overlapped) == 0:
        win32_code = api.getLastError()
        api.closeHandle(handle)  # 解锁失败路径 best-effort
        throw_win32(api, "UnlockFileEx", win32_code, lock_path)
    if api.closeHandle(handle) == 0:
        throw_last_error(api, "CloseHandle", f"lock file {lock_path}")
    return result


def _read_current_dacl(api, path: str) -> dict:
    """经 GetNamedSecurityInfoW 读目录当前显式 DACL。返回 {old_acl,
    descriptor}（目录没有显式 DACL 时 old_acl 为 None）。"""
    owner_slot = alloc_ptr_slot()
    group_slot = alloc_ptr_slot()
    dacl_slot = alloc_ptr_slot()
    sacl_slot = alloc_ptr_slot()
    descriptor_slot = alloc_ptr_slot()
    read_result = api.getNamedSecurityInfoW(
        path, abi.SE_FILE_OBJECT, abi.DACL_SECURITY_INFORMATION,
        owner_slot, group_slot, dacl_slot, sacl_slot, descriptor_slot,
    )
    if read_result != abi.ERROR_SUCCESS:
        throw_win32(api, "GetNamedSecurityInfoW", read_result, path)
    return {"old_acl": decode_ptr(dacl_slot), "descriptor": decode_ptr(descriptor_slot)}


def _merge_and_apply(api, path: str, entry: bytes, old_acl, descriptor, label: str) -> None:
    """grantWrite / revokeWrite 共用尾部：entry 合并进 old_acl（None = 尚无
    显式 DACL；SetEntriesInAclW 从零构建），先 free 描述符再应用合并结果，
    最后 free 合并 ACL——每次调用都检查并以调用方标签上报。"""
    new_acl_slot = alloc_ptr_slot()
    merge_result = api.setEntriesInAclW(1, entry, old_acl, new_acl_slot)
    if merge_result != abi.ERROR_SUCCESS:
        if descriptor is not None:
            api.localFree(descriptor)  # 连带释放其中的 ACL 块
        throw_win32(api, "SetEntriesInAclW", merge_result, f"{label}({path})")
    new_acl = decode_ptr(new_acl_slot)
    if new_acl is None:
        if descriptor is not None:
            api.localFree(descriptor)
        throw_win32(api, "SetEntriesInAclW", api.getLastError(), f"{label}({path}): null new ACL")

    # 描述符块（含 old_acl）在合并后即死——应用前 free，与 POC 一致
    freed_descriptor = api.localFree(descriptor) if descriptor is not None else None
    apply_result = api.setNamedSecurityInfoW(
        path, abi.SE_FILE_OBJECT, abi.DACL_SECURITY_INFORMATION,
        None, None, new_acl, None,
    )
    freed_new = api.localFree(new_acl)
    if apply_result != abi.ERROR_SUCCESS:
        throw_win32(api, "SetNamedSecurityInfoW", apply_result, f"{label}({path})")
    if freed_descriptor is not None and not is_null_ptr(freed_descriptor):
        throw_last_error(api, "LocalFree", f"{label}({path}) descriptor")
    if not is_null_ptr(freed_new):
        throw_last_error(api, "LocalFree", f"{label}({path}) new ACL")


def _sid_bytes(api, sid_addr: int) -> bytes:
    """按 GetLengthSid 有界读取 SID 原始字节（ACE 内联 SID 比较的基准）。"""
    length = api.getLengthSid(sid_addr)
    if length == 0:
        throw_last_error(api, "GetLengthSid", f"capability SID at {sid_addr:#x}")
    return ctypes_string_at(sid_addr, length)


def ctypes_string_at(ptr: int, length: int) -> bytes:
    """指针处定长字节读取（有界）。"""
    return ctypes.string_at(ptr, length)


def _same_inline_sid(ace_sid: bytes, sid: bytes) -> bool:
    """ACE 内联 SID 字节切片 vs 能力 SID 字节：逐字段有界比较
    （revision/count/authority/子权限），语义同 ffi.same_sid_at。"""
    if len(sid) < 8 or len(ace_sid) < len(sid):
        return False
    count = ace_sid[1]
    if count > abi.SID_MAX_SUB_AUTHORITIES or len(sid) != 8 + count * 4:
        return False
    return ace_sid[:len(sid)] == sid


def _has_exact_grant(api, old_acl: int, sid_addr: int) -> bool:
    """显式 DACL 已携带本模块将添加的**精确**写授权（Allow ACE、OI|CI 继承、
    GRANT_MASK、能力 SID）时为真。ACE 的 SID 是内联的（嵌在 4 字节掩码之后，
    没有指针可读——读指针只会得到垃圾地址并让 EqualSid 崩溃，上游 gdb 实证），
    故以有界字节切片逐字段比对。畸形头部读作「无精确授权」，调用方回退到
    合并-应用路径（那里拥有健壮的失败处理）。"""
    sid = _sid_bytes(api, sid_addr)
    acl_size = decode_uint16_at(old_acl, 2)
    ace_count = decode_uint16_at(old_acl, 4)
    if acl_size < 8 or acl_size > 1_048_576:
        return False  # 不合理：回退合并路径
    acl_data = ctypes_string_at(old_acl, acl_size)
    offset = 8  # 首个 ACE 跟在 8 字节 ACL 头之后
    for _ in range(ace_count):
        if offset + 8 > acl_size:
            return False  # 不合理：回退合并路径
        # ACE_HEADER: AceType@0, AceFlags@1, AceSize@2 (WORD);
        # ACCESS_ALLOWED_ACE: Mask@4, 内联 SID@8。
        ace_size = decode_uint16_at(old_acl, offset + 2)
        if ace_size < 8 or offset + ace_size > acl_size:
            return False  # 不合理：回退合并路径
        exact = (
            decode_uint8_at(old_acl, offset) == abi.ACCESS_ALLOWED_ACE_TYPE
            and decode_uint8_at(old_acl, offset + 1) == abi.SUB_CONTAINERS_AND_OBJECTS_INHERIT
            and decode_uint32_at(old_acl, offset + 4) == abi.GRANT_MASK
        )
        if exact and _same_inline_sid(acl_data[offset + 8:offset + ace_size], sid):
            return True
        offset += ace_size
    return False


def grant_write(api, path: str, sid_addr: int) -> None:
    """把 GRANT_MASK（Write+Delete，icacls 显示 "Modify"）授予能力 SID 于
    path，继承到子容器与对象。幂等：目录当前显式 DACL 已带精确 ACE（上一
    服务生命周期的会话授权残留）时跳过 SetNamedSecurityInfoW 应用——否则会
    把相同 ACE 急切重传播全树（大工作区要分钟级）。否则读-合并-写：新 ACE
    并入目录**当前**显式 DACL，既有显式 ACE 全部保留。全程持每路径锁。目录
    须由调用方所有（owner 隐含 WRITE_DAC）——与 POC 同前提。"""
    def action():
        current = _read_current_dacl(api, path)
        if current["old_acl"] is not None and _has_exact_grant(api, current["old_acl"], sid_addr):
            # 精确 ACE 已在位：释放描述符就是全部操作
            if current["descriptor"] is not None:
                freed = api.localFree(current["descriptor"])
                if not is_null_ptr(freed):
                    throw_last_error(api, "LocalFree", f"grantWrite({path}) descriptor")
            return
        _merge_and_apply(api, path, build_explicit_access(sid_addr, abi.GRANT_ACCESS, abi.GRANT_MASK),
                         current["old_acl"], current["descriptor"], "grantWrite")
    with_path_lock(api, path, action)


def revoke_write(api, path: str, sid_addr: int) -> bool:
    """从目录 DACL 移除该能力 SID 的全部 ACE（REVOKE_ACCESS 合并——其它条目
    保留）。返回是否真的尝试了移除（目录完全没有 DACL 时为 False）。
    整段 get-merge-set 持每路径锁。"""
    def action():
        current = _read_current_dacl(api, path)
        if current["old_acl"] is None:
            if current["descriptor"] is not None:
                freed = api.localFree(current["descriptor"])
                if not is_null_ptr(freed):
                    throw_last_error(api, "LocalFree", f"revokeWrite({path}) descriptor")
            return False
        _merge_and_apply(api, path, build_explicit_access(sid_addr, abi.REVOKE_ACCESS, 0),
                         current["old_acl"], current["descriptor"], "revokeWrite")
        return True
    return with_path_lock(api, path, action)
