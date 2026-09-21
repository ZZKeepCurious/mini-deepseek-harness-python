"""Shell 选择与可执行验证（对齐 terminal-controller/src/shells.ts）。

发现优先列出执行环境声明的缺省 shell；仅当 provider 未声明时才回退 `/bin/sh`
（POSIX）或 `cmd.exe`（Windows）。可执行解析语义对齐 subprocess-local
`resolveExecutable`（绝对路径直接验证；裸名按 PATH × PATHEXT 探测；相对路径拒）。
"""
from __future__ import annotations

import os
import sys
from typing import Any

__all__ = [
    "SubprocessExecutableNotFoundError",
    "terminal_environment",
    "resolve_executable",
    "profile",
    "resolve_shell",
    "discover_shells",
]


class SubprocessExecutableNotFoundError(Exception):
    """命令不在执行环境的可执行集合内（对齐 dsh-subprocess 同名校验错误）。"""


def terminal_environment() -> dict:
    """平台与执行环境的缺省 shell（subprocess-local terminalEnvironment 等价）。"""
    if sys.platform.startswith("win"):
        default = os.environ.get("ComSpec") or None
        environment = {"platform": "windows"}
    else:
        default = os.environ.get("SHELL")
        if not default:
            try:
                import pwd

                default = pwd.getpwuid(os.getuid()).pw_shell
            except Exception:  # noqa: BLE001 - 无法读取账户库即无缺省声明
                default = None
        environment = {"platform": "posix"}
    if default:
        environment["defaultShell"] = default
    return environment


def _executable_candidates(command: str, env: Any) -> list[str]:
    path = (env.get("PATH") if env is not None else os.environ.get("PATH")) or ""
    extensions = [""]
    if sys.platform.startswith("win") and os.path.splitext(command)[1] == "":
        pathext = (env.get("PATHEXT") if env is not None
                   else os.environ.get("PATHEXT")) or ".COM;.EXE;.BAT;.CMD"
        extensions = pathext.split(";")
    candidates: list[str] = []
    for directory in path.split(os.pathsep):
        for extension in extensions:
            candidates.append(os.path.abspath(os.path.join(directory, command + extension)))
    return candidates


def resolve_executable(command: str, env: Any = None, signal=None) -> str:
    """把一个裸名或绝对路径解析为可执行文件路径；失败抛稳定错误。"""
    if not command:
        raise ValueError("subprocess: executable must be non-empty")
    if signal is not None:
        signal.throw_if_aborted()
    absolute = os.path.isabs(command)
    if not absolute and ("/" in command or (sys.platform.startswith("win") and "\\" in command)):
        raise RuntimeError(
            f"subprocess: command {command!r} is a relative path; use an absolute "
            "path or a bare PATH name")
    candidates = [command] if absolute else _executable_candidates(command, env)
    for candidate in candidates:
        if signal is not None:
            signal.throw_if_aborted()
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    if absolute:
        raise SubprocessExecutableNotFoundError(
            f"subprocess: command {command!r} is not an executable file")
    raise SubprocessExecutableNotFoundError(
        f"subprocess: command {command!r} was not found on PATH")


def profile(path: str) -> dict:
    """由可执行路径派生交互式 profile（shells.ts:24-28）。"""
    name = path[max(path.rfind("/"), path.rfind("\\")) + 1:]
    kind = name.lower()
    if kind.endswith(".exe"):
        kind = kind[:-4]
    if kind == "cmd":
        args: list[str] = []
    elif kind in ("pwsh", "powershell"):
        args = ["-NoLogo"]
    else:
        args = ["-i"]
    return {"path": path, "name": name, "args": args}


def resolve_shell(configured: dict | None, signal=None) -> dict:
    """解析配置的 shell 或执行环境的缺省 shell（shells.ts:12-22）。"""
    shell = configured
    if shell is None:
        environment = terminal_environment()
        fallback = "cmd.exe" if environment["platform"] == "windows" else "/bin/sh"
        shell = profile(environment.get("defaultShell") or fallback)
    path = resolve_executable(shell["path"], None, signal)
    return {**shell, "path": path}


def discover_shells(configured: dict | None, candidates: list[str], signal=None) -> list[dict]:
    """列出已验证候选，缺省或配置的 shell 在最前（shells.ts:38-57）。"""
    preferred = resolve_shell(configured, signal)
    found: list[dict | None] = []
    for candidate in candidates:
        try:
            found.append(resolve_shell(profile(candidate), signal))
        except SubprocessExecutableNotFoundError:
            found.append(None)
    shells: dict[str, dict] = {}
    for shell in [preferred, *found]:
        if shell is None:
            continue
        key = shell["path"].lower() if "\\" in shell["path"] else shell["path"]
        if key not in shells:
            shells[key] = shell
    return list(shells.values())
