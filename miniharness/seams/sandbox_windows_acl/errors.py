"""fail-closed Win32 错误类型（上游 errors.ts 对应物）。

后端每个 Win32 API 失败都抛本类型并携带 API 名与精确错误码；原 POC 静默
忽略一切失败调用、会以**未受限**令牌放跑子进程（fail-open）——本类存在的
意义就是杜绝该失败模式。消息形态逐字对齐上游：``{api} failed (Win32 {code})``。

另提供 AggregateError 垫片：Python 3.10 无 JS 同名内建，dispose/init 的
清理失败聚合语义（收集全部失败再统一报告）需要一个承载结构；errors 列表
挂在 ``.errors`` 上供调用方检查。
"""
from __future__ import annotations


class Win32Error(Exception):
    """单个 Win32 API 失败：API 名 + 精确错误码（BOOL API 取 GetLastError，
    ACL/HRESULT 风格 API 的返回值即错误码）。"""

    def __init__(self, api: str, win32_code: int, detail: str | None = None):
        message = f"{api} failed (Win32 {win32_code})"
        if detail is not None:
            message += f": {detail}"
        super().__init__(message)
        self.api = api
        self.win32_code = win32_code


class AggregateError(Exception):
    """JS AggregateError 垫片：best-effort 清理的多个失败一起上报。

    上游在 AclWriteGrant.dispose / AclSandbox.dispose / init 失败清理处用
    AggregateError 汇报「主操作完成但有 N 个清理失败」；Python 3.10 无
    ExceptionGroup，以本类承载同一语义（.errors 为失败列表）。
    """

    def __init__(self, errors: list[BaseException], message: str):
        super().__init__(message)
        self.errors = list(errors)
