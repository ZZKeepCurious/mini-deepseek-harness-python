"""subprocess 接缝服务（`ctx.subprocess`）——对齐上游 subprocess 的 Service
Definition 面，功能对标迁移到既有载体。

上游 `packages/subprocess/subprocess`（Service Definition）+ `subprocess-local`
（15 文件：Windows Job 对象 / systemd scope / execve runner / node-pty / spill）。

mini 的功能表面已散布既有模块（verified-diffs §3.49 登记）：
  * `resolve_executable` / `terminal_environment` —— terminal_controller/shells.py
  * env 清洗（scrubbed_parent_env）—— seams/subprocess_env.py
  * spawn（Popen + 读/监视线程 + 内存缓冲）—— shell/ 执行器族
  * PTY —— terminal_bash/
  * 输出收集（OutputBuffer 绝对偏移 + UTF-8 安全裁头）—— shell/types.py

本模块把它们收编为**一个 `ctx.subprocess` 服务**（对齐上游 SubprocessRuntime
注入面），供未来消费者注入；不复制实现。平台专属的进程树 containment
（Windows Job / systemd scope / execve runner）在 Python 无等价载体，登记为
载体差异（mini 以 Popen + CREATE_NEW_PROCESS_GROUP 近似）。

不适用：spill 收集器（上游磁盘 spill）mini 以内存增长缓冲承载（§3.45）。
"""
from __future__ import annotations

import os
import re as _re
from typing import Any

from ..core.scope import Context, Service
from ..seams.subprocess_env import scrubbed_parent_env
from ..terminal_controller.shells import (
    SubprocessExecutableNotFoundError,
    resolve_executable,
    terminal_environment,
)

__all__ = [
    "DSH_ENV_PREFIX",
    "SENSITIVE_ENV_PATTERN",
    "SubprocessExecutableNotFoundError",
    "SubprocessRuntime",
    "install_subprocess",
    "resolve_executable",
    "scrubbed_parent_env",
    "terminal_environment",
]

SENSITIVE_ENV_PATTERN = _re.compile(r"KEY|PASSWORD|SECRET|TOKEN", _re.IGNORECASE)
DSH_ENV_PREFIX = "DSH_"


class SubprocessRuntime(Service):
    """`ctx.subprocess`：可执行解析 + 终端环境 + 环境清洗的服务门面。

    对齐上游 `SubprocessRuntime`（subprocess/src/index.ts）：resolveExecutable /
    terminalEnvironment / scrubbedParentEnv 的收编面。spawn/spawnTerminal 的
    实际执行由 shell/ 与 terminal_bash/ 承载（mini 载体差异，§3.49）。
    """

    provide = "subprocess"

    def __init__(self, ctx: Context):
        super().__init__(ctx, "subprocess")

    def resolve_executable(self, command: str, env: dict | None = None,
                           signal: Any = None) -> str:
        """把一个裸名/绝对路径解析为可执行文件（子进程契约）。"""
        return resolve_executable(command, env, signal)

    def terminal_environment(self) -> dict:
        """平台与缺省 shell 声明（terminalEnvironment 等价）。"""
        return terminal_environment()

    def scrubbed_parent_env(self, base: dict | None = None) -> dict:
        """父环境减去凭据形 + DSH_* 名字后的基底（scrubbedParentEnv 等价）。"""
        return scrubbed_parent_env(base)


def install_subprocess(ctx: Context) -> SubprocessRuntime:
    """装配 `ctx.subprocess`（幂等，重复装返回既有实例）。"""
    existing = ctx.get("subprocess")
    if existing is not None:
        return existing
    return SubprocessRuntime(ctx)