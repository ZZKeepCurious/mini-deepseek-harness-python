"""terminal-controller（api 组）：Session 作用域的浏览器终端远程控制。

对齐 upstream `packages/api/terminal-controller`：`TerminalController` 服务
（`ctx.terminalController`）经 `terminal_bash.provider` 的 PTY seam 提供
`environment` / `shells` / `list` / `create` / `attach` / `rename` / `close`；
`BrowserTerminal` 用 pyte 保存有界恢复屏幕，`TerminalFollower`
按 UTF-8 字节预算约束每路输出。web 层经 `terminal/*` 路由与 `terminal/attach`
双工流暴露同一契约（上行 `TerminalInput` 命令 + 下行 output/state/ack）。
"""

from .index import TerminalController, install_terminal_controller
from .shells import (
    SubprocessExecutableNotFoundError,
    discover_shells,
    resolve_executable,
    resolve_shell,
    terminal_environment,
)
from .stream import TerminalFollower
from .terminal import BrowserTerminal, TerminalAttachment
from .types import (
    ERROR_CONTROL_UNAVAILABLE,
    ERROR_LIMIT_REACHED,
    IDENTITY_PATTERN,
    TERMINAL_CONFIG_DEFAULTS,
    TerminalControlUnavailable,
    TerminalLimitReached,
    is_identity,
    resolve_config,
)

__all__ = [
    "BrowserTerminal",
    "ERROR_CONTROL_UNAVAILABLE",
    "ERROR_LIMIT_REACHED",
    "IDENTITY_PATTERN",
    "SubprocessExecutableNotFoundError",
    "TERMINAL_CONFIG_DEFAULTS",
    "TerminalAttachment",
    "TerminalControlUnavailable",
    "TerminalController",
    "TerminalFollower",
    "TerminalLimitReached",
    "discover_shells",
    "install_terminal_controller",
    "is_identity",
    "resolve_config",
    "resolve_executable",
    "resolve_shell",
    "terminal_environment",
]
