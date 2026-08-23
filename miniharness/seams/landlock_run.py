"""landlock-run —— Landlock 自限制执行器（ctypes 载体）。

对应 dsh 真实源码：native/landlock-run（~300 行 C11 launcher，npm 预编译分发）。
上游以独立二进制形态存在的原因是 Node 载体无法直调 syscall；Python 经 ctypes
即可调用 Landlock UAPI，因此 mini 以本模块复刻**同一 CLI 契约**
（native/landlock-run/docs/cli-contract.md，逐条对齐）：

  * 语法：``[--ro <path>]... [--rw <path>]... -- <argv>...`` 或 ``--probe``
    （互斥）；无其它旗标、无环境变量输入。
  * ``--ro`` 授予路径下 read+execute；``--rw`` 授予协商 ABI 可治理的全部访问；
    未授予一律拒绝（allow-list）；非目录 grant 只保留文件兼容位
    （``--rw /dev/null`` 即此语义）。
  * 退出码：launcher 级失败一律 125 且绝不 exec；exec 成功后子进程状态原样
    透传——消费者须「125 + ``landlock-run: `` fatal 行」双条件归因。
  * 报告行：probe 成功 stdout 恰一行 ``landlock: fully enforced`` /
    ``landlock: partially enforced (older ABI)``；部分 ABI 下受限运行先打一行
    stderr ``landlock-run: partial enforcement (older Landlock ABI)`` 再继续；
    fatal 一律 ``landlock-run: `` 前缀 + exit 125。
  * 语义：prctl(PR_SET_NO_NEW_PRIVS) → create_ruleset → add_rule(PATH_BENEATH)
    → restrict_self → execvp；规则集跨 execve 继承，调用方自身不受限。

ABI 协商：create_ruleset(NULL, 0, VERSION) 返回内核支持的最高 ABI；
handled_access_fs 按协商结果屏蔽（请求内核不认识的位会 EINVAL）；
full 当且仅当内核 ≥ 本工具已知最高 ABI（5），否则 partial 但仍受限。
syscall 号 444/445/446 为通用统一编号（x86_64/arm64/riscv64/loongarch64 同号），
其它架构 fail-closed 退出 125。非 Linux 宿主同样干净退出 125（探测映射 unusable，
消费链 fail-closed，与上游「无平台包 → 探测失败」的降级设计一致）。

UAPI 常量逐字取自内核头文件（linux/landlock.h + linux/prctl.h），作为审计记录
的一部分内嵌于此（上游 C 源同纪律）。仅依赖 stdlib（ctypes/os/sys）。
"""

import ctypes
import os
import platform
import stat
import sys

# ---------- UAPI（linux/landlock.h） ----------

LANDLOCK_CREATE_RULESET_VERSION = 1 << 0
LANDLOCK_RULE_PATH_BENEATH = 1

# landlock_access_fs 各位与其进入内核的 ABI 版本
_ACCESS_FS_EXECUTE = 1 << 0
_ACCESS_FS_WRITE_FILE = 1 << 1
_ACCESS_FS_READ_FILE = 1 << 2
_ACCESS_FS_READ_DIR = 1 << 3
_ACCESS_FS_REMOVE_DIR = 1 << 4
_ACCESS_FS_REMOVE_FILE = 1 << 5
_ACCESS_FS_MAKE_CHAR = 1 << 6
_ACCESS_FS_MAKE_DIR = 1 << 7
_ACCESS_FS_MAKE_SOCK = 1 << 8
_ACCESS_FS_MAKE_FIFO = 1 << 9
_ACCESS_FS_MAKE_BLOCK = 1 << 10
_ACCESS_FS_MAKE_SYM = 1 << 11
_ACCESS_FS_REFER = 1 << 12       # ABI 2（5.19）
_ACCESS_FS_TRUNCATE = 1 << 13    # ABI 3（6.2）
_ACCESS_FS_IOCTL_DEV = 1 << 14   # ABI 4（6.7）

# 本工具已知最高 ABI（上游 launcher 同为 5；ABI 5 引入 scoping，无新 fs 位）
TOOL_MAX_ABI = 5

# 各 ABI 新增的 fs 访问位（ABI 1 为基线全集）
_ABI_NEW_BITS = {
    2: _ACCESS_FS_REFER,
    3: _ACCESS_FS_TRUNCATE,
    4: _ACCESS_FS_IOCTL_DEV,
}
_BASE_ABI1_BITS = (
    _ACCESS_FS_EXECUTE | _ACCESS_FS_WRITE_FILE | _ACCESS_FS_READ_FILE
    | _ACCESS_FS_READ_DIR | _ACCESS_FS_REMOVE_DIR | _ACCESS_FS_REMOVE_FILE
    | _ACCESS_FS_MAKE_CHAR | _ACCESS_FS_MAKE_DIR | _ACCESS_FS_MAKE_SOCK
    | _ACCESS_FS_MAKE_FIFO | _ACCESS_FS_MAKE_BLOCK | _ACCESS_FS_MAKE_SYM
)

# 仅目录有意义的操作（非目录 grant 必须剔除：cli-contract「文件兼容位」）
_DIR_ONLY_MASK = (
    _ACCESS_FS_READ_DIR | _ACCESS_FS_REMOVE_DIR | _ACCESS_FS_REMOVE_FILE
    | _ACCESS_FS_MAKE_CHAR | _ACCESS_FS_MAKE_DIR | _ACCESS_FS_MAKE_SOCK
    | _ACCESS_FS_MAKE_FIFO | _ACCESS_FS_MAKE_BLOCK | _ACCESS_FS_MAKE_SYM
    | _ACCESS_FS_REFER
)

# --ro 授予：路径下 read + execute（目录含 READ_DIR；文件由兼容位掩码裁剪）
_RO_GRANT_BITS = (
    _ACCESS_FS_EXECUTE | _ACCESS_FS_READ_FILE | _ACCESS_FS_READ_DIR
)

PR_SET_NO_NEW_PRIVS = 38

LAUNCHER_FAILURE_EXIT = 125

PROBE_FULL_LINE = "landlock: fully enforced"
PROBE_PARTIAL_LINE = "landlock: partially enforced (older ABI)"
PARTIAL_RUN_LINE = "landlock-run: partial enforcement (older Landlock ABI)"
FATAL_PREFIX = "landlock-run: "

# 统一 syscall 编号的架构（上游发布矩阵 linux-x64/linux-arm64 及同号新架构）
_UNIFIED_SYSCALL_ARCHS = {"x86_64", "amd64", "aarch64", "arm64", "riscv64", "loongarch64"}
_SYSCALL_CREATE_RULESET = 444
_SYSCALL_ADD_RULE = 445
_SYSCALL_RESTRICT_SELF = 446


def negotiated_fs_bits(abi: int) -> int:
    """协商 ABI 可治理的 handled_access_fs 位集（cumulative）。"""
    bits = _BASE_ABI1_BITS
    for level, bit in sorted(_ABI_NEW_BITS.items()):
        if abi >= level:
            bits |= bit
    return bits


def file_compatible_bits(bits: int, read_only: bool) -> int:
    """非目录 grant 的文件兼容位：剔仅目录操作（cli-contract 第 15 行语义）。"""
    masked = bits & ~_DIR_ONLY_MASK
    if read_only:
        # 文件上的 read grant 不需要 WRITE/TRUNCATE/IOCTL 位
        masked &= ~(_ACCESS_FS_WRITE_FILE | _ACCESS_FS_TRUNCATE | _ACCESS_FS_IOCTL_DEV)
    return masked


class _RulesetAttr(ctypes.Structure):
    """struct landlock_ruleset_attr（mini 只声明 fs 字段：更小 size 表示更老
    ABI 的调用方，内核按设计接受子集）。"""

    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    """struct landlock_path_beneath_attr。"""

    _fields_ = [("allowed_access", ctypes.c_uint64),
                ("parent_fd", ctypes.c_int32)]


class _Landlock:
    """libc syscall 封装；不可用宿主上构造即抛 LauncherUnavailable。"""

    def __init__(self):
        if platform.system() != "Linux":
            raise LauncherUnavailable(f"landlock requires Linux, not {platform.system()}")
        if platform.machine() not in _UNIFIED_SYSCALL_ARCHS:
            raise LauncherUnavailable(
                f"unsupported architecture {platform.machine()} "
                f"(unified landlock syscalls on {_UNIFIED_SYSCALL_ARCHS} only)")
        try:
            self._libc = ctypes.CDLL(None, use_errno=True)
        except OSError as exc:
            raise LauncherUnavailable(f"cannot load libc: {exc}") from exc
        self._syscall = self._libc.syscall
        self._syscall.restype = ctypes.c_long

    def create_ruleset(self, handled_access_fs: int, flags: int = 0) -> int:
        attr = _RulesetAttr(handled_access_fs)
        return self._syscall(_SYSCALL_CREATE_RULESET, ctypes.byref(attr),
                             ctypes.sizeof(attr), flags)

    def kernel_abi(self) -> int:
        """内核支持的最高 Landlock ABI；不支持时抛 LauncherUnavailable。"""
        rc = self.create_ruleset(0, LANDLOCK_CREATE_RULESET_VERSION)
        if rc < 0:
            raise LauncherUnavailable(
                f"kernel does not support Landlock (errno={ctypes.get_errno()})")
        return rc

    def add_rule(self, ruleset_fd: int, allowed_access: int, parent_fd: int) -> None:
        attr = _PathBeneathAttr(allowed_access, parent_fd)
        rc = self._syscall(_SYSCALL_ADD_RULE, ruleset_fd,
                           LANDLOCK_RULE_PATH_BENEATH, ctypes.byref(attr), 0)
        if rc < 0:
            err = ctypes.get_errno()
            raise LauncherUnavailable(f"add_rule failed (errno={err})")

    def restrict_self(self, ruleset_fd: int) -> None:
        if self._libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise LauncherUnavailable(
                f"prctl(PR_SET_NO_NEW_PRIVS) failed (errno={ctypes.get_errno()})")
        rc = self._syscall(_SYSCALL_RESTRICT_SELF, ruleset_fd, 0)
        if rc < 0:
            raise LauncherUnavailable(
                f"restrict_self failed (errno={ctypes.get_errno()})")


class LauncherUnavailable(Exception):
    """launcher 级失败：打印 fatal 行并 exit 125，绝不 exec 目标命令。"""


def fatal(message: str):
    print(FATAL_PREFIX + message, file=sys.stderr)
    raise SystemExit(LAUNCHER_FAILURE_EXIT)


def parse_grant_args(argv: list[str]):
    """解析 CLI 语法：(ro, rw, command) 或 probe 标记；违规即 fatal。"""
    ro: list[str] = []
    rw: list[str] = []
    probe = False
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--probe":
            probe = True
            i += 1
        elif arg in ("--ro", "--rw"):
            if i + 1 >= len(argv):
                fatal(f"{arg} requires a path argument")
            (ro if arg == "--ro" else rw).append(argv[i + 1])
            i += 2
        elif arg == "--":
            command = argv[i + 1:]
            if probe or not command:
                fatal("'--' must be followed by a command argv")
            return ro, rw, command, False
        else:
            fatal(f"unknown argument: {arg}")
    if probe:
        if ro or rw:
            fatal("--probe is mutually exclusive with grants")
        return ro, rw, None, True
    fatal("missing '--' separator before command argv")


def _open_grant_root(path: str) -> int:
    try:
        return os.open(path, os.O_PATH | os.O_CLOEXEC)
    except OSError as exc:
        raise LauncherUnavailable(f"cannot open grant root {path}: {exc}") from exc


def _is_dir(fd: int) -> bool:
    """对已打开的 grant fd 做 S_ISDIR 判定（对齐上游 fstat 语义，无 open 后 TOCTOU）。"""
    try:
        return stat.S_ISDIR(os.fstat(fd).st_mode)
    except OSError as exc:
        raise LauncherUnavailable(f"fstat on grant root failed: {exc}") from exc


def restrict_and_exec(ro: list[str], rw: list[str], command: list[str]) -> None:
    """自限制后 exec：规则安装成功才允许运行目标命令（fail-closed）。"""
    try:
        ll = _Landlock()
        version = ll.kernel_abi()
    except LauncherUnavailable as exc:
        fatal(str(exc))
    if version < TOOL_MAX_ABI:
        # 内核可治理范围低于工具已知最高：部分强制，仍受限继续（契约报告行）
        print(PARTIAL_RUN_LINE, file=sys.stderr)
    abi = min(version, TOOL_MAX_ABI)
    handled = negotiated_fs_bits(min(version, max(_ABI_NEW_BITS, default=1)))
    try:
        fd = ll.create_ruleset(handled)
        if fd < 0:
            fatal(f"create_ruleset failed (errno={ctypes.get_errno()})")
        for path in ro:
            parent = _open_grant_root(path)
            # 目录授予 RO 位全集；文件只保留文件兼容位（cli-contract 第 15 行）
            if _is_dir(parent):
                allowed = _RO_GRANT_BITS & handled
            else:
                allowed = file_compatible_bits(_RO_GRANT_BITS, read_only=True) & handled
            ll.add_rule(fd, allowed, parent)
            os.close(parent)
        for path in rw:
            parent = _open_grant_root(path)
            full = negotiated_fs_bits(abi)
            allowed = full if _is_dir(parent) else full & ~_DIR_ONLY_MASK
            ll.add_rule(fd, allowed & handled, parent)
            os.close(parent)
        ll.restrict_self(fd)
        os.close(fd)
    except LauncherUnavailable as exc:
        fatal(str(exc))
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        fatal(f"cannot exec {command[0]}: {exc}")


def run_probe() -> None:
    """--probe：内核可强制（完整或部分）→ 打印恰一行报告并 exit 0；否则 125。"""
    try:
        ll = _Landlock()
        version = ll.kernel_abi()
    except LauncherUnavailable as exc:
        fatal(str(exc))
    print(PROBE_FULL_LINE if version >= TOOL_MAX_ABI else PROBE_PARTIAL_LINE)


def main(argv: list[str] | None = None) -> None:
    args = list(sys.argv[1:] if argv is None else argv)
    ro, rw, command, probe = parse_grant_args(args)
    if probe:
        run_probe()
        return
    restrict_and_exec(ro, rw, command)


if __name__ == "__main__":
    main()
