"""输出保留工具叶模块（对齐 packages/util/output-retention）。

上游对照：packages/util/output-retention/src/index.ts:445-460
`truncateWithoutSplittingSurrogatePair`。持久会话日志一旦携带孤立的 UTF-16
高代理项就不是良构文本，严格 JSON 读取器会拒绝；本函数在按上限截断时丢弃
切口落在代理对内部的半个字符。

载体差异（登记）：上游字符串是 UTF-16 码元序列（`text.length` 即码元数）；
Python `str` 是码点序列（BMP 外字符一个码点 = JS 两个码元）。为与上游逐字
等价，这里按 UTF-16 码元计量与截断，并镜像「切口落在孤立高代理项则丢弃」。
"""
from __future__ import annotations

__all__ = ["utf16_length", "truncate_without_splitting_surrogate_pair"]


def utf16_length(text: str) -> int:
    """`text` 的 UTF-16 码元数（BMP 外每字符计 2）。"""
    return sum(2 if ord(char) > 0xFFFF else 1 for char in text)


def truncate_without_splitting_surrogate_pair(text: str, max_chars: int) -> str:
    """把 `text` 截到 `max_chars` 个 UTF-16 码元以内，且不引入孤立高代理项。

    切口落在代理对内部时丢弃不成对的半个字符（结果比上限少一个码元）。
    文本自带的孤立代理项不在此修复（与上游一致：仅处理切口处）。
    """
    if utf16_length(text) <= max_chars:
        return text
    parts: list[str] = []
    used = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if used + width > max_chars:
            break
        parts.append(char)
        used += width
    capped = "".join(parts)
    if capped and 0xD800 <= ord(capped[-1]) <= 0xDBFF:
        return capped[:-1]
    return capped
