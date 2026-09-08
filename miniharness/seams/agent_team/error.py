"""Agent Teams 域专有错误（对齐上游 packages/experimental/agent-team/src/error.ts）。"""

from __future__ import annotations

__all__ = ["TeamError", "error_message"]


class TeamError(Exception):
    """Agent Teams 稳定失败：携带域内机器可读错误码（上游 TeamError extends HarnessError）。"""

    def __init__(self, message: str, code: str):
        super().__init__(message)
        self.code = code
        self.name = "TeamError"


def error_message(error: object) -> str:
    """把任意抛出值渲染为有界单行诊断，不替换原始拒绝。

    对齐上游 errorMessage()：Error 取 message，str 原样，其余 inspect。
    """
    if isinstance(error, BaseException):
        return str(error)
    if isinstance(error, str):
        return error
    return repr(error)