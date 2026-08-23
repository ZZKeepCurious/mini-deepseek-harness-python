"""受限令牌构造（上游 token.ts 对应物）：打开当前进程令牌、提取其 logon
SID、构建 well-known SID、以 POC 的 restricting-SID allowlist 调
CreateRestrictedToken。每个 API 调用都检查；任何失败带 API 名与精确 Win32
错误码抛出——原 POC 忽略了这一切并静默以**完整未受限**令牌跑子进程。"""
from __future__ import annotations

import os
import struct

from .ffi import (alloc_bytes, alloc_ptr_slot, alloc_uint32, decode_ptr, decode_uint32,
                  encode_uint32, ptr_address, throw_last_error, throw_win32)
from . import win32_abi as abi


def open_current_process_token(api):
    """以 CreateRestrictedToken 所需权限打开当前进程访问令牌（POC 的
    OpenProcessToken 调用；经真实 OpenProcess 句柄取——GetCurrentProcess()
    伪句柄无法穿过 FFI 寻址）。"""
    pid = os.getpid()
    process_handle = api.openProcess(abi.PROCESS_QUERY_INFORMATION, 0, pid)
    if process_handle is None or process_handle == 0:
        throw_last_error(api, "OpenProcess", f"pid {pid}")

    token_slot = alloc_ptr_slot()
    opened = api.openProcessToken(
        process_handle,
        abi.TOKEN_QUERY | abi.TOKEN_DUPLICATE | abi.TOKEN_ADJUST_DEFAULT | abi.TOKEN_ASSIGN_PRIMARY,
        token_slot,
    )
    if opened == 0:
        win32_code = api.getLastError()
        api.closeHandle(process_handle)  # 错误路径 best-effort
        throw_win32(api, "OpenProcessToken", win32_code, f"pid {pid}")
    if api.closeHandle(process_handle) == 0:
        throw_last_error(api, "CloseHandle", "OpenProcess process handle")
    token = decode_ptr(token_slot)
    if token is None:
        throw_win32(api, "OpenProcessToken", api.getLastError(), "null token handle")
    return token


def find_logon_sid(api, token):
    """找到并拷贝令牌的 logon 会话 SID（S-1-5-5-x-y，属性 SE_GROUP_LOGON_ID）。
    受限令牌需要它访问 WinSta0/desktop 等每登录对象；POC 同法提取。令牌无
    logon SID 时抛错。"""
    needed_slot = alloc_uint32()
    # 预期以 ERROR_INSUFFICIENT_BUFFER 失败：这是尺寸探测
    api.getTokenInformation(token, abi.TokenGroups, None, 0, needed_slot)
    needed = decode_uint32(needed_slot)
    if needed == 0:
        throw_last_error(api, "GetTokenInformation", "TokenGroups size query")
    if needed < abi.TOKEN_GROUPS_OFFSET:
        throw_win32(api, "GetTokenInformation", api.getLastError(), f"implausible TokenGroups size {needed}")

    groups = alloc_bytes(needed)
    if api.getTokenInformation(token, abi.TokenGroups, groups, len(groups), needed_slot) == 0:
        throw_last_error(api, "GetTokenInformation", "TokenGroups")
    data = bytes(groups)
    group_count = struct.unpack_from("<I", data, 0)[0]
    for index in range(group_count):
        base = abi.TOKEN_GROUPS_OFFSET + index * abi.SID_AND_ATTRIBUTES_SIZE
        sid_addr = int.from_bytes(data[base:base + 8], "little") or None
        attributes = struct.unpack_from("<I", data, base + 8)[0]
        # SE_GROUP_LOGON_ID 位 31 置位：掩码比较须按无符号语义
        is_logon_id = (attributes & abi.SE_GROUP_LOGON_ID) == abi.SE_GROUP_LOGON_ID
        if sid_addr is None or not is_logon_id:
            continue
        sid_length = api.getLengthSid(sid_addr)
        if sid_length == 0:
            throw_last_error(api, "GetLengthSid", f"logon SID group {index}")
        copy = alloc_bytes(sid_length)
        if api.copySid(sid_length, copy, sid_addr) == 0:
            throw_last_error(api, "CopySid", f"logon SID group {index}")
        return copy
    raise RuntimeError(
        f"CreateRestrictedToken prerequisite failed: no logon SID found among {group_count} token groups")


def make_well_known_sid(api, sid_type: int):
    """创建一个 well-known SID（68 字节缓冲）并断言其有效。"""
    sid = alloc_bytes(abi.SECURITY_MAX_SID_SIZE)
    size_slot = alloc_uint32()
    encode_uint32(size_slot, abi.SECURITY_MAX_SID_SIZE)
    if api.createWellKnownSid(sid_type, None, sid, size_slot) == 0:
        throw_last_error(api, "CreateWellKnownSid", f"type {sid_type}")
    if api.isValidSid(sid) == 0:
        throw_last_error(api, "IsValidSid", f"CreateWellKnownSid type {sid_type}")
    return sid


def set_token_default_dacl_grant(api, token, sid_addr: int) -> None:
    """为 ``sid_addr`` 向令牌**缺省 DACL** 合并一个全访问 Allow ACE——未带
    显式 SD 的新对象继承的 DACL。受限令牌逐字继承用户的缺省 DACL，而其中不
    指名任何 restricting SID：新建匿名管道（子 stdio）会在创建时过不了写
    pass-2 检查（ERROR_ACCESS_DENIED；Node 呈现为 spawn EPERM），一切管道
    stdio 的孙进程 spawn 全断。合并的 ACE 指名一个 **restricting SID**
    （workspace-write 用写 SID，read-only 用 Everyone），新对象的自身 DACL
    因此能过 pass-2，而对象创建本身仍被父容器 DACL 把关（授权树之外的文件
    依旧不可创建）。fail-closed：任何 Win32 失败在 spawn 之前抛出。"""
    needed_slot = alloc_uint32()
    # 预期以 ERROR_INSUFFICIENT_BUFFER 失败：尺寸探测
    api.getTokenInformation(token, abi.TokenDefaultDacl, None, 0, needed_slot)
    needed = decode_uint32(needed_slot)
    if needed == 0:
        throw_last_error(api, "GetTokenInformation", "TokenDefaultDacl size query")
    buffer = alloc_bytes(needed)
    if api.getTokenInformation(token, abi.TokenDefaultDacl, buffer, len(buffer), needed_slot) == 0:
        throw_last_error(api, "GetTokenInformation", "TokenDefaultDacl")
    current_dacl = int.from_bytes(bytes(buffer[:8]), "little") or None
    if current_dacl is None:
        raise RuntimeError("setTokenDefaultDaclGrant: the token carries no default DACL to extend")
    from .acl import build_explicit_access

    new_dacl_slot = alloc_ptr_slot()
    result = api.setEntriesInAclW(
        1,
        build_explicit_access(sid_addr, abi.GRANT_ACCESS, abi.FILE_ALL_ACCESS),
        current_dacl,
        new_dacl_slot,
    )
    if result != abi.ERROR_SUCCESS:
        throw_win32(api, "SetEntriesInAclW", result, "default DACL merge")
    new_dacl = decode_ptr(new_dacl_slot)
    if new_dacl is None:
        throw_win32(api, "SetEntriesInAclW", result, "null merged default DACL")
    # TOKEN_DEFAULT_DACL { PACL DefaultDacl; }——结构就是一个指针；
    # SetTokenInformation 返回前已把 ACL 拷走。
    info = struct.pack("<Q", new_dacl)
    if api.setTokenInformation(token, abi.TokenDefaultDacl, info, len(info)) == 0:
        win32_code = api.getLastError()
        api.localFree(new_dacl)
        throw_win32(api, "SetTokenInformation", win32_code, "TokenDefaultDacl")
    api.localFree(new_dacl)


def _build_restricting_sids(sid_addrs: list[int]) -> bytes:
    """打包 ``SID_AND_ATTRIBUTES[count]``（16 字节步长；Attributes 保持 0）。"""
    return b"".join(struct.pack("<QII", addr, 0, 0) for addr in sid_addrs)


class RestrictingSidSet:
    """进入每个受限令牌 restricting 列表的 well-known SID。"""

    def __init__(self, world):
        self.world = world


def _as_ptr(value) -> int:
    """SID 形态归一：已是数值地址则原样，ctypes 缓冲区取其地址。"""
    return value if isinstance(value, int) else ptr_address(value)


def create_restricted_token(api, current_token, logon_sid, write_sids: list, known: RestrictingSidSet,
                            mode: str):
    """以模式选择的 restricting 列表创建写受限令牌（Win11 26200 实证，见
    POC-worktree restrict-variant harness）：
      * read-only:       [logon SID, EVERYONE]
      * workspace-write: [logon SID, EVERYONE, workspace SID, 可选 temp SID]

    logon SID + EVERYONE keep-alive 组两种模式共享：没有它们早期 DLL 初始化
    死于 0xC0000142、CNG（\\Device\\CNG 写 trustee——pwsh 崩溃 0xE0434352）
    不可用。写 SID 只进 workspace-write——read-only 不携带写 SID，因此早前
    workspace-write 时段的常驻授权 ACE（/permission 降级或崩溃恢复会话）在
    read-only 下保持**惰性**：WRITE_RESTRICTED pass-2 只授予 restricting 列表
    携带的东西，同时未撤销的 ACE 让再升级免费（grant 的 exact-ACE 跳过，
    无需重传播）。Everyone 自身的 ambient 授权是文档化的 partial 边界。
    Authenticated Users 不进两个列表：WMI 命名空间安全检查失败（0x80041003）
    → CIM 在一切受限模式不可用，且 C:\\ 根建树逃逸（常驻 AU:(AD) +
    AU:(OI)(CI)(IO)(M) ACE）双双关闭。INTERACTIVE/LOCAL 同样缺席——宿主对
    INTERACTIVE 开放 Public 树写，移除即关闭该逃逸。S-1-2-1 有意缺席：
    见 win32_abi 模块注记。FAIL-CLOSED：任何失败抛出——绝不裸放子进程。
    """
    if mode == "read-only":
        restricting = _build_restricting_sids([_as_ptr(logon_sid), _as_ptr(known.world)])
    else:
        if not write_sids:
            raise RuntimeError("createRestrictedToken: workspace-write restricting list requires at least one write SID")
        addresses = [_as_ptr(logon_sid), _as_ptr(known.world)] + [_as_ptr(s) for s in write_sids]
        restricting = _build_restricting_sids(addresses)
    token_slot = alloc_ptr_slot()
    created = api.createRestrictedToken(
        current_token,
        abi.DISABLE_MAX_PRIVILEGE | abi.LUA_TOKEN | abi.WRITE_RESTRICTED,
        0, None,   # 不禁用任何 SID
        0, None,   # 不删除任何特权
        len(restricting) // abi.SID_AND_ATTRIBUTES_SIZE,
        restricting,
        token_slot,
    )
    if created == 0:
        throw_last_error(api, "CreateRestrictedToken",
                         f"restricting SIDs: {len(restricting) // abi.SID_AND_ATTRIBUTES_SIZE}")
    token = decode_ptr(token_slot)
    if token is None:
        throw_win32(api, "CreateRestrictedToken", api.getLastError(), "null token handle")
    return token
