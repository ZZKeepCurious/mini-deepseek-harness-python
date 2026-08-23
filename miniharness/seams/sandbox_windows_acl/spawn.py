"""受限进程 spawn（上游 spawn.ts 对应物）：匿名管道 stdio + STARTUPINFOW
(STARTF_USESTDHANDLES) + CreateProcessAsUserW（受限令牌下）+ 异步管道排水 +
退出等待。控制台隔离（CREATE_NO_WINDOW / CREATE_NEW_CONSOLE）**有意缺席**：
该受限方案下隐藏控制台子进程死于 STATUS_DLL_INIT_FAILED（0xC0000142，上游
实证，见 win32_abi 模块注记）。stdio 重定向走管道不受影响；子进程共享宿主
控制台。"""
from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from .errors import Win32Error
from .ffi import (alloc_bytes, alloc_process_info, alloc_ptr_slot, alloc_startup_info,
                  alloc_uint32, decode_process_info, decode_ptr, decode_uint32,
                  encode_startup_info, is_null_ptr, throw_last_error, throw_win32)
from . import win32_abi as abi


def quote_arg(argument: str) -> str:
    """按 CommandLineToArgvW 解析规则给一个参数加引号：反斜杠只在引号字符
    之前加倍——包括本函数追加的收尾引号，因此尾随反斜杠串同样加倍（否则奇数
    反斜杠会把收尾引号逃逸成字面字符，破坏命令行余下部分）。与 Microsoft 为
    命令行参数文档化的 CRT ArgvQuote 行为一致。"""
    if argument == "":
        return '""'
    # 需要加引号的情况：含空白/引号、或以反斜杠结尾（否则收尾引号会被逃逸）
    needs_quote = bool(re.search(r'[\s"]', argument)) or argument.endswith("\\")
    if not needs_quote:
        return argument
    quoted = '"'
    index = 0
    while index < len(argument):
        backslashes = 0
        while index < len(argument) and argument[index] == "\\":
            backslashes += 1
            index += 1
        if index == len(argument):
            # 尾随反斜杠串：加倍以防逃逸收尾引号
            quoted += "\\" * (backslashes * 2)
        elif argument[index] == '"':
            quoted += "\\" * (backslashes * 2 + 1) + '"'
            index += 1
        else:
            quoted += "\\" * backslashes + argument[index]
            index += 1
    return quoted + '"'


def build_command_line(program: str, args: list[str]) -> str:
    """从 program + argv 构建 CreateProcess 解析的单条命令行。"""
    return " ".join(quote_arg(a) for a in [program, *args])


@dataclass
class SpawnedNative:
    """管道 stdio 的受限子进程：进程句柄 + 待排水的管道读端。"""
    pid: int
    process: int
    stdout_read: int
    stderr_read: int


@dataclass
class SpawnedInherited:
    """继承 stdio 的受限子进程：进程句柄 + 其 kill-on-close job。"""
    pid: int
    process: int
    job: int


def _create_pipe(api):
    read_slot = alloc_ptr_slot()
    write_slot = alloc_ptr_slot()
    if api.createPipe(read_slot, write_slot, None, 0) == 0:
        throw_last_error(api, "CreatePipe")
    read = decode_ptr(read_slot)
    write = decode_ptr(write_slot)
    if read is None or write is None:
        throw_last_error(api, "CreatePipe", "null pipe handle")
    return read, write


def _set_inheritable(api, handle: int, label: str) -> None:
    if api.setHandleInformation(handle, abi.HANDLE_FLAG_INHERIT, abi.HANDLE_FLAG_INHERIT) == 0:
        throw_last_error(api, "SetHandleInformation", label)


def spawn_sandboxed(api, token: int, options: dict) -> SpawnedNative:
    """在受限令牌下创建管道 stdio 进程。子进程 stdin 立即关闭（EOF），与 POC
    一致；stdout/stderr 读端交还调用方排水。子进程继承调用方环境块
    （lpEnvironment NULL）；调用方经 SetEnvironmentVariableW 重写自身环境后再
    spawn（runner 的每会话 temp 契约）——koffi 走显式环境块会在
    CreateProcessAsUserW 触发 ERROR_INVALID_PARAMETER（上游实证）；ctypes 同样
    保持 lpEnvironment NULL 形态。"""
    std_in = _create_pipe(api)
    std_out = _create_pipe(api)
    std_err = _create_pipe(api)
    # 每根管道的子进程端必须可继承（POC lines 262-268）
    _set_inheritable(api, std_in[0], "stdin read end")
    _set_inheritable(api, std_out[1], "stdout write end")
    _set_inheritable(api, std_err[1], "stderr write end")

    startup_info = alloc_startup_info()
    encode_startup_info(startup_info, {
        "cb": abi.STARTUPINFOW_SIZE,
        "dwFlags": abi.STARTF_USESTDHANDLES,
        "hStdInput": std_in[0],
        "hStdOutput": std_out[1],
        "hStdError": std_err[1],
    })

    process_info = alloc_process_info()
    command_line = build_command_line(options["command"], options["args"])
    created = api.createProcessAsUserW(
        token, None, command_line,
        None, None,
        1,   # bInheritHandles：重定向必需
        0,   # 无创建旗标：挂起/无窗口变体在该受限方案下不可用
        None, options["cwd"],
        startup_info, process_info,
    )
    # CloseHandle 会冲掉 GetLastError——先捕获失败码，再关掉到此刻为止创建的
    # 全部六根管道句柄（上游 failure-paths.spec 钉死的 six-close 契约）
    if created == 0:
        win32_code = api.getLastError()
        api.closeHandle(std_in[0])
        api.closeHandle(std_in[1])
        api.closeHandle(std_out[0])
        api.closeHandle(std_out[1])
        api.closeHandle(std_err[0])
        api.closeHandle(std_err[1])
        throw_win32(api, "CreateProcessAsUserW", win32_code,
                    f"command: {options['command']}, cwd: {options['cwd']}")

    info = decode_process_info(process_info)
    process_handle = info["hProcess"]
    thread_handle = info["hThread"]
    if process_handle is None or thread_handle is None:
        raise RuntimeError(
            f"CreateProcessAsUserW succeeded but returned null process/thread handles (pid {info['dwProcessId']})")

    # 宿主侧清理：子进程端的句柄已复制进子进程；宿主关掉自己的副本，
    # ReadFile 才能在子进程退出后看到 EOF。
    api.closeHandle(std_in[0])
    api.closeHandle(std_out[1])
    api.closeHandle(std_err[1])
    api.closeHandle(std_in[1])
    api.closeHandle(thread_handle)

    return SpawnedNative(pid=info["dwProcessId"], process=process_handle,
                         stdout_read=std_out[0], stderr_read=std_err[0])


async def drain_pipe(api, handle: int) -> bytes:
    """经 PeekNamedPipe 非阻塞轮询把一根管道读端排干成字节。"""
    chunks: list[bytes] = []
    while True:
        bytes_read_slot = alloc_uint32()
        total_avail_slot = alloc_uint32()
        left_this_message_slot = alloc_uint32()
        peeked = api.peekNamedPipe(handle, None, 0, bytes_read_slot, total_avail_slot, left_this_message_slot)
        if peeked == 0:
            win32_code = api.getLastError()
            if win32_code in (abi.ERROR_BROKEN_PIPE, abi.ERROR_NO_DATA):
                break  # 子进程已关自己那端：干净 EOF
            raise Win32Error("PeekNamedPipe", win32_code, f"drain failure after {len(chunks)} chunk(s)")
        available = decode_uint32(total_avail_slot)
        if available > 0:
            chunk = alloc_bytes(available)
            read_slot = alloc_uint32()
            if api.readFile(handle, chunk, len(chunk), read_slot, None) == 0:
                raise Win32Error("ReadFile", api.getLastError(), f"drain failure after {len(chunks)} chunk(s)")
            chunks.append(bytes(chunk[:decode_uint32(read_slot)]))
        # 小退避替代忙等：子进程无输出时全速轮询管道只会空转事件循环
        await asyncio.sleep(0.001)
    api.closeHandle(handle)
    return b"".join(chunks)


def wait_for_exit(api, process: int) -> int:
    """等待进程退出并返回退出码。只在两路排水都完成后调用——排水在子进程
    关闭管道端后才结束，即子进程此时已退出，此等待立即返回。提前调用会阻塞
    线程、饿死排水（POC 注释警告的管道缓冲死锁）。"""
    wait_result = api.waitForSingleObject(process, abi.INFINITE)
    if wait_result == 0xFFFFFFFF:
        throw_last_error(api, "WaitForSingleObject")
    exit_code_slot = alloc_uint32()
    if api.getExitCodeProcess(process, exit_code_slot) == 0:
        throw_last_error(api, "GetExitCodeProcess")
    api.closeHandle(process)
    return decode_uint32(exit_code_slot)


def create_kill_on_close_job(api) -> int:
    """创建 kill-on-close job object（JOBOBJECT_EXTENDED_LIMIT_INFORMATION 的
    LimitFlags@16 写 JOB_OBJECT_LIMIT_KILL_ON_CLOSE，布局经 abi-probe 验证）。
    调用方带着打开的 job 句柄死去时，Windows 终结 job 内每个进程——孤儿子
    进程兜底。返回的句柄由调用方持有至子进程生命周期结束。"""
    job = api.createJobObjectW(None, None)
    if is_null_ptr(job):
        throw_last_error(api, "CreateJobObjectW")
    information = bytearray(abi.JOBOBJECT_EXTENDED_LIMIT_SIZE)
    information[abi.JOBOBJECT_EXTENDED_LIMIT_FLAGS_OFFSET:abi.JOBOBJECT_EXTENDED_LIMIT_FLAGS_OFFSET + 4] = \
        int(abi.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE).to_bytes(4, "little")
    if api.setInformationJobObject(job, abi.JobObjectExtendedLimitInformation,
                                   bytes(information), len(information)) == 0:
        win32_code = api.getLastError()
        api.closeHandle(job)
        throw_win32(api, "SetInformationJobObject", win32_code)
    return job


def spawn_sandboxed_inherited(api, token: int, options: dict) -> SpawnedInherited:
    """在受限令牌下创建 stdio 直通调用方管道的进程——runner 形态：宿主以
    管道 stdio spawn runner，runner 的受限子进程写同一批管道。

    Node 启动时清掉自己 stdio 句柄的可继承位（uv_disable_stdio_inheritance）；
    Python 无此行为但保持同一显式形态：spawn 窗口内重开继承位并经
    STARTF_USESTDHANDLES 显式传入——否则子进程收到 INVALID 标准句柄
    （"The handle is invalid"，上游硬教训）。子进程以挂起态启动，使其能先
    入 kill-on-close job 再执行任何代码。"""
    job = create_kill_on_close_job(api)
    std_in = api.getStdHandle(abi.STD_INPUT_HANDLE)
    std_out = api.getStdHandle(abi.STD_OUTPUT_HANDLE)
    std_err = api.getStdHandle(abi.STD_ERROR_HANDLE)
    if is_null_ptr(std_in) or is_null_ptr(std_out) or is_null_ptr(std_err):
        api.closeHandle(job)
        throw_last_error(api, "GetStdHandle", "null standard handle")

    def make_inheritable(handle: int, label: str) -> None:
        if api.setHandleInformation(handle, abi.HANDLE_FLAG_INHERIT, abi.HANDLE_FLAG_INHERIT) == 0:
            throw_last_error(api, "SetHandleInformation", f"{label} (enable inherit)")

    def restore_inherit(handle: int) -> None:
        # best-effort 卫生：runner 不再 spawn 别的东西；此处失败不得掩盖
        # 子进程结果，因此有意不检查
        api.setHandleInformation(handle, abi.HANDLE_FLAG_INHERIT, 0)

    make_inheritable(std_in, "stdin")
    make_inheritable(std_out, "stdout")
    make_inheritable(std_err, "stderr")

    startup_info = alloc_startup_info()
    encode_startup_info(startup_info, {
        "cb": abi.STARTUPINFOW_SIZE,
        "dwFlags": abi.STARTF_USESTDHANDLES,
        "hStdInput": std_in,
        "hStdOutput": std_out,
        "hStdError": std_err,
    })

    process_info = alloc_process_info()
    command_line = build_command_line(options["command"], options["args"])
    created = api.createProcessAsUserW(
        token, None, command_line,
        None, None,
        1,                    # bInheritHandles：重开的 std 句柄必须可继承
        abi.CREATE_SUSPENDED,  # 挂起启动：job 指派先于任何执行
        None, options["cwd"],
        startup_info, process_info,
    )
    restore_inherit(std_in)
    restore_inherit(std_out)
    restore_inherit(std_err)
    if created == 0:
        win32_code = api.getLastError()
        api.closeHandle(job)
        throw_win32(api, "CreateProcessAsUserW", win32_code,
                    f"command: {options['command']}, cwd: {options['cwd']}")

    info = decode_process_info(process_info)
    process_handle = info["hProcess"]
    thread_handle = info["hThread"]
    if process_handle is None or thread_handle is None:
        api.closeHandle(job)
        raise RuntimeError(
            f"CreateProcessAsUserW succeeded but returned null process/thread handles (pid {info['dwProcessId']})")

    if api.assignProcessToJobObject(job, process_handle) == 0:
        # 子进程以挂起态创建且**不在** kill-on-close job 里：只关句柄会把它
        # 永远吊着。先终结，再丢句柄并抛错。
        win32_code = api.getLastError()
        api.terminateProcess(process_handle, 1)
        api.closeHandle(thread_handle)
        api.closeHandle(process_handle)
        api.closeHandle(job)
        throw_win32(api, "AssignProcessToJobObject", win32_code, f"pid {info['dwProcessId']}")
    if api.resumeThread(thread_handle) == 0xFFFFFFFF:
        # 关 job 即触发 kill-on-close：挂起的子进程随之死亡而不是悬到本进程
        # 退出；进程/线程句柄也必须一起走。
        win32_code = api.getLastError()
        api.closeHandle(thread_handle)
        api.closeHandle(process_handle)
        api.closeHandle(job)
        throw_win32(api, "ResumeThread", win32_code, f"pid {info['dwProcessId']}")
    api.closeHandle(thread_handle)

    return SpawnedInherited(pid=info["dwProcessId"], process=process_handle, job=job)
