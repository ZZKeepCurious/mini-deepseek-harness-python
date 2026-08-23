"""Windows ACL 写限制沙箱后端（上游 index.ts / AclSandbox 对应物）。

镜像 github.com/huoyaoyuan/windows-acl-restrict-poc @ 10e4dfb（固定修订版）
的机制：WRITE_RESTRICTED 令牌，其 restricting SIDs 含独立的工作区与 temp 写
SID，本沙箱把这两个 SID 加进各自所属目录的 DACL——交集检查于是**恰好**在
任一能力持有 Write ACE 的地方允许写（该检查同时继承其余 restricting SIDs 的
ambient 写 ACE——keep-alive 组 logon SID + Everyone；Authenticated Users、
INTERACTIVE、LOCAL 不进两个列表，完整边界见 seam 的双列表契约与包 README）。
写 SID 是每**工作区**身份（workspace_sid.workspace_write_sid）：从规范工作区
路径确定性派生，工作区根 ACE 因此每机器每工作区只物化一次，后续 provision
命中 exact-ACE 跳过。每个私有 temp 目录拿自己的 SID：共享工作区的兄弟会话
进不了彼此的 temp 树。与 POC 不同，每个 API 失败都带 API 名与精确 Win32 错误
码抛出；子进程**绝不**裸放。

已知边界（受限令牌固有，非本移植引入）：
  * 只限写访问；读、网络、进程可见性不受限（WRITE_RESTRICTED 只取交写访问）；
  * 控制台隔离不可得——子进程共享宿主控制台（CREATE_NO_WINDOW /
    CREATE_NEW_CONSOLE 的子进程死于 STATUS_DLL_INIT_FAILED）；
  * 私有 temp 目录与每个可写目录必须由调用方所有（owner 隐含 WRITE_DAC）；
  * 授权是真实目录上的常驻 ACE 变更。WORKSPACE 授权有意**永不撤销**——ACE
    就是跨会话复用缓存（撤销会逼下一会话重传播全树）；TEMP 授权可撤销：
    dispose() 摘除它们，使可继承 ACE 绝不比其会话的 temp 目录活得久。
    ambient temp 根绝不隐式授权。``manage_dacls=False`` 时 DACL 归**调用方**
    （seam 的授权复用）：init()/dispose() 完全跳过 grant/revoke。

载体差异（语义不变）：init/dispose 同步（上游 async 只是 win32() Promise 包装
的形态产物，底层 koffi 调用本就是同步的）；管道 stdio 的排水任务在 spawn()
时刻经 asyncio.ensure_future 启动（对齐上游「spawn 即排水」的死锁规避），
因此管道形态要求运行中的事件循环——runner 走 inherit 形态不受影响。
"""
from __future__ import annotations

import asyncio
import os
import stat

from .acl import grant_write, revoke_write
from .errors import AggregateError, Win32Error
from .ffi import alloc_ptr_slot, decode_ptr, is_null_ptr, ptr_address, throw_last_error, win32_sync
from .path_boundary import assert_private_temp_disjoint
from .spawn import drain_pipe, spawn_sandboxed, spawn_sandboxed_inherited, wait_for_exit
from .token import (RestrictingSidSet, create_restricted_token, find_logon_sid,
                    make_well_known_sid, open_current_process_token,
                    set_token_default_dacl_grant)
from . import win32_abi as abi


class _Unset:
    """temp_dir 三态哨兵：未传（UNSET）/ 显式禁用（None）/ 显式路径（str）。"""

    def __repr__(self) -> str:  # pragma: no cover - 仅诊断
        return "UNSET"


UNSET = _Unset()


class AclSandboxOptions:
    """构造选项：workspace/temp 允许列表与其独立 SID 身份。"""

    def __init__(self, writable_dirs: list[str], mode: str,
                 temp_dir: str | None | _Unset = UNSET,
                 write_sid: str | None = None,
                 temp_write_sid: str | None = None,
                 manage_dacls: bool | None = None):
        self.writable_dirs = writable_dirs
        self.mode = mode
        self.temp_dir = temp_dir
        self.write_sid = write_sid
        self.temp_write_sid = temp_write_sid
        self.manage_dacls = True if manage_dacls is None else manage_dacls


class AclSandboxSpawnOptions:
    """逐次 spawn 选项：程序、argv/cwd 与 stdio 形态（'pipe' 缺省 / 'inherit'
    —— runner 用法：字节直通，结果里 stdout/stderr 为空）。"""

    def __init__(self, command: str, args: list[str] | None = None,
                 cwd: str | None = None, stdio: str = "pipe"):
        self.command = command
        self.args = args if args is not None else []
        self.cwd = cwd
        self.stdio = stdio


class AclSandboxChildResult:
    """已结算的受限子进程：捕获的 stdio 与退出码。"""

    def __init__(self, stdout: bytes, stderr: bytes, exit_code: int):
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code


def _free_sid_best_effort(api, sid_addr, label: str, failures: list) -> None:
    """释放一个可选 SID，失败保留进列表供 best-effort 兄弟清理继续。"""
    if sid_addr is None:
        return
    try:
        freed = api.localFree(sid_addr)
        if not is_null_ptr(freed):
            throw_last_error(api, "LocalFree", label)
    except Exception as error:  # noqa: BLE001 - 聚合清理失败
        failures.append(error)


class _InheritedChild:
    """inherit 形态的运行中子进程：pid + 结算协程（退出后关 job 句柄；
    重复 wait 复用同一次等待结果——句柄只关一次）。"""

    def __init__(self, api, native):
        self._api = api
        self._native = native
        self.pid = native.pid
        self._settled = False

    async def wait(self) -> AclSandboxChildResult:
        if not self._settled:
            self._exit_code = wait_for_exit(self._api, self._native.process)
            self._settled = True
        if self._api.closeHandle(self._native.job) == 0:
            throw_last_error(self._api, "CloseHandle", "kill-on-close job")
        return AclSandboxChildResult(b"", b"", self._exit_code)


class _PipeChild:
    """pipe 形态的运行中子进程：pid + 结算协程（排水在 spawn 时已启动）。"""

    def __init__(self, api, native, stdout_task, stderr_task):
        self._api = api
        self._native = native
        self._stdout_task = stdout_task
        self._stderr_task = stderr_task
        self.pid = native.pid

    async def wait(self) -> AclSandboxChildResult:
        # waitForExit 有意不在此前启动：WaitForSingleObject 阻塞线程，子进程
        # 还在跑时会饿死排水（管道缓冲死锁）。排水只在子进程关闭管道端后才
        # resolve——那时等待立即返回。
        state = getattr(self, "_exit", None)
        if state is None:
            state = self._exit = wait_for_exit(self._api, self._native.process)
        stdout_buffer = await self._stdout_task
        stderr_buffer = await self._stderr_task
        return AclSandboxChildResult(stdout_buffer, stderr_buffer, state)


class AclSandbox:
    """一个写受限沙箱实例：令牌 + 写 SID 授权 + spawn。``init()`` fail-closed
    ——任何 Win32 失败都会撤销可撤销（temp）授权并抛错；``dispose()`` 撤销
    temp 授权、常驻 workspace ACE 原位保留（跨实例复用缓存）、释放全部分配并
    上报每个清理失败。``manage_dacls=False`` 时授权归调用方（seam 的复用流）：
    init() 一个不加、dispose() 一个不撤。所有子进程退出之后才可调 dispose()
    ——活着的子进程下撤销授权等于抽走它剩余的写许可。"""

    def __init__(self, options: AclSandboxOptions):
        self.mode = options.mode
        self.manage_dacls = options.manage_dacls
        resolved_dirs = []
        for directory in options.writable_dirs:
            absolute = os.path.abspath(directory)
            if not os.path.exists(absolute) or not stat.S_ISDIR(os.stat(absolute).st_mode):
                raise RuntimeError(f"AclSandbox writable dir does not exist or is not a directory: {absolute}")
            resolved_dirs.append(absolute)
        self.writable_dirs = resolved_dirs
        self._temp_dir_option = options.temp_dir
        self.write_sid = options.write_sid
        self.temp_write_sid = options.temp_write_sid
        if self.mode == "workspace-write" and self.write_sid is None:
            raise RuntimeError(
                "AclSandbox workspace-write requires a write SID — derive it from the workspace via workspaceWriteSid()")
        if self.mode == "workspace-write" and self._temp_dir_option is UNSET:
            raise RuntimeError("AclSandbox workspace-write requires an explicit private temp directory or null")
        if self.mode == "read-only" and self._temp_dir_option is not UNSET and self._temp_dir_option is not None:
            raise RuntimeError("AclSandbox read-only does not accept a temp directory")
        if self.mode == "read-only" and (self.write_sid is not None or self.temp_write_sid is not None):
            raise RuntimeError("AclSandbox read-only does not accept write SIDs")
        if (self.mode == "workspace-write" and self._temp_dir_option is not None
                and self._temp_dir_option is not UNSET and self.temp_write_sid is None):
            raise RuntimeError(
                "AclSandbox workspace-write with temp requires a temp write SID — derive it via tempWriteSid()")
        if self._temp_dir_option is None and self.temp_write_sid is not None:
            raise RuntimeError("AclSandbox temp write SID requires a temp directory")
        if self.write_sid is not None and self.temp_write_sid == self.write_sid:
            raise RuntimeError("AclSandbox workspace and temp write SIDs must be distinct")

        self._api = None
        self._token = None
        self._write_sid_addr = None
        self._temp_write_sid_addr = None
        self._granted_paths: list[dict] = []   # {path, sid_addr}
        self.temp_dir_resolved = None

    @property
    def temp_dir(self):
        """已决议 temp 目录（init 后可用；禁用 temp 授权时为 None）。"""
        return self.temp_dir_resolved

    def init(self) -> None:
        """创建受限令牌并应用能力 SID 授权。每实例恰一次（幂等不安全）。"""
        if self._api is not None:
            raise RuntimeError("AclSandbox is already initialized")
        api = win32_sync()
        current_token = open_current_process_token(api)
        current_token_open = True
        restricted_token = None
        try:
            def parse_sid(sid: str) -> int:
                sid_slot = alloc_ptr_slot()
                if api.convertStringSidToSidW(sid, sid_slot) == 0:
                    throw_last_error(api, "ConvertStringSidToSidW", sid)
                parsed = decode_ptr(sid_slot)
                if parsed is None:
                    raise Win32Error("ConvertStringSidToSidW", api.getLastError(), sid)
                return parsed

            self._write_sid_addr = parse_sid(self.write_sid) if self.write_sid is not None else None
            self._temp_write_sid_addr = parse_sid(self.temp_write_sid) if self.temp_write_sid is not None else None

            temp_dir = None if (self.mode == "read-only"
                                or self._temp_dir_option in (None, UNSET)) else self._temp_dir_option
            if self.mode == "workspace-write" and self._temp_dir_option is UNSET:
                raise RuntimeError("AclSandbox workspace-write temp directory was not resolved")
            if temp_dir is not None:
                if not os.path.exists(temp_dir) or not stat.S_ISDIR(os.stat(temp_dir).st_mode):
                    raise RuntimeError(f"AclSandbox temp dir does not exist or is not a directory: {temp_dir}")
                assert_private_temp_disjoint(self.writable_dirs, temp_dir)
            self.temp_dir_resolved = temp_dir

            # manage_dacls=False：调用方（seam 的 grant）已物化 ACE；本实例既不加也不删。
            # 自己持 DACL 时：writableDir ACE **常驻**（每工作区复用缓存——dispose 永不撤销，
            # 否则下次 provision 重传播整树），temp ACE **可撤销**（dispose 在私有目录删除前
            # 摘除；ambient temp 根绝不授权）。
            if self.manage_dacls and self._write_sid_addr is not None:
                for path in self.writable_dirs:
                    grant_write(api, path, self._write_sid_addr)
                if temp_dir is not None and self._temp_write_sid_addr is not None:
                    # 先记录后授权：grantWrite 可能在应用成功后抛错（LocalFree
                    # 失败），fail-closed 清理仍须撤到那条路径（撤未授权路径
                    # 是 no-op 合并）。
                    self._granted_paths.append({"path": temp_dir, "sid_addr": self._temp_write_sid_addr})
                    grant_write(api, temp_dir, self._temp_write_sid_addr)
            logon_sid = find_logon_sid(api, current_token)
            world_sid = make_well_known_sid(api, abi.WinWorldSid)
            write_sids = [s for s in (self._write_sid_addr, self._temp_write_sid_addr) if s is not None]
            restricted_token = create_restricted_token(
                api, current_token, logon_sid, write_sids,
                RestrictingSidSet(world=world_sid),
                self.mode,
            )
            self._token = restricted_token
            # 受限令牌缺省 DACL 只指名用户的 ambient SIDs——没有任何 restricting
            # SID。受限进程新建的每个对象（匿名 stdio 管道、同步对象）的 DACL 都
            # 取自那里，写 pass-2 会拒绝管道创建（ERROR_ACCESS_DENIED；Node 报
            # EPERM），一切管道 stdio 孙进程 spawn 全断。为 restricting SID（有
            # 私有 temp SID 用之，否则工作区 SID，read-only 用 Everyone）合并一个
            # 全访问 ACE：新对象创建仍由父对象 DACL 把关，而新对象自身 DACL 能过
            # pass-2。选 temp SID 是防止一会话 temp 树里的缺省 DACL 对象沾上共享
            # 工作区能力。
            default_grant_sid = self._temp_write_sid_addr or self._write_sid_addr
            if default_grant_sid is None:
                default_grant_sid = ptr_address(world_sid)
            set_token_default_dacl_grant(api, restricted_token, default_grant_sid)
            if api.closeHandle(current_token) == 0:
                throw_last_error(api, "CloseHandle", "current process token")
            current_token_open = False
            self._api = api
        except BaseException as error:
            # fail-closed 清理：失败的 init 绝不留可撤销（temp）授权或 SID 分配。
            # 常驻 workspace ACE **不**撤销——那是预期终态（复用缓存），不是错误残留。
            cleanup_failures: list[BaseException] = []
            if current_token_open and api.closeHandle(current_token) == 0:
                cleanup_failures.append(Win32Error("CloseHandle", api.getLastError(),
                                                   "current process token after init failure"))
            if restricted_token is not None and api.closeHandle(restricted_token) == 0:
                cleanup_failures.append(Win32Error("CloseHandle", api.getLastError(),
                                                   "restricted token after init failure"))
            for grant in self._granted_paths:
                try:
                    revoke_write(api, grant["path"], grant["sid_addr"])
                except Exception as cleanup_error:  # noqa: BLE001 - 聚合
                    cleanup_failures.append(cleanup_error)
            for label, sid_addr in (("workspace write SID", self._write_sid_addr),
                                    ("temp write SID", self._temp_write_sid_addr)):
                _free_sid_best_effort(api, sid_addr, label, cleanup_failures)
            self._token = None
            self._write_sid_addr = None
            self._temp_write_sid_addr = None
            self.temp_dir_resolved = None
            self._granted_paths = []
            if cleanup_failures:
                raise AggregateError(
                    [error, *cleanup_failures],
                    f"AclSandbox init failed and {len(cleanup_failures)} cleanup operation(s) also failed",
                ) from error
            raise

    def spawn(self, options: AclSandboxSpawnOptions):
        """在受限令牌下 spawn 进程。fail-closed：任何 Win32 失败都抛；子进程
        绝不以未受限形态创建。``stdio='inherit'`` 子进程直接共享调用方 stdio，
        并入 kill-on-close job（随调用方死亡），结果的 stdout/stderr 为空。"""
        api = self._api
        token = self._token
        if api is None or token is None:
            raise RuntimeError("AclSandbox is not initialized: call init() first")
        args = list(options.args)
        cwd = options.cwd if options.cwd is not None else os.getcwd()

        if options.stdio == "inherit":
            return _InheritedChild(api, spawn_sandboxed_inherited(
                api, token, {"command": options.command, "args": args, "cwd": cwd}))

        native = spawn_sandboxed(api, token, {"command": options.command, "args": args, "cwd": cwd})
        stdout_task = asyncio.ensure_future(drain_pipe(api, native.stdout_read))
        stderr_task = asyncio.ensure_future(drain_pipe(api, native.stderr_read))
        return _PipeChild(api, native, stdout_task, stderr_task)

    def dispose(self) -> None:
        """撤销可撤销（temp）授权、释放 SID、关闭令牌；常驻 workspace ACE
        保留（复用缓存）。上报每个清理失败。"""
        api = self._api
        if api is None:
            return
        failures: list[BaseException] = []
        if self.manage_dacls:
            for grant in self._granted_paths:
                try:
                    revoke_write(api, grant["path"], grant["sid_addr"])
                except Exception as error:  # noqa: BLE001 - 聚合
                    failures.append(error)
        for label, sid_addr in (("workspace write SID", self._write_sid_addr),
                                ("temp write SID", self._temp_write_sid_addr)):
            _free_sid_best_effort(api, sid_addr, label, failures)
        token = self._token
        if token is not None:
            try:
                if api.closeHandle(token) == 0:
                    throw_last_error(api, "CloseHandle", "restricted token")
            except Exception as error:  # noqa: BLE001 - 聚合清理失败
                failures.append(error)
        self._api = None
        self._token = None
        self._write_sid_addr = None
        self._temp_write_sid_addr = None
        self._granted_paths = []
        if failures:
            raise AggregateError(
                failures, f"AclSandbox dispose completed with {len(failures)} cleanup failure(s)")
