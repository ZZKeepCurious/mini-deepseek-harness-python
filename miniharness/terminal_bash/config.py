"""terminal-bash backend 配置（对齐 upstream terminal-bash/src/config.ts）。

* Config 全部可选字段（Schemastery 缺省在 SCHEMA_DEFAULTS 物化）；
* resolve_config —— 显式默认步骤：未设或空串/空数组的 shellPath/shellArgs 取方言缺省，
  非空显式值恒优先（config.ts:60-81）；
* validate_config —— 逐字段正安全整数、backendType/shellPath 非空、maxReadBytes ≤
  scrollbackMaxBytes、handoffGraceMs ≥ pollIntervalMs（config.ts:107-121）。
"""

from __future__ import annotations

import os
import shutil

__all__ = [
    "DEFAULT_BASH_ARGS",
    "DEFAULT_BASH_SHELL",
    "DEFAULT_PWSH_ARGS",
    "resolve_config",
    "resolve_pwsh_path",
    "validate_config",
    "SCHEMA_DEFAULTS",
]

SHELL_DIALECTS = ("bash", "pwsh")

DEFAULT_BASH_SHELL = "/bin/bash"
DEFAULT_BASH_ARGS = ["--noprofile", "--norc", "-i"]
DEFAULT_PWSH_ARGS = ["-NoLogo", "-NoProfile"]

#: 上游 config.ts:84-100 的 Schemastery 缺省物化。
SCHEMA_DEFAULTS = {
    "backendType": "shell",
    "shellDialect": "bash",
    "rows": 40,
    "cols": 160,
    "scrollbackLines": 10_000,
    "scrollbackMaxBytes": 4 * 1024 * 1024,
    "maxReadBytes": 256 * 1024,
    "pollIntervalMs": 50,
    "exactProbeAfterMs": 150,
    "idleSilenceMs": 3_000,
    "handoffGraceMs": 500,
    "timeoutMs": 30_000,
    "disposeGraceMs": 3_000,
}

#: 数值配置字段（validate_config 逐项断言正安全整数；不含 bool）。
_NUMERIC_FIELDS = (
    "rows", "cols", "scrollbackLines", "scrollbackMaxBytes", "maxReadBytes",
    "pollIntervalMs", "exactProbeAfterMs", "idleSilenceMs", "handoffGraceMs",
    "timeoutMs", "disposeGraceMs",
)


def resolve_pwsh_path() -> str:
    """方言缺省 pwsh 解析（上游 dsh-pwsh-local resolvePwshPath 的最佳努力等价面）。

    Windows：先 PATH 再 Program Files/PowerShell 7 标准安装位；POSIX：`pwsh` on PATH。
    解析不到 raise fail loud（上游同列的 Windows 探测命令在 mini 简化登记）。
    """
    candidates = []
    if os.name == "nt":
        found = shutil.which("pwsh")
        if found:
            return found
        base = os.environ.get("ProgramFiles", r"C:\Program Files")
        candidates = [
            os.path.join(base, "PowerShell", "7", "pwsh.exe"),
            os.path.join(base, "PowerShell", "7-preview", "pwsh.exe"),
        ]
    else:
        found = shutil.which("pwsh")
        if found:
            return found
    for candidate in candidates:
        if os.path.exists(candidate):
            return candidate
    raise RuntimeError("cannot resolve pwsh executable; set terminal-bash shellPath explicitly")


def resolve_config(config: dict) -> dict:
    """应用缺省 + 方言解析，返回全量 ResolvedConfig（config.ts:69-81）。"""
    resolved = dict(SCHEMA_DEFAULTS)
    for key, value in dict(config or {}).items():
        if value is not None:
            resolved[key] = value
    dialect = resolved["shellDialect"]
    if dialect not in SHELL_DIALECTS:
        raise ValueError(f"terminal-bash: unknown shellDialect {dialect!r} (known: {list(SHELL_DIALECTS)})")
    unified = str(resolved.get("shellPath") or "")
    unified_args = list(resolved.get("shellArgs") or [])
    resolved["shellPath"] = unified if unified else (
        resolve_pwsh_path() if dialect == "pwsh" else DEFAULT_BASH_SHELL)
    resolved["shellArgs"] = unified_args if unified_args else (
        DEFAULT_PWSH_ARGS if dialect == "pwsh" else list(DEFAULT_BASH_ARGS))
    return resolved


def validate_config(config: dict) -> dict:
    """断言配置组合合法，返回原 config（config.ts:107-121）。"""
    if not config["backendType"]:
        raise RuntimeError("terminal-bash: backendType must be non-empty")
    if not config["shellPath"]:
        raise RuntimeError("terminal-bash: shellPath must be non-empty")
    for name in _NUMERIC_FIELDS:
        value = config.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise RuntimeError(f"terminal-bash: {name} must be a positive safe integer")
    if config["maxReadBytes"] > config["scrollbackMaxBytes"]:
        raise RuntimeError("terminal-bash: maxReadBytes must not exceed scrollbackMaxBytes")
    if config["handoffGraceMs"] < config["pollIntervalMs"]:
        raise RuntimeError(
            "terminal-bash: handoffGraceMs must be at least pollIntervalMs so one readiness poll runs inside the grace window")
    return config