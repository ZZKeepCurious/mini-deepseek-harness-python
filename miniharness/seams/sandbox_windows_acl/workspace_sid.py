"""每工作区写身份：确定性派生的 ``S-1-4-x-y`` SID（上游 workspace-sid.ts 对应物）。

同一工作区的每次受限执行——跨会话、跨服务重启、跨调用——携带**同一**写 SID，
工作区根 ACE 因此每机器每工作区只物化一次（grant 的 exact-ACE 跳过使后续
provision O(1)），而不是每会话一次全树传播。SID 的权限完全由指名它的 ACE
定义（只存在于工作区树与会话私有 temp 目录上），且只有为该工作区铸造的令牌
才带它——SID 字符串本身不是秘密。临时目录用 {@ temp_write_sid} 的独立
每目录身份；与工作区共用身份会让同工作区的兄弟会话互写对方 temp 树。

输入**必须**是规范工作区路径（Windows 上即 realpath 产物——sandbox-policy
的 resolveWorkspaceRoot 已先做）：规范化收敛大小写/别名拼写，同一目录的两种
拼写派生同一 SID；按拼写原样回退会为同一目录铸出第二身份（可自愈，代价是多
一次全树传播）。重命名工作区目录派生新 SID——旧常驻 ACE 成惰性残留，下一
会话重新传播一次。
"""
from __future__ import annotations

import hashlib

_MODULUS = 2 ** 30 - 1


def workspace_write_sid(workspace_root: str) -> str:
    """从规范工作区路径派生写 SID（``S-1-4-x-y``，子权限各 30 位）。"""
    digest = hashlib.sha256(workspace_root.encode("utf-8")).digest()
    first = (int.from_bytes(digest[0:4], "little") % _MODULUS) + 1
    second = (int.from_bytes(digest[4:8], "little") % _MODULUS) + 1
    return f"S-1-4-{first}-{second}"


def temp_write_sid(temp_dir: str) -> str:
    """从一个私有 temp 目录的绝对路径派生其写 SID（固定第三子权限 ``-1``
    做域分隔，与一切两子权限的工作区 SID 区分开）。"""
    digest = hashlib.sha256(b"temp\x00" + temp_dir.encode("utf-8")).digest()
    first = (int.from_bytes(digest[0:4], "little") % _MODULUS) + 1
    second = (int.from_bytes(digest[4:8], "little") % _MODULUS) + 1
    return f"S-1-4-{first}-{second}-1"
