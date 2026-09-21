"""terminal 域：owner 作用域持久 PTY 注册表 + backend 契约（P1 核心）。

对齐 upstream `terminal/` 包族：终端服务注册表（terminal/terminal）、bounded
缓冲与 sanitizer（terminal-bash 的确定性面）、tool-terminal/controller 后续
分阶段接线（design-terminal-domain.md）。P1 只含纯确定性核心；真 PTY provider
（pywinpty/pty）与交互读写/就绪轮询在 P2。
"""

from .types import (
    CONTROLLED_PROMPT,
    PROMPT_MARKER_PREFIX,
    TERMINAL_ERROR_CODES,
    TERMINAL_SIGNALS,
    TERMINAL_WAIT_REASONS,
    Cancellation,
    TerminalBackend,
    TerminalBackendCleanupError,
    TerminalError,
)
from .sanitize import TerminalSanitizer, normalize_terminal_text
from .bounded_buffer import BoundedTextBuffer, read_scrollback, utf8_tail
from .operation import LocalSendOperation
from .service import TerminalSessionService, install_terminals

__all__ = [
    "CONTROLLED_PROMPT",
    "PROMPT_MARKER_PREFIX",
    "TERMINAL_ERROR_CODES",
    "TERMINAL_SIGNALS",
    "TERMINAL_WAIT_REASONS",
    "BoundedTextBuffer",
    "Cancellation",
    "LocalSendOperation",
    "TerminalBackend",
    "TerminalBackendCleanupError",
    "TerminalError",
    "TerminalSanitizer",
    "TerminalSessionService",
    "install_terminals",
    "normalize_terminal_text",
    "read_scrollback",
    "utf8_tail",
]