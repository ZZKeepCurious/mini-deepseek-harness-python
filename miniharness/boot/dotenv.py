""".env 解析：KEY=VALUE 行；# 注释与空行忽略；值剥离成对引号。

归属说明（架构文档 §5 规则 4，迁移步骤 3 已决议）：上游 .env 加载在
app-boot（loadEnv），mini 原实现放在凭据层导致 boot → seams 跨域依赖；
提升为本模块（boot 域）后，凭据层（seams，L3）反向引用（L3 → L1 合法）。
覆盖上游 launch-environment 常见子集；未定义行为之外的行跳过。

bootstrap-only 拒绝（is_bootstrap_only，对齐上游 index.ts:92-128）：
这些名字只允许启动环境设置——它们决定进程如何启动、代码与指令从哪加载、
如何触达网络。发现文件设置它们 → 整体拒绝（fail loud）。名单为上游全集
的 Python 侧映射：进程/网络/VCS 名通用保留，Node 启动名换为 Python 对应物
（PYTHONPATH/PYTHONSTARTUP/PYTHONHOME），前缀 DSH_/XDG_/DYLD_/BASH_FUNC_ 不变。
"""
from __future__ import annotations

import re

__all__ = ["BootstrapEnvNameError", "is_bootstrap_only", "parse_dotenv"]

_POSIX_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# 精确名单（上游 BOOTSTRAP_NAMES 的 Python 侧映射）
BOOTSTRAP_NAMES = frozenset({
    # 进程启动与模块解析
    "PATH", "HOME", "USERPROFILE", "SHELL",
    "PYTHONPATH", "PYTHONSTARTUP", "PYTHONHOME",
    "LD_PRELOAD", "LD_LIBRARY_PATH", "LD_AUDIT",
    # 解释器启动钩子（shell 侧保留）
    "BASH_ENV", "ENV", "SHELLOPTS", "BASHOPTS",
    "PERL5OPT", "PERL5LIB", "RUBYOPT", "RUBYLIB",
    "JAVA_TOOL_OPTIONS", "_JAVA_OPTIONS", "JDK_JAVA_OPTIONS",
    # 版本控制命令钩子与配置重定向
    "GIT_SSH", "GIT_SSH_COMMAND", "GIT_EXTERNAL_DIFF", "GIT_PAGER", "GIT_EDITOR",
    "GIT_ASKPASS", "SSH_ASKPASS",
    "GIT_CONFIG_GLOBAL", "GIT_CONFIG_SYSTEM", "GIT_CONFIG_COUNT",
    "EDITOR", "VISUAL", "PAGER",
    # 网络可达与信任
    "DEEPSEEK_BASE_URL", "DEEPSEEK_SEARCH_BASE_URL",
    "SSL_CERT_FILE", "SSL_CERT_DIR",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
})

# 前缀名单（上游 BOOTSTRAP_PREFIXES）
BOOTSTRAP_PREFIXES = ("DSH_", "XDG_", "DYLD_", "BASH_FUNC_")


class BootstrapEnvNameError(ValueError):
    """.env 声明了 bootstrap-only 名字（只允许启动环境设置）。"""


def _is_posix_identifier(key: str) -> bool:
    return bool(_POSIX_IDENTIFIER.match(key))


def is_bootstrap_only(name: str) -> bool:
    """该名字是否只允许继承的进程环境设置（上游 isBootstrapOnly）。"""
    upper = name.upper()
    return upper in BOOTSTRAP_NAMES or upper.startswith(BOOTSTRAP_PREFIXES)


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