"""服务端写授权物化（上游 grant.ts 对应物）。沙箱 seam 持有：每工作区一把
常驻 workspace 授权 + 每个活跃会话/工作区对一把可撤销 temp 授权。工作区身份
靠确定性派生存活；temp 身份派生自随机私有路径，重启后有意全新。

fail-closed：``create`` 任何失败都抛、此刻尚无任何授权；``dispose`` 撤销全部
可撤销授权并上报每个清理失败。
"""
from __future__ import annotations

from .acl import grant_write, revoke_write
from .errors import AggregateError
from .ffi import alloc_ptr_slot, decode_ptr, is_null_ptr, throw_last_error, win32_sync


class AclWriteGrant:
    """一个写 SID 在 provider 生命周期内的授权物化：解析后的 SID 指针 +
    当前 DACL 携带其 ACE 的全部目录。workspace 路径以**常驻**形态添加（其
    ACE 是跨会话复用缓存，活得比 grant 长——dispose 跳过撤销，否则下一次
    provision 要重传播整棵树）；temp 路径可撤销（dispose 撤销——可继承的
    ACE 绝不能比其会话的 temp 目录活得久）。用 create 构造；dispose 撤销
    可撤销路径并释放 SID。"""

    def __init__(self, api, sid_addr: int, write_sid: str):
        self._api = api
        self._sid_addr = sid_addr
        self.write_sid = write_sid
        self._revocable_paths: list[str] = []
        self._standing_paths: list[str] = []

    @classmethod
    def create(cls, write_sid: str, api=None) -> "AclWriteGrant":
        """解析 SID 字符串并打开绑定表（每服务一次惰性加载）。fail-closed：
        任何失败抛出——此刻尚未授予任何东西。"""
        bindings = api if api is not None else win32_sync()
        sid_slot = alloc_ptr_slot()
        if bindings.convertStringSidToSidW(write_sid, sid_slot) == 0:
            throw_last_error(bindings, "ConvertStringSidToSidW", write_sid)
        sid_addr = decode_ptr(sid_slot)
        if sid_addr is None:
            throw_last_error(bindings, "ConvertStringSidToSidW", f"null SID for {write_sid}")
        return cls(bindings, sid_addr, write_sid)

    @property
    def sid_addr(self) -> int:
        return self._sid_addr

    def add(self, path: str, standing: bool = False) -> None:
        """在一个目录上加写 ACE（幂等：已常驻的精确 ACE 会跳过急切的全树
        重传播，见 acl.grantWrite）。路径**先记录后授权**：应用后的异常
        （SetNamedSecurityInfoW 成功但 LocalFree 失败）仍须撤销该路径，而撤销
        未授权路径是无害的 no-op 合并。调用方把抛错视为物化失败并 dispose
        实例以撤销已授予路径。"""
        (self._standing_paths if standing else self._revocable_paths).append(path)
        grant_write(self._api, path, self._sid_addr)

    @property
    def paths(self) -> list[str]:
        """当前携带本授权的全部目录，按授权顺序。"""
        return [*self._standing_paths, *self._revocable_paths]

    def dispose(self) -> None:
        """撤销每个可撤销授权（常驻 ACE 保留）并释放 SID；上报所有清理失败。"""
        failures: list[BaseException] = []
        for path in self._revocable_paths:
            try:
                revoke_write(self._api, path, self._sid_addr)
            except Exception as error:  # noqa: BLE001 - 聚合一切清理失败
                failures.append(error)
        try:
            freed = self._api.localFree(self._sid_addr)
            if not is_null_ptr(freed):
                throw_last_error(self._api, "LocalFree", "write SID")
        except Exception as error:  # noqa: BLE001 - 同上
            failures.append(error)
        if failures:
            raise AggregateError(
                failures,
                f"AclWriteGrant dispose completed with {len(failures)} cleanup failure(s)")
