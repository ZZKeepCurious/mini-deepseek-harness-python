"""Team 领域输入规范：requiredText + writeScope（对齐上游 validation.ts）。"""

from __future__ import annotations

import re

from .error import TeamError

__all__ = ["required_text", "write_scope"]

_ABSOLUTE_WINDOWS_PATH = re.compile(r"^[a-z]:", re.IGNORECASE)


def required_text(value: str, field: str, max_length: int) -> str:
    """规范化一段必填人工文本：trim 后非空且不超过 maxLength 字符。

    对齐上游 requiredText：空 → TEAM_INVALID_ARGUMENT，超长 → TEAM_INVALID_ARGUMENT。
    """
    text = value.strip()
    if len(text) == 0:
        raise TeamError(f"{field} must be non-empty", "TEAM_INVALID_ARGUMENT")
    if len(text) > max_length:
        raise TeamError(f"{field} exceeds {max_length} characters", "TEAM_INVALID_ARGUMENT")
    return text


def write_scope(value: str) -> str:
    """规范化一个工作区相对路径前缀（不视为锁）。

    对齐上游 writeScope：反斜杠归一为斜杠、去前导 `./` 去尾斜杠；空 / 绝对 / 盘符 / 空段 /
    `.` / `..` 一律拒。
    """
    normalized = re.sub(r"^\./", "", value.replace("\\", "/"))
    while normalized.endswith("/"):
        normalized = normalized[:-1]
    segments = normalized.split("/")
    if (
        len(normalized) == 0
        or normalized.startswith("/")
        or _ABSOLUTE_WINDOWS_PATH.match(normalized) is not None
        or any(seg == "" or seg == "." or seg == ".." for seg in segments)
    ):
        raise TeamError(
            f"invalid workspace-relative write scope {value!r}", "TEAM_INVALID_WRITE_SCOPE"
        )
    return normalized