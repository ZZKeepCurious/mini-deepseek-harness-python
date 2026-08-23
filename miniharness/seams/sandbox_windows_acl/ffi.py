"""Win32 ACL 沙箱后端的惰性绑定表（上游 ffi.ts 的 ctypes 等价物）。

上游以 koffi 惰性加载 kernel32/advapi32，非 Windows 进程永远不打开 Win32
库；本模块同纪律：``win32_sync()`` 首次调用才建表并缓存，非 win32 宿主直接
抛 RuntimeError（fail-closed）。每个函数签名对照真实 Windows 头文件声明
（winnt.h / accctrl.h / aclapi.h / securitybaseapi.h / sddl.h /
processthreadsapi.h / fileapi.h / namedpipeapi.h / synchapi.h / winbase.h）；
STARTUPINFOW / PROCESS_INFORMATION 结构布局由 ctypes 按 C 规则生成并在建
模块时与 ABI 常量断言（上游 koffi.struct 同款加载期校验）。

载体差异（语义不变）：
  * 句柄一律以 Python int（地址）表示，NULL 即 ``None``/0；
    INVALID_HANDLE_VALUE 是无符号全一（0xFFFFFFFFFFFFFFFF）。
  * ``use_last_error=True`` 让 ctypes 在每次调用前后保存恢复 lasterror，
    ``getLastError()`` 读它——等价于上游显式绑定 GetLastError 且不会被其它
    Win32 调用冲掉。
  * 上游 ``win32()`` 的 Promise 包装只服务 runner 的 await 形态；底层 koffi
    加载本就是同步的，mini 只保留同步入口 ``win32_sync()``。
"""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes

from .errors import Win32Error
from . import win32_abi as abi

HANDLE = wintypes.HANDLE          # c_void_p
PHANDLE = ctypes.POINTER(HANDLE)
PDWORD = ctypes.POINTER(wintypes.DWORD)
LPCWSTR = wintypes.LPCWSTR
LPWSTR = wintypes.LPWSTR


class STARTUPINFOW(ctypes.Structure):
    """x64 布局（processthreadsapi.h）：cb@0(4)+pad，lpReserved/lpDesktop/
    lpTitle @8..31，dwX..dwFlags 八个 DWORD @32..63，wShowWindow@64 与
    cbReserved2@66（pad 到 72），lpReserved2@72，hStdInput/hStdOutput/
    hStdError @80..103 → 共 104。

    字段一律用固定宽度 c_uint32/c_uint16（koffi 同款平台无关宽度）：
    ``wintypes.DWORD = c_ulong`` 在非 Windows 宿主是 8 字节，同一份定义会
    算出 136，加载期 ABI 断言直接炸掉整个包（Linux CI 实证）。"""

    _fields_ = [
        ("cb", ctypes.c_uint32),
        ("lpReserved", LPWSTR),
        ("lpDesktop", LPWSTR),
        ("lpTitle", LPWSTR),
        ("dwX", ctypes.c_uint32),
        ("dwY", ctypes.c_uint32),
        ("dwXSize", ctypes.c_uint32),
        ("dwYSize", ctypes.c_uint32),
        ("dwXCountChars", ctypes.c_uint32),
        ("dwYCountChars", ctypes.c_uint32),
        ("dwFillAttribute", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("wShowWindow", ctypes.c_uint16),
        ("cbReserved2", ctypes.c_uint16),
        ("lpReserved2", ctypes.POINTER(ctypes.c_ubyte)),
        ("hStdInput", HANDLE),
        ("hStdOutput", HANDLE),
        ("hStdError", HANDLE),
    ]


class PROCESS_INFORMATION(ctypes.Structure):
    """同样固定宽度：两个句柄 @0..15 + 两个 DWORD @16..23 → 共 24。"""

    _fields_ = [
        ("hProcess", HANDLE),
        ("hThread", HANDLE),
        ("dwProcessId", ctypes.c_uint32),
        ("dwThreadId", ctypes.c_uint32),
    ]


if ctypes.sizeof(STARTUPINFOW) != abi.STARTUPINFOW_SIZE:  # pragma: no cover - ABI 断裂守卫
    raise RuntimeError(
        f"STARTUPINFOW layout mismatch: computed {ctypes.sizeof(STARTUPINFOW)}, header probe {abi.STARTUPINFOW_SIZE}")
if ctypes.sizeof(PROCESS_INFORMATION) != abi.PROCESS_INFORMATION_SIZE:  # pragma: no cover - 同上
    raise RuntimeError(
        f"PROCESS_INFORMATION layout mismatch: computed {ctypes.sizeof(PROCESS_INFORMATION)}, "
        f"header probe {abi.PROCESS_INFORMATION_SIZE}")


def is_null_ptr(value) -> bool:
    """koffi 可能交还 NULL 的各种形态统一判空。"""
    return value is None or value == 0


def is_invalid_handle(handle) -> bool:
    """CreateFileW 失败标记（-1 → 无符号全一指针）。"""
    if is_null_ptr(handle):
        return True
    return handle == 0xFFFFFFFFFFFFFFFF or handle == -1


def alloc_ptr_slot():
    """一个指针尺寸槽位（``T**`` 出参用）。"""
    return HANDLE()


def alloc_uint32():
    """一个 uint32 槽位。"""
    return wintypes.DWORD()


def encode_uint32(slot, value: int) -> None:
    slot.value = value


def decode_uint32(slot) -> int:
    return slot.value


def decode_ptr(slot):
    """解出槽位里存的指针值（NULL → None）。"""
    return slot.value


def alloc_bytes(length: int):
    """裸字节块（SID 拷贝 / 变长数组用），生命周期由返回对象持有。"""
    return (ctypes.c_ubyte * length)()


def ptr_address(buffer) -> int:
    """ctypes 缓冲区 → 数值地址（供手工结构打包用）。"""
    return ctypes.addressof(buffer)


def alloc_overlapped():
    """全零 OVERLAPPED（x64 32 字节）。LockFileEx/UnlockFileEx 收它而不是
    NULL：koffi 3.1.1 在 NULL 处崩溃，同步句柄上的全零 OVERLAPPED 是文档
    等价物（从偏移 0 起锁，hEvent 保持 NULL）。ctypes 无此崩溃但保持同形，
    免去载体分叉。"""
    return alloc_bytes(32)


def alloc_startup_info() -> STARTUPINFOW:
    return STARTUPINFOW()


def encode_startup_info(startup_info: STARTUPINFOW, fields: dict) -> None:
    """把 stdio 相关字段写进清零的 STARTUPINFOW（其余字段保持零初始化）。"""
    startup_info.cb = fields["cb"]
    startup_info.dwFlags = fields["dwFlags"]
    startup_info.hStdInput = fields["hStdInput"]
    startup_info.hStdOutput = fields["hStdOutput"]
    startup_info.hStdError = fields["hStdError"]


def alloc_process_info() -> PROCESS_INFORMATION:
    return PROCESS_INFORMATION()


def decode_process_info(process_info: PROCESS_INFORMATION) -> dict:
    """CreateProcessAsUserW 之后解码 PROCESS_INFORMATION。"""
    return {
        "hProcess": process_info.hProcess,
        "hThread": process_info.hThread,
        "dwProcessId": process_info.dwProcessId,
        "dwThreadId": process_info.dwThreadId,
    }


# ---- 定点读原语（ACL 步行 / TOKEN_GROUPS 解码用） ---------------------------

def decode_ptr_at(buffer, offset: int):
    """读缓冲区内 offset 处存的指针**值**（如 TOKEN_GROUPS 条目）。"""
    value = int.from_bytes(bytes(buffer[offset:offset + 8]), "little")
    return None if value == 0 else value


def decode_uint8_at(ptr: int, offset: int) -> int:
    return ctypes.string_at(ptr + offset, 1)[0]


def decode_uint16_at(ptr: int, offset: int) -> int:
    return int.from_bytes(ctypes.string_at(ptr + offset, 2), "little")


def decode_uint32_at(ptr: int, offset: int) -> int:
    return int.from_bytes(ctypes.string_at(ptr + offset, 4), "little")


def same_sid_at(left: int, left_offset: int, right: int, right_offset: int) -> bool:
    """逐字段比较两个 SID（revision/count/authority/子权限全部有界读取）。

    不做定长结构解码——那会读过短 SID 分配的尾部（少于 8 个子权限的 SID 比
    SID_STRUCT 短）；子权限数不合理视为不等。
    """
    left_revision = decode_uint8_at(left, left_offset)
    right_revision = decode_uint8_at(right, right_offset)
    if left_revision != right_revision:
        return False
    left_count = decode_uint8_at(left, left_offset + 1)
    right_count = decode_uint8_at(right, right_offset + 1)
    if left_count != right_count or left_count > abi.SID_MAX_SUB_AUTHORITIES:
        return False
    for index in range(6):
        if decode_uint8_at(left, left_offset + 2 + index) != decode_uint8_at(right, right_offset + 2 + index):
            return False
    for index in range(left_count):
        if decode_uint32_at(left, left_offset + 8 + index * 4) != decode_uint32_at(right, right_offset + 8 + index * 4):
            return False
    return True


class Win32Bindings:
    """绑定表：后端用到的每个 Win32 调用，签名对真实头文件核实。

    测试以同形假对象整体替换本表（上游测试同策略 mock 整个 api 对象）。
    """

    def getLastError(self) -> int:  # noqa: N802 - 与上游绑定名逐字对应
        return ctypes.get_last_error()


def _bind(lib, name: str, restype, argtypes):
    fn = getattr(lib, name)
    fn.restype = restype
    fn.argtypes = argtypes
    return fn


_cached: Win32Bindings | None = None


def _build() -> Win32Bindings:
    if sys.platform != "win32":
        # 非 Windows 宿主 fail-closed（上游 koffi 加载 kernel32.dll 失败同位）
        raise OSError(f"windows-acl backend requires win32, got {sys.platform}")
    kernel32 = ctypes.WinDLL("kernel32.dll", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32.dll", use_last_error=True)

    table = Win32Bindings()
    # ---- 进程 / 令牌句柄 ----------------------------------------------------
    table.openProcess = _bind(kernel32, "OpenProcess", HANDLE, [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD])
    table.openProcessToken = _bind(advapi32, "OpenProcessToken", wintypes.BOOL, [HANDLE, wintypes.DWORD, PHANDLE])
    table.closeHandle = _bind(kernel32, "CloseHandle", wintypes.BOOL, [HANDLE])
    # ---- 错误 / 诊断 ---------------------------------------------------------
    table.formatMessageW = _bind(kernel32, "FormatMessageW", wintypes.DWORD, [
        wintypes.DWORD, HANDLE, wintypes.DWORD, wintypes.DWORD, HANDLE, wintypes.DWORD, HANDLE])
    # ---- 内存 -----------------------------------------------------------------
    table.localAlloc = _bind(kernel32, "LocalAlloc", HANDLE, [wintypes.UINT, ctypes.c_size_t])
    table.localFree = _bind(kernel32, "LocalFree", HANDLE, [HANDLE])
    # ---- SID ------------------------------------------------------------------
    table.convertStringSidToSidW = _bind(advapi32, "ConvertStringSidToSidW", wintypes.BOOL, [LPWSTR, PHANDLE])
    table.createWellKnownSid = _bind(advapi32, "CreateWellKnownSid", wintypes.BOOL, [
        wintypes.DWORD, HANDLE, HANDLE, PDWORD])
    table.isValidSid = _bind(advapi32, "IsValidSid", wintypes.BOOL, [HANDLE])
    table.getLengthSid = _bind(advapi32, "GetLengthSid", wintypes.DWORD, [HANDLE])
    table.copySid = _bind(advapi32, "CopySid", wintypes.BOOL, [wintypes.DWORD, HANDLE, HANDLE])
    # ---- 令牌信息 --------------------------------------------------------------
    table.getTokenInformation = _bind(advapi32, "GetTokenInformation", wintypes.BOOL, [
        HANDLE, wintypes.DWORD, HANDLE, wintypes.DWORD, PDWORD])
    table.setTokenInformation = _bind(advapi32, "SetTokenInformation", wintypes.BOOL, [
        HANDLE, wintypes.DWORD, HANDLE, wintypes.DWORD])
    # ---- 受限令牌 ---------------------------------------------------------------
    table.createRestrictedToken = _bind(advapi32, "CreateRestrictedToken", wintypes.BOOL, [
        HANDLE, wintypes.DWORD, wintypes.DWORD, HANDLE, wintypes.DWORD, HANDLE,
        wintypes.DWORD, HANDLE, PHANDLE])
    # ---- ACL 编辑 ----------------------------------------------------------------
    table.setEntriesInAclW = _bind(advapi32, "SetEntriesInAclW", wintypes.DWORD, [
        wintypes.DWORD, HANDLE, HANDLE, PHANDLE])
    table.setNamedSecurityInfoW = _bind(advapi32, "SetNamedSecurityInfoW", wintypes.DWORD, [
        LPCWSTR, wintypes.DWORD, wintypes.DWORD, HANDLE, HANDLE, HANDLE, HANDLE])
    table.getNamedSecurityInfoW = _bind(advapi32, "GetNamedSecurityInfoW", wintypes.DWORD, [
        LPCWSTR, wintypes.DWORD, wintypes.DWORD, PHANDLE, PHANDLE, PHANDLE, PHANDLE, PHANDLE])
    # ---- 环境 / io -----------------------------------------------------------------
    table.getTempPathW = _bind(kernel32, "GetTempPathW", wintypes.DWORD, [wintypes.DWORD, LPWSTR])
    # fileapi.h ~64：HANDLE CreateFileW(LPCWSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE)
    table.createFileW = _bind(kernel32, "CreateFileW", HANDLE, [
        LPCWSTR, wintypes.DWORD, wintypes.DWORD, HANDLE, wintypes.DWORD, wintypes.DWORD, HANDLE])
    # fileapi.h：BOOL LockFileEx(HANDLE, DWORD, DWORD, DWORD, DWORD, LPOVERLAPPED)
    #           BOOL UnlockFileEx(HANDLE, dwReserved, low, high, LPOVERLAPPED) —— 无 dwFlags
    table.lockFileEx = _bind(kernel32, "LockFileEx", wintypes.BOOL, [
        HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, HANDLE])
    table.unlockFileEx = _bind(kernel32, "UnlockFileEx", wintypes.BOOL, [
        HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, HANDLE])
    table.createPipe = _bind(kernel32, "CreatePipe", wintypes.BOOL, [PHANDLE, PHANDLE, HANDLE, wintypes.DWORD])
    table.setHandleInformation = _bind(kernel32, "SetHandleInformation", wintypes.BOOL, [HANDLE, wintypes.DWORD, wintypes.DWORD])
    table.createProcessAsUserW = _bind(advapi32, "CreateProcessAsUserW", wintypes.BOOL, [
        HANDLE, LPCWSTR, LPCWSTR, HANDLE, HANDLE, wintypes.BOOL, wintypes.DWORD,
        HANDLE, LPCWSTR, ctypes.POINTER(STARTUPINFOW), ctypes.POINTER(PROCESS_INFORMATION)])
    table.setEnvironmentVariableW = _bind(kernel32, "SetEnvironmentVariableW", wintypes.BOOL, [LPCWSTR, LPCWSTR])
    table.readFile = _bind(kernel32, "ReadFile", wintypes.BOOL, [HANDLE, HANDLE, wintypes.DWORD, PDWORD, HANDLE])
    table.peekNamedPipe = _bind(kernel32, "PeekNamedPipe", wintypes.BOOL, [
        HANDLE, HANDLE, wintypes.DWORD, PDWORD, PDWORD, PDWORD])
    table.waitForSingleObject = _bind(kernel32, "WaitForSingleObject", wintypes.DWORD, [HANDLE, wintypes.DWORD])
    table.getExitCodeProcess = _bind(kernel32, "GetExitCodeProcess", wintypes.BOOL, [HANDLE, PDWORD])
    table.resumeThread = _bind(kernel32, "ResumeThread", wintypes.DWORD, [HANDLE])
    # ---- job object（runner kill-on-close） ------------------------------------------
    table.createJobObjectW = _bind(kernel32, "CreateJobObjectW", HANDLE, [HANDLE, LPCWSTR])
    table.setInformationJobObject = _bind(kernel32, "SetInformationJobObject", wintypes.BOOL, [
        HANDLE, wintypes.DWORD, HANDLE, wintypes.DWORD])
    table.assignProcessToJobObject = _bind(kernel32, "AssignProcessToJobObject", wintypes.BOOL, [HANDLE, HANDLE])
    # 无法放进 kill-on-close job 的挂起子进程必须先终结——只关句柄会把它永远吊着
    table.terminateProcess = _bind(kernel32, "TerminateProcess", wintypes.BOOL, [HANDLE, wintypes.UINT])
    # ---- 控制台 ------------------------------------------------------------------------
    # HandlerRoutine=NULL + add=1 使本进程忽略 CTRL+C（wincon.h）：runner 要活到
    # 子进程退出之后撤销授权、镜像退出码；子进程自己处理自己的 Ctrl+C。
    table.setConsoleCtrlHandler = _bind(kernel32, "SetConsoleCtrlHandler", wintypes.BOOL, [HANDLE, wintypes.BOOL])
    table.getStdHandle = _bind(kernel32, "GetStdHandle", HANDLE, [wintypes.DWORD])
    return table


def win32_sync() -> Win32Bindings:
    """解析惰性绑定表（首次失败原样抛出，fail-closed）；结果缓存复用。"""
    global _cached
    if _cached is None:
        _cached = _build()
    return _cached


def error_text(api: Win32Bindings, win32_code: int) -> str:
    """FormatMessageW 把错误码翻成可读文本；格式化失败返回 ''。"""
    buffer = ctypes.create_unicode_buffer(1024)
    length = api.formatMessageW(
        abi.FORMAT_MESSAGE_FROM_SYSTEM | abi.FORMAT_MESSAGE_IGNORE_INSERTS,
        None, win32_code, 0, ctypes.addressof(buffer), len(buffer), None)
    if length == 0:
        return ""
    return buffer[:length].strip()


def get_temp_path(api: Win32Bindings) -> str:
    """GetTempPathW 读进程临时目录。防御超长系统临时路径：所需长度（含
    NUL）超过缓冲容量时 GetTempPathW 不写缓冲只报需求长度——此时不得解码。"""
    buffer = ctypes.create_unicode_buffer(abi.MAX_PATH + 1)
    length = api.getTempPathW(len(buffer), buffer)
    if length == 0:
        throw_last_error(api, "GetTempPathW")
    if length > len(buffer):
        raise Win32Error("GetTempPathW", abi.ERROR_INSUFFICIENT_BUFFER,
                         f"required {length} chars exceed the {len(buffer)}-char buffer; nothing was written")
    return buffer[:length]


def throw_last_error(api: Win32Bindings, name: str, detail: str | None = None):
    """BOOL 风格 API 失败抛错；必须在失败调用后立刻调，避免 GetLastError 被
    其它 Win32 调用冲掉。"""
    raise Win32Error(name, api.getLastError(), detail)


def throw_win32(api: Win32Bindings, name: str, win32_code: int, detail: str | None = None):
    """HRESULT 风格 API（返回值即错误码）失败抛错。"""
    raise Win32Error(name, win32_code, detail)
