""".env 解析：KEY=VALUE 行；# 注释与空行忽略；值剥离成对引号。

归属说明（架构文档 §5 规则 4，迁移步骤 3 已决议）：上游 .env 加载在
app-boot（loadEnv），mini 原实现放在凭据层导致 boot → seams 跨域依赖；
提升为本模块（boot 域）后，凭据层（seams，L3）反向引用（L3 → L1 合法）。
覆盖上游 launch-environment 常见子集；未定义行为之外的行跳过。
"""
from __future__ import annotations

import re

__all__ = ["parse_dotenv"]

_POSIX_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_posix_identifier(key: str) -> bool:
    return bool(_POSIX_IDENTIFIER.match(key))


def parse_dotenv(text: str) -> dict[str, str]:
    entries: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not _is_posix_identifier(key):
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
            value = value[1:-1]
        if value:
            entries[key] = value
    return entries