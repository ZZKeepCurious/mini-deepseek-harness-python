"""Windows ACL 沙箱后端的 ABI 常量（上游 win32-abi.ts 逐值对应物）。

每个取值都经上游 MinGW 头文件核实并由 verify/abi-probe.cpp 运行时复核
（x64 布局断言通过）；本文件保持同一数值，注释保留关键语义出处。

有意排除 POC 的两件事（上游在 Win11 26200 上实证）：
  * S-1-2-1（console logon SID）：CreateWellKnownSid(WinLocalLogonSid) 在
    此环境报 ERROR_INVALID_PARAMETER(87) 留下垃圾 SID 使
    CreateRestrictedToken 报 ERROR_INVALID_SID(1337)；改用正确的
    WinConsoleLogonSid 能拿到合法 S-1-2-1，但子进程一旦配
    CREATE_NO_WINDOW / CREATE_NEW_CONSOLE 就死于 STATUS_DLL_INIT_FAILED
    （0xC0000142）。
  * 控制台隔离：该受限方案下隐藏控制台不可得，子进程共享宿主控制台
    （stdio 重定向走管道不受影响）。
"""
from __future__ import annotations

# ---- winnt.h ---------------------------------------------------------------

# TOKEN_* 访问权
TOKEN_ASSIGN_PRIMARY = 0x0001   # CreateProcessAsUser 以令牌建进程所需
TOKEN_DUPLICATE = 0x0002        # DuplicateTokenEx 所需
TOKEN_QUERY = 0x0008            # GetTokenInformation 读令牌信息所需
TOKEN_ADJUST_DEFAULT = 0x0080   # 改令牌缺省 DACL 所需

# SID_AND_ATTRIBUTES.Attributes：SE_GROUP_LOGON_ID（位 31 置位，有符号比较须转无符号）
SE_GROUP_LOGON_ID = 0xC0000000

# 文件泛写族（winnt.h ~5893-5913）
STANDARD_RIGHTS_WRITE = 0x00020000  # == READ_CONTROL
FILE_GENERIC_WRITE = 0x00120116
DELETE = 0x00010000                 # 删除/改名对象
FILE_DELETE_CHILD = 0x0040          # 删除/改名目录子项

# GRANT_MASK = FILE_GENERIC_WRITE - READ_CONTROL + DELETE + FILE_DELETE_CHILD
# （Explorer/icacls 显示 "Modify"）。WRITE_DAC/WRITE_OWNER 有意**不授**：
# 授了子进程就能夺权或重写 DACL 逃出 allowlist（安全边界）。
GRANT_MASK = (FILE_GENERIC_WRITE | DELETE | FILE_DELETE_CHILD) & ~STANDARD_RIGHTS_WRITE  # 0x00110156

# FILE_ALL_ACCESS（STANDARD_RIGHTS_REQUIRED | SYNCHRONIZE | 0x1FF）：合并进
# 受限令牌缺省 DACL 的 ACE 掩码——令牌持有者对它新建的每个对象（含管道）
# 必须保住全访问，且 ACE 须指名 restricting SID 才能过创建时的写 pass-2。
FILE_ALL_ACCESS = 0x1F01FF

# CreateRestrictedToken 旗标
DISABLE_MAX_PRIVILEGE = 0x1   # 剥最高权限提升，防子进程提权
LUA_TOKEN = 0x4               # 受限用户（过滤管理员）令牌
WRITE_RESTRICTED = 0x8        # 写访问与 restricting SIDs 的 ACL 授权取交——沙箱核心机制

# WELL_KNOWN_SID_TYPE：WinWorldSid = S-1-1-0（Everyone），受限令牌唯一用到的
# 内建 SID（keep-alive 组，见 token.py）
WinWorldSid = 1

# TOKEN_INFORMATION_CLASS
TokenGroups = 2        # 令牌组 SID 列表
TokenDefaultDacl = 6   # 缺省 DACL——未带显式 SD 的新对象继承的 DACL

# SECURITY_INFORMATION
DACL_SECURITY_INFORMATION = 0x00000004

# PROCESS 访问权
PROCESS_QUERY_INFORMATION = 0x0400

# ---- accctrl.h -------------------------------------------------------------

SE_FILE_OBJECT = 1          # trustee 路径指向文件系统对象
TRUSTEE_IS_UNKNOWN = 0      # TRUSTEE_TYPE 未知（形态看 TrusteeForm）
TRUSTEE_IS_SID = 0          # Trustee.ptstrName 是 SID 指针
NO_MULTIPLE_TRUSTEE = 0     # Trustee.pMultipleTrustee 为空

GRANT_ACCESS = 1            # SetEntriesInAclW 加为 allow ACE
REVOKE_ACCESS = 4           # SetEntriesInAclW 移除匹配的 allow ACE

# grfInheritance：ACE 适用目录本身 + 子目录 + 文件（OI|CI）
SUB_CONTAINERS_AND_OBJECTS_INHERIT = 0x3

# ---- winbase.h -------------------------------------------------------------

STARTF_USESTDHANDLES = 0x00000100  # 子进程使用 hStd* 句柄
HANDLE_FLAG_INHERIT = 0x1          # SetHandleInformation 重开句柄可继承位
INFINITE = 0xFFFFFFFF              # 无限等待
MAX_PATH = 260                     # 传统路径长度界
CREATE_SUSPENDED = 0x4             # 主线程挂起启动（先入 kill-on-close job 再跑）

STD_INPUT_HANDLE = -10   # GetStdHandle 选择子
STD_OUTPUT_HANDLE = -11
STD_ERROR_HANDLE = -12

FORMAT_MESSAGE_FROM_SYSTEM = 0x00001000
FORMAT_MESSAGE_IGNORE_INSERTS = 0x00000200

# ---- 错误码 -----------------------------------------------------------------

ERROR_SUCCESS = 0
ERROR_INSUFFICIENT_BUFFER = 122   # 尺寸探测调用成功但需要更大缓冲
ERROR_BROKEN_PIPE = 109           # 管道对端已关闭
ERROR_NO_DATA = 232               # 管道正在关闭
ERROR_LOCK_VIOLATION = 33         # 字节区间锁冲突

# ---- 锁文件（fileapi.h / minwinbase.h / winnt.h） ---------------------------

GENERIC_READ = 0x80000000
GENERIC_WRITE = 0x40000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
# FILE_SHARE_DELETE 有意不用：锁中的文件若可被删重建，两进程会各持"同名锁"
FILE_SHARE_DELETE = 0x00000004
OPEN_ALWAYS = 4                   # 不存在则建，存在则开
LOCKFILE_EXCLUSIVE_LOCK = 0x2     # 排他字节区间锁
LOCKFILE_FAIL_IMMEDIATELY = 0x1   # 冲突即报 ERROR_LOCK_VIOLATION 不等待

# ACE_HEADER.AceType
ACCESS_ALLOWED_ACE_TYPE = 0

SID_MAX_SUB_AUTHORITIES = 15

# ACE_HEADER.AceFlags：读 DACL 时显示的继承位不属于本模块的显式编辑面
INHERITED_ACE = 0x10

# ---- job object（winnt.h ~4859-4866, ~5138, ~5190-5199） ---------------------

JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000  # 孤儿子进程兜底
JobObjectExtendedLimitInformation = 9
JOBOBJECT_EXTENDED_LIMIT_SIZE = 144                # abi-probe 验证
JOBOBJECT_EXTENDED_LIMIT_FLAGS_OFFSET = 16         # LimitFlags 字段偏移（abi-probe 验证）

# ---- ABI 布局（verify/abi-probe.cpp x64 验证） --------------------------------

SECURITY_MAX_SID_SIZE = 68     # SID 最大字节数
SID_AND_ATTRIBUTES_SIZE = 16   # { PSID Sid @0 (8); DWORD Attributes @8 (4) } + pad
TOKEN_GROUPS_OFFSET = 8        # Groups[] 起点（GroupCount @0 + 对齐）
EXPLICIT_ACCESS_W_SIZE = 48    # perms@0 mode@4 inheritance@8 Trustee@16
TRUSTEE_W_OFFSET = 16
TRUSTEE_W_PTSTRNAME_OFFSET = 24  # TRUSTEE_W 内偏移（=> EXPLICIT_ACCESS_W 内 40）
STARTUPINFOW_SIZE = 104        # abi-probe 验证
PROCESS_INFORMATION_SIZE = 24  # abi-probe 验证
