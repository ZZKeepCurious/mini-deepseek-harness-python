"""Windows ACL 写限制沙箱后端包：ctypes FFI 物化上游
windows-acl-restrict-poc @ 10e4dfb 的 WRITE_RESTRICTED 机制。

模块划分（对应上游单文件 index.ts 内的分区）：
  * ``win32_abi`` —— 常量与 API 名（逐字对齐上游 win32-abi.ts）
  * ``ffi``      —— ctypes 绑定表 + 句柄/指针/错误辅助（替代 koffi）
  * ``errors``   —— Win32Error（API 名 + Win32 码）与 AggregateError 垫片
  * ``acl``      —— DACL 授权（grantWrite/revokeWrite，exact-ACE 幂等）
  * ``token``    —— 受限令牌（logon SID 发现、双 restricting 列表、缺省 DACL 授予）
  * ``workspace_sid`` —— 每工作区/temp 能力 SID 确定性派生
  * ``path_boundary`` —— temp 根 ⊄ workspace 断言 / 私有 temp 不相交断言
  * ``grant``    —— 跨进程复用的授权句柄（provider 缓存层）
  * ``spawn``    —— CreateProcessAsUserW（管道/inherit 两形态、six-close 契约）
  * ``index``    —— AclSandbox 实例编排（init fail-closed 清理 / dispose 可撤销撤销）
  * ``runner``   —— argv 包装 CLI（稳定 argv 契约、exit 127 失败签名）

公开面：AclSandbox 与其选项/spawn 选项/子进程结果、workspace_write_sid/
temp_write_sid、assert_temp_root_outside_workspace。runner 是 ``python -m``
CLI 入口而非库导出——从包面移除它，``-m`` 执行才不会撞上包链里已加载的
runner 模块（runpy 双重导入警告）。非 win32 平台 import 即抛 OSError。
"""
from .errors import AggregateError, Win32Error
from .grant import AclWriteGrant
from .index import (UNSET, AclSandbox, AclSandboxChildResult, AclSandboxOptions,
                    AclSandboxSpawnOptions)
from .path_boundary import assert_private_temp_disjoint, assert_temp_root_outside_workspace
from .workspace_sid import temp_write_sid, workspace_write_sid

__all__ = [
    "AggregateError",
    "Win32Error",
    "AclWriteGrant",
    "UNSET",
    "AclSandbox",
    "AclSandboxOptions",
    "AclSandboxSpawnOptions",
    "AclSandboxChildResult",
    "assert_private_temp_disjoint",
    "assert_temp_root_outside_workspace",
    "temp_write_sid",
    "workspace_write_sid",
]
