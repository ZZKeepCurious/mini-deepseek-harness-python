"""terminal-bash 后端：真实 PTY 会话载体（P2）。

对齐 upstream `terminal/terminal-bash` 包：配置解析/校验、shell 子进程环境、
终端协议应答、平台 PTY provider、LocalPtySession 就绪状态机与 BashTerminalBackend
装配。确定性面（config/emulator/session 状态机）可直接单测；真实载体
（pywinpty/pty）以冒烟覆盖。
"""

from .config import (
    DEFAULT_BASH_ARGS,
    DEFAULT_BASH_SHELL,
    DEFAULT_PWSH_ARGS,
    SCHEMA_DEFAULTS,
    resolve_config,
    resolve_pwsh_path,
    validate_config,
)
from .emulator import TerminalProtocolEmulator
from .environment import CONTROLLED_PROMPT, ENCODING_PREAMBLE, PWSH_PROMPT_SETUP, child_environment
from .index import BashTerminalBackend, apply, install_terminal_bash, spawn_argv, startup_session
from .provider import (
    SubprocessForeground,
    SubprocessOutcome,
    SubprocessTerminalActivity,
    TerminalHandle,
    TerminalOutputChannel,
    spawn_terminal,
)
from .session import LocalPtySession
from .shell_activity import ShellActivity, prepare_shell_activity

from ..terminal.types import TerminalError

__all__ = [
    "CONTROLLED_PROMPT",
    "DEFAULT_BASH_ARGS",
    "DEFAULT_BASH_SHELL",
    "DEFAULT_PWSH_ARGS",
    "ENCODING_PREAMBLE",
    "PWSH_PROMPT_SETUP",
    "SCHEMA_DEFAULTS",
    "BashTerminalBackend",
    "LocalPtySession",
    "ShellActivity",
    "SubprocessForeground",
    "SubprocessOutcome",
    "SubprocessTerminalActivity",
    "TerminalError",
    "TerminalHandle",
    "TerminalOutputChannel",
    "TerminalProtocolEmulator",
    "apply",
    "child_environment",
    "install_terminal_bash",
    "prepare_shell_activity",
    "resolve_config",
    "resolve_pwsh_path",
    "spawn_argv",
    "spawn_terminal",
    "startup_session",
    "validate_config",
]